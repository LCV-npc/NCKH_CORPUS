"""Validated, snapshot-based dictionary loader for rule-only medical NER."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent


def _configured_path(name: str, default: Path) -> Path:
    """Resolve configured storage paths relative to ``backend/``.

    This keeps values in ``backend/.env`` portable while still allowing an
    absolute path for a deployment that stores the artifacts elsewhere.
    """
    raw_value = os.getenv(name, "").strip()
    path = Path(raw_value).expanduser() if raw_value else default
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


DATA_DIR = _configured_path("NCKH_DATA_DIR", PROJECT_DIR.parent / "data")
DICT_DIR = _configured_path("NCKH_DICTIONARY_DIR", BASE_DIR / "Tu Dien Y Hoc")
MANIFEST_PATH = DICT_DIR / "manifest_v1.json"
CONTROLLED_VOCABULARY_PATH = DICT_DIR / "controlled_vocabulary_v1.json"
ALLOWED_CATEGORIES = {"Bệnh Lý", "Triệu Chứng", "Đông Y / YHCT"}
COLOR_MAP = {
    "Bệnh Lý": "#86efac",
    "Triệu Chứng": "#93c5fd",
    "Đông Y / YHCT": "#fde047",
}


class DictionaryError(RuntimeError):
    """Base error for dictionary loading and snapshot publication."""


class DictionaryValidationError(DictionaryError):
    """The manifest or a versioned artifact is invalid."""


class DictionaryUnavailableError(DictionaryError):
    """No validated snapshot is available for annotation."""


@dataclass(frozen=True)
class DictSnapshot:
    """Immutable dictionary state used by one or more concurrent NER calls."""

    term_dict: Mapping[str, Mapping[str, Any]]
    term_candidates: Mapping[str, tuple[Mapping[str, Any], ...]]
    accentless_term_dict: Mapping[str, Mapping[str, Any]]
    accentless_term_candidates: Mapping[str, tuple[Mapping[str, Any], ...]]
    ambiguous_term_keys: frozenset[str]
    max_words: int
    max_words_by_first_token: Mapping[str, int]
    generation: int
    metadata: Mapping[str, Any]


@dataclass
class _MutableDictionary:
    """Temporary build state; never exposed to the NER engine."""

    term_dict: dict[str, dict[str, Any]] = field(default_factory=dict)
    term_candidates: defaultdict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    accentless_term_dict: dict[str, dict[str, Any]] = field(default_factory=dict)
    accentless_term_candidates: defaultdict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    ambiguous_term_keys: set[str] = field(default_factory=set)
    invalid_categories: list[dict[str, Any]] = field(default_factory=list)
    alias_loaded: int = 0
    alias_rejected: int = 0


_snapshot: DictSnapshot | None = None
_snapshot_lock = threading.RLock()
_build_lock = threading.RLock()
_runtime_alias_rows: tuple[dict[str, Any], ...] = ()
_last_load_error: str | None = None
_last_load_error_type: str | None = None
_last_load_attempt_at: str | None = None


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_color(category: str) -> str:
    canonical = _normalize_category(category, warn=False) or "Bệnh Lý"
    return COLOR_MAP.get(canonical, "#e2e8f0")


def _normalize_category(value: str, *, warn: bool = True) -> str | None:
    raw = unicodedata.normalize("NFC", str(value or "")).strip().casefold()
    aliases = {
        "bệnh lý": "Bệnh Lý",
        "benh ly": "Bệnh Lý",
        "disease": "Bệnh Lý",
        "problem": "Bệnh Lý",
        "triệu chứng": "Triệu Chứng",
        "trieu chung": "Triệu Chứng",
        "symptom": "Triệu Chứng",
        "đông y / yhct": "Đông Y / YHCT",
        "dong y / yhct": "Đông Y / YHCT",
        "yhct": "Đông Y / YHCT",
    }
    category = aliases.get(raw)
    if category:
        return category
    if warn and raw:
        log.warning("Bỏ qua category không được hỗ trợ trong dictionary: %s", value)
    return None


def icd10_entity_type(code: str) -> Optional[str]:
    compact = str(code or "").strip().upper().replace(".", "")
    if compact.startswith("R"):
        return "Triệu Chứng"
    if compact.startswith(("V", "W", "X", "Y", "Z")):
        return None
    return "Bệnh Lý"


def normalize_match_text(text: str) -> str:
    """Normalize only for lookup; offsets always refer to untouched text."""
    normalized = unicodedata.normalize("NFC", str(text or "")).casefold()
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    return " ".join(words)


def strip_diacritics(text: str) -> str:
    normalized = normalize_match_text(text).replace("đ", "d")
    return "".join(
        char
        for char in unicodedata.normalize("NFD", normalized)
        if unicodedata.category(char) != "Mn"
    )


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"", "0", "false", "no", "off", "null", "none"}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return bool(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise DictionaryValidationError(f"Không đọc được JSON {path.name}: {exc}") from exc


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(value)
    return value


def get_snapshot() -> DictSnapshot:
    """Return the current immutable snapshot, loading it lazily if needed."""
    with _snapshot_lock:
        current = _snapshot
    if current is not None:
        return current

    try:
        load_ner_dictionary()
    except Exception as exc:
        if isinstance(exc, DictionaryError):
            raise DictionaryUnavailableError(str(exc)) from exc
        raise DictionaryUnavailableError(f"Không nạp được từ điển: {exc}") from exc

    with _snapshot_lock:
        current = _snapshot
    if current is None:
        raise DictionaryUnavailableError("Từ điển ICD-10/YHCT chưa sẵn sàng.")
    return current


def get_dictionary_metadata(snapshot: DictSnapshot | None = None) -> dict[str, Any]:
    snapshot = snapshot or get_snapshot()
    return _thaw(snapshot.metadata)


def get_dictionary_runtime_status() -> dict[str, Any]:
    with _snapshot_lock:
        current = _snapshot
        error = _last_load_error
        error_type = _last_load_error_type
        attempted_at = _last_load_attempt_at
    if current is None:
        return {
            "available": False,
            "last_load_error": error,
            "last_reload_error": error,
            "last_load_error_type": error_type,
            "last_load_attempt_at": attempted_at,
        }
    payload = get_dictionary_metadata(current)
    payload.update(
        {
            "available": True,
            "last_load_error": error,
            "last_reload_error": error,
            "last_load_error_type": error_type,
            "last_load_attempt_at": attempted_at,
        }
    )
    return payload


def _record_load_failure(exc: Exception) -> None:
    global _last_load_error, _last_load_error_type, _last_load_attempt_at
    with _snapshot_lock:
        _last_load_error = str(exc)
        _last_load_error_type = type(exc).__name__
        _last_load_attempt_at = _now_iso()


def _record_load_success() -> None:
    global _last_load_error, _last_load_error_type, _last_load_attempt_at
    with _snapshot_lock:
        _last_load_error = None
        _last_load_error_type = None
        _last_load_attempt_at = _now_iso()


def _add(
    state: _MutableDictionary,
    term: str,
    category: str,
    code: str,
    label_vn: str = "",
    source: str = "",
    **metadata: Any,
) -> bool:
    key = normalize_match_text(term)
    if len(key) < 2 or not re.search(r"[^\W\d_]", key, flags=re.UNICODE):
        return False
    if metadata.get("active_for_ner") is False:
        return False

    normalized_category = _normalize_category(category)
    if normalized_category is None:
        state.invalid_categories.append(
            {
                "term": str(term or "").strip(),
                "code": str(code or "").strip(),
                "source": str(source or "").strip(),
                "category": str(category or "").strip(),
            }
        )
        return False

    alias_type = str(metadata.get("alias_type") or "canonical").strip()
    case_sensitive_value = metadata.get("case_sensitive")
    case_sensitive = (
        _coerce_bool(case_sensitive_value)
        if case_sensitive_value is not None
        else alias_type == "abbreviation"
    )
    is_ambiguous = _coerce_bool(metadata.get("ambiguous"))
    candidate = {
        "cat": normalized_category,
        "concept_id": str(metadata.get("concept_id") or code or "").strip(),
        "concept_id_source": str(metadata.get("concept_id_source") or source or "").strip(),
        "code": str(code or "").strip(),
        "display_code": str(metadata.get("display_code") or code or "").strip(),
        "label_vn": str(label_vn or metadata.get("canonical_term") or term).strip(),
        "canonical_term": str(metadata.get("canonical_term") or label_vn or term).strip(),
        "match_term": str(term).strip(),
        "source": source or str(metadata.get("source") or ""),
        "alias_type": alias_type,
        "case_sensitive": case_sensitive,
        "allow_accentless": _coerce_bool(metadata.get("allow_accentless", False)),
        "ambiguous": is_ambiguous,
        "auto_matchable": not is_ambiguous,
        "icd_chapter_code": str(metadata.get("icd_chapter_code") or "").strip(),
        "icd_chapter_name": str(metadata.get("icd_chapter_name") or "").strip(),
        "rule_score": metadata.get("rule_score"),
        "score_type": str(metadata.get("score_type") or "").strip(),
        "score_calibrated": _coerce_bool(metadata.get("score_calibrated", False)),
        "is_dagger": _coerce_bool(metadata.get("is_dagger", False)) or "†" in str(code),
        "is_asterisk": _coerce_bool(metadata.get("is_asterisk", False)) or "*" in str(code),
        "qualifier": str(metadata.get("qualifier") or ""),
    }

    priority = {
        "approved_alias": 4,
        "custom_alias": 3,
        "yhct": 2,
        "icd10": 1,
    }
    identity = (candidate["concept_id"], candidate["cat"])
    values = state.term_candidates[key]
    existing_index = next(
        (
            index
            for index, value in enumerate(values)
            if (value.get("concept_id") or value.get("code"), value.get("cat")) == identity
        ),
        None,
    )
    if existing_index is not None:
        existing = values[existing_index]
        # A runtime/derived alias may share the same normalized surface as a
        # canonical entry.  Keep the canonical provenance in that case; an
        # alias must never replace the JSON source entry in the exact index.
        if (
            existing.get("alias_type") == "canonical"
            and normalize_match_text(existing.get("match_term") or "") == key
        ):
            return False
        if priority.get(existing.get("source", ""), 0) >= priority.get(candidate.get("source", ""), 0):
            return False
        values[existing_index] = candidate
    else:
        values.append(candidate)

    auto_candidates = [item for item in values if item.get("auto_matchable", True)]
    if len(values) == 1 and len(auto_candidates) == 1:
        state.term_dict[key] = auto_candidates[0]
        state.ambiguous_term_keys.discard(key)
    else:
        state.term_dict.pop(key, None)
        state.ambiguous_term_keys.add(key)
    return True


def _safe_relative_path(base: Path, raw_path: Any, label: str) -> tuple[Path, str]:
    value = str(raw_path or "").strip()
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise DictionaryValidationError(f"{label} phải là đường dẫn tương đối an toàn: {value!r}")
    resolved_base = base.resolve()
    resolved = (resolved_base / candidate).resolve()
    if resolved != resolved_base and resolved_base not in resolved.parents:
        raise DictionaryValidationError(f"{label} nằm ngoài thư mục được phép: {value!r}")
    return resolved, value.replace("\\", "/")


def _validate_payload(payload: Any, path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DictionaryValidationError(f"Artifact {path.name} phải là JSON object.")
    for field_name in ("schema_version", "dictionary_version", "kind", "entries"):
        if field_name not in payload:
            raise DictionaryValidationError(f"Artifact {path.name} thiếu field {field_name!r}.")
    for field_name in ("schema_version", "dictionary_version", "kind"):
        if not str(payload.get(field_name) or "").strip():
            raise DictionaryValidationError(f"Artifact {path.name} có field {field_name!r} rỗng.")
    entries = payload["entries"]
    if not isinstance(entries, list):
        raise DictionaryValidationError(f"Artifact {path.name}.entries phải là list.")
    try:
        expected_count = int(expected.get("count", -1))
    except (TypeError, ValueError) as exc:
        raise DictionaryValidationError(f"Manifest có count không hợp lệ cho {path.name}.") from exc
    if len(entries) != expected_count:
        raise DictionaryValidationError(
            f"Sai số mục cho {path.name}: expected={expected_count}, actual={len(entries)}"
        )

    seen_ids: set[str] = set()
    is_controlled = payload.get("kind") == "controlled_vocabulary"
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DictionaryValidationError(f"{path.name}.entries[{index}] phải là object.")
        required = (
            (
                "surface_form",
                "canonical_term",
                "concept_id",
                "concept_code",
                "match_method",
                "concept_id_source",
                "entity_type",
                "rule_id",
                "status",
            )
            if is_controlled
            else ("id", "canonical_term", "code", "active_for_ner", "ambiguous")
        )
        missing = [key for key in required if key not in entry]
        if missing:
            raise DictionaryValidationError(
                f"{path.name}.entries[{index}] thiếu field: {', '.join(missing)}"
            )
        raw_identifier = entry.get("concept_id") if is_controlled else entry.get("id")
        identifier = str(raw_identifier or "").strip()
        canonical = str(entry.get("canonical_term") or "").strip()
        if not identifier or not canonical:
            raise DictionaryValidationError(
                f"{path.name}.entries[{index}] có Concept ID/canonical term rỗng."
            )
        if is_controlled and not str(entry.get("concept_code") or "").strip():
            raise DictionaryValidationError(
                f"{path.name}.entries[{index}] có Concept code rỗng."
            )
        if is_controlled and not str(entry.get("match_method") or "").strip():
            raise DictionaryValidationError(
                f"{path.name}.entries[{index}] có match_method rỗng."
            )
        if is_controlled and str(entry.get("concept_id_source") or "").strip().upper() != "ICD10":
            raise DictionaryValidationError(
                f"{path.name}.entries[{index}] phải tham chiếu Concept ID từ ICD10."
            )
        if is_controlled and not str(entry.get("entity_type") or "").strip():
            raise DictionaryValidationError(
                f"{path.name}.entries[{index}] có entity_type rỗng."
            )
        if is_controlled and not str(entry.get("rule_id") or "").strip():
            raise DictionaryValidationError(
                f"{path.name}.entries[{index}] có rule_id rỗng."
            )
        if identifier in seen_ids and not is_controlled:
            raise DictionaryValidationError(f"Trùng Concept ID {identifier!r} trong {path.name}.")
        if not is_controlled:
            seen_ids.add(identifier)
    return payload


def _verified_payload(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        raise DictionaryValidationError(f"Không tìm thấy dictionary artifact: {path}")
    expected_hash = str(expected.get("sha256") or "").strip()
    if not expected_hash:
        raise DictionaryValidationError(f"Manifest thiếu SHA-256 cho {path.name}.")
    actual_hash = _sha256(path)
    if actual_hash != expected_hash:
        raise DictionaryValidationError(
            f"Sai SHA-256 cho {path.name}: expected={expected_hash}, actual={actual_hash}"
        )
    payload = _validate_payload(_read_json(path), path, expected)
    return payload


def _verify_source_snapshot(payload: Mapping[str, Any], artifact_name: str) -> dict[str, str]:
    source = payload.get("source")
    if not isinstance(source, Mapping):
        raise DictionaryValidationError(f"{artifact_name} thiếu block source.")
    filename = str(source.get("file") or "").strip()
    expected_hash = str(source.get("sha256") or "").strip()
    if not filename or not expected_hash:
        raise DictionaryValidationError(
            f"{artifact_name} phải có source.file và source.sha256 để bảo đảm tái lập."
        )
    source_path, relative_name = _safe_relative_path(DATA_DIR, filename, f"{artifact_name}.source.file")
    if not source_path.is_file():
        raise DictionaryValidationError(
            f"Không tìm thấy nguồn {relative_name} trong NCKH_DATA_DIR={DATA_DIR}."
        )
    actual_hash = _sha256(source_path)
    if actual_hash != expected_hash:
        raise DictionaryValidationError(
            f"Nguồn {relative_name} đã thay đổi sau khi tạo {artifact_name}; "
            "hãy chạy `python -m core.dictionary_builder`."
        )
    return {"file": relative_name, "sha256": actual_hash}


def _alias_values(alias: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(alias, Mapping):
        value = str(alias.get("term") or "").strip()
        extra = {
            "alias_type": alias.get("alias_type", "abbreviation"),
            "case_sensitive": alias.get("case_sensitive", True),
            "allow_accentless": alias.get("allow_accentless", False),
        }
        return value, extra
    return str(alias or "").strip(), {}


def _chapter_metadata(entry: Mapping[str, Any]) -> dict[str, str]:
    chapter = entry.get("chapter")
    if not isinstance(chapter, Mapping):
        return {"icd_chapter_code": "", "icd_chapter_name": ""}
    return {
        "icd_chapter_code": str(chapter.get("code") or "").strip(),
        "icd_chapter_name": str(chapter.get("name") or "").strip(),
    }


def _load_icd(state: _MutableDictionary, entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        if not entry.get("active_for_ner"):
            continue
        category = entry.get("category") or icd10_entity_type(entry.get("code", ""))
        if not category:
            continue
        common = {
            "concept_id": entry.get("id"),
            "concept_id_source": "ICD10",
            "display_code": entry.get("display_code"),
            "qualifier": entry.get("qualifier"),
            "is_dagger": bool(entry.get("qualifier") == "dagger"),
            "is_asterisk": bool(entry.get("qualifier") == "asterisk"),
            "canonical_term": entry.get("canonical_term"),
            "ambiguous": bool(entry.get("ambiguous")),
            **_chapter_metadata(entry),
        }
        _add(
            state,
            entry["canonical_term"],
            category,
            entry["code"],
            entry["canonical_term"],
            "icd10",
            **common,
        )
        for alias in entry.get("aliases") or []:
            value, extra = _alias_values(alias)
            if value:
                _add(
                    state,
                    value,
                    category,
                    entry["code"],
                    entry["canonical_term"],
                    "icd10",
                    alias_type=extra.pop("alias_type", "alias"),
                    **extra,
                    **common,
                )


def _load_yhct(state: _MutableDictionary, entries: list[dict[str, Any]]) -> None:
    active_forms = [
        entry
        for entry in entries
        if entry.get("record_type") == "clinical_form"
        and entry.get("active_for_ner")
        and not entry.get("ambiguous")
    ]
    form_codes: dict[str, set[str]] = defaultdict(set)
    for entry in active_forms:
        form_codes[normalize_match_text(entry["canonical_term"])].add(entry["code"])

    for entry in entries:
        if not entry.get("active_for_ner"):
            continue
        ambiguous = bool(entry.get("ambiguous"))
        if entry.get("record_type") == "base":
            common = {
                "concept_id": entry.get("id"),
                "concept_id_source": "YHCT",
                "display_code": entry.get("display_code"),
                "canonical_term": entry.get("canonical_term"),
                "ambiguous": ambiguous,
            }
            _add(state, entry["canonical_term"], "Đông Y / YHCT", entry["code"], entry["canonical_term"], "yhct", **common)
            _add(state, entry.get("yhct_term", ""), "Đông Y / YHCT", entry["code"], entry["canonical_term"], "yhct", alias_type="alias", **common)
            _add(
                state,
                entry.get("yhhd_term", ""),
                "Bệnh Lý",
                entry.get("icd10_code", ""),
                entry.get("yhhd_term", ""),
                "yhct",
                alias_type="alias",
                concept_id=entry.get("id"),
                concept_id_source="YHCT",
                display_code=entry.get("icd10_code"),
                canonical_term=entry.get("yhhd_term"),
                ambiguous=ambiguous,
            )
            continue

        form = entry["canonical_term"]
        common = {
            "concept_id": entry.get("id"),
            "concept_id_source": "YHCT",
            "display_code": entry.get("display_code"),
            "canonical_term": form,
            "alias_type": "clinical_form",
            "ambiguous": ambiguous,
        }
        if len(form_codes[normalize_match_text(form)]) == 1:
            _add(state, form, "Đông Y / YHCT", entry["code"], form, "yhct", **common)
        for parent in (entry.get("parent_yhct_term"), entry.get("parent_yhhd_term")):
            if parent:
                _add(state, f"{parent} {form}", "Đông Y / YHCT", entry["code"], form, "yhct", **common)


def _load_controlled_grammar_variants(
    state: _MutableDictionary,
    controlled_payload: Optional[dict[str, Any]] = None,
) -> None:
    """Load deterministic variants without inventing Concept IDs."""
    if controlled_payload is not None:
        valid_concept_ids = {
            str(value.get("concept_id") or "").strip()
            for values in state.term_candidates.values()
            for value in values
            if value.get("concept_id")
            and str(value.get("concept_id_source") or "").strip().upper() == "ICD10"
        }
        for item in controlled_payload.get("entries") or []:
            concept_id = str(item.get("concept_id") or "").strip()
            if not concept_id or not concept_id.startswith("ICD10:") or concept_id not in valid_concept_ids:
                raise DictionaryValidationError(
                    "Controlled vocabulary tham chiếu Concept ID không tồn tại: "
                    f"{concept_id or '<empty>'}"
                )
            concept_source = str(item.get("concept_id_source") or "").strip().upper()
            # This derived artifact is the bridge between the official ICD-10
            # labels and real-text surface forms for the NER corpus.  YHCT is
            # still loaded from its own runtime artifact, but it must not
            # introduce a Concept mapping into this ICD-10-only bridge.
            if concept_source != "ICD10":
                raise DictionaryValidationError(
                    "Controlled vocabulary của corpus chỉ được tham chiếu ICD10; "
                    f"nhận được {concept_source!r}"
                )
            source = "icd10"
            _add(
                state,
                str(item.get("surface_form") or ""),
                str(item.get("category") or ""),
                str(item.get("concept_code") or ""),
                str(item.get("canonical_term") or ""),
                source,
                alias_type=str(item.get("match_method") or "grammar_variant"),
                concept_id=concept_id,
                concept_id_source=concept_source,
                display_code=item.get("display_code"),
                canonical_term=item.get("canonical_term"),
                case_sensitive=item.get("case_sensitive", False),
                allow_accentless=item.get("allow_accentless", False),
                rule_score=item.get("rule_score"),
                score_type=item.get("score_type"),
                score_calibrated=item.get("score_calibrated", False),
                icd_chapter_code=item.get("icd_chapter_code"),
                icd_chapter_name=item.get("icd_chapter_name"),
            )
        return

    # Backward-compatible fallback for a manifest created before the derived
    # controlled_vocabulary artifact was introduced.
    source_entries = [
        dict(info)
        for values in state.term_candidates.values()
        for info in values
        if info.get("source") == "icd10"
    ]
    variants: dict[tuple[str, str, str], dict[str, Any]] = {}
    variant_rules = (
        ("các ", "grammar_variant", 0.98),
        ("những ", "grammar_variant", 0.98),
        ("bệnh lý ", "headword_variant", 0.97),
    )
    for info in source_entries:
        canonical = str(info.get("canonical_term") or info.get("label_vn") or "").strip()
        canonical_key = normalize_match_text(canonical)
        for prefix, alias_type, rule_score in variant_rules:
            if not canonical_key.startswith(prefix):
                continue
            variant = canonical[len(prefix):].strip()
            variant_key = normalize_match_text(variant)
            if len(variant_key.split()) < 2:
                continue
            identity = (variant_key, str(info.get("concept_id") or ""), str(info.get("cat") or ""))
            variants.setdefault(
                identity,
                {
                    "variant": variant,
                    "info": info,
                    "canonical": canonical,
                    "alias_type": alias_type,
                    "rule_score": rule_score,
                },
            )

    for item in variants.values():
        info = item["info"]
        _add(
            state,
            item["variant"],
            info.get("cat") or "",
            info.get("code") or "",
            info.get("label_vn") or item["canonical"],
            info.get("source") or "",
            alias_type=item["alias_type"],
            concept_id=info.get("concept_id"),
            concept_id_source=info.get("concept_id_source"),
            display_code=info.get("display_code"),
            canonical_term=item["canonical"],
            icd_chapter_code=info.get("icd_chapter_code"),
            icd_chapter_name=info.get("icd_chapter_name"),
            case_sensitive=False,
            allow_accentless=False,
            rule_score=item["rule_score"],
            score_type="heuristic",
            score_calibrated=False,
        )


def _build_accentless_index(state: _MutableDictionary) -> None:
    collisions: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for key, values in state.term_candidates.items():
        for info in values:
            if info.get("auto_matchable", True):
                collisions[strip_diacritics(key)].append((key, info))

    for no_tone, entries in collisions.items():
        if any(len(key.split()) < 2 and not info.get("allow_accentless") for key, info in entries):
            continue
        targets = {(value.get("concept_id") or value.get("code"), value.get("cat")) for _, value in entries}
        if len(targets) != 1 or all(no_tone == key for key, _ in entries):
            continue
        chosen = dict(sorted(entries, key=lambda item: (-len(item[0]), item[0]))[0][1])
        chosen["alias_type"] = "accentless"
        chosen["rule_score"] = chosen.get("rule_score") or 0.94
        chosen["score_type"] = "heuristic"
        chosen["score_calibrated"] = False
        state.accentless_term_candidates[no_tone] = [chosen]
        state.accentless_term_dict[no_tone] = chosen


def _build_indexes(state: _MutableDictionary) -> tuple[int, dict[str, int]]:
    max_words = max((len(key.split()) for key in state.term_candidates), default=1)
    max_words_by_first_token: dict[str, int] = {}
    for key in state.term_candidates:
        first = key.split(maxsplit=1)[0]
        max_words_by_first_token[first] = max(
            max_words_by_first_token.get(first, 0), len(key.split())
        )
    return max_words, max_words_by_first_token


def _normalize_alias_rows(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    for raw in rows or []:
        if not isinstance(raw, Mapping):
            continue
        status = str(raw.get("status") or "APPROVED").strip().upper()
        if status not in {"APPROVED", "AUTO_APPROVED"}:
            # Pending/rejected rows are review data, not runtime dictionary
            # state and therefore must not affect the provenance hash.
            continue
        alias_type = str(raw.get("alias_type") or "approved_alias").strip()
        normalized.append(
            {
                "surface_form": str(raw.get("surface_form") or "").strip(),
                "concept_id": str(raw.get("concept_id") or "").strip(),
                "concept_code": str(raw.get("concept_code") or "").strip(),
                "canonical_term": str(raw.get("canonical_term") or "").strip(),
                "alias_type": alias_type,
                "case_sensitive": (
                    _coerce_bool(raw["case_sensitive"])
                    if raw.get("case_sensitive") is not None
                    else alias_type == "abbreviation"
                ),
                "allow_accentless": _coerce_bool(raw.get("allow_accentless", False)),
                "status": status,
                "dictionary_version": str(raw.get("dictionary_version") or "").strip(),
            }
        )
    return tuple(
        sorted(
            normalized,
            key=lambda row: (
                row["surface_form"].casefold(),
                row["concept_id"].casefold(),
                row["concept_code"].casefold(),
                row["alias_type"].casefold(),
            ),
        )
    )


def _alias_rows_hash(rows: tuple[dict[str, Any], ...]) -> str:
    payload = json.dumps(list(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _apply_runtime_aliases(state: _MutableDictionary, rows: tuple[dict[str, Any], ...], dictionary_version: str) -> None:
    by_identity: dict[str, dict[str, Any]] = {}
    for values in state.term_candidates.values():
        for info in values:
            for value in (info.get("concept_id"), info.get("code")):
                identity = str(value or "").strip().casefold()
                if identity and identity not in by_identity and info.get("auto_matchable", True):
                    by_identity[identity] = info

    for row in rows:
        if row["status"] not in {"APPROVED", "AUTO_APPROVED"}:
            continue
        if row["dictionary_version"] and row["dictionary_version"] != dictionary_version:
            state.alias_rejected += 1
            continue
        alias = row["surface_form"]
        key = normalize_match_text(alias)
        identity = row["concept_id"].casefold()
        base = by_identity.get(identity)
        if not key or not identity or not base:
            state.alias_rejected += 1
            log.warning("Bỏ qua runtime alias không trỏ tới Concept hợp lệ: %s", alias)
            continue

        existing_ids = {
            str(value.get("concept_id") or value.get("code") or "").casefold()
            for value in state.term_candidates.get(key, [])
        }
        if identity in existing_ids:
            # The alias is already represented by the canonical/controlled form;
            # never replace that source entry merely because it came from MySQL.
            continue

        loaded = _add(
            state,
            alias,
            base.get("cat") or "",
            row["concept_code"] or base.get("code") or "",
            row["canonical_term"] or base.get("canonical_term") or alias,
            "approved_alias",
            alias_type=row["alias_type"],
            case_sensitive=row["case_sensitive"],
            allow_accentless=row["allow_accentless"],
            concept_id=base.get("concept_id") or row["concept_id"] or row["concept_code"],
            concept_id_source=base.get("concept_id_source") or "",
            display_code=base.get("display_code"),
            canonical_term=row["canonical_term"] or base.get("canonical_term") or alias,
            icd_chapter_code=base.get("icd_chapter_code"),
            icd_chapter_name=base.get("icd_chapter_name"),
            rule_score=base.get("rule_score"),
            score_type=base.get("score_type") or "heuristic",
            score_calibrated=base.get("score_calibrated", False),
        )
        if loaded:
            state.alias_loaded += 1


def _build_snapshot(alias_rows: tuple[dict[str, Any], ...], *, rebuild_artifacts: bool = False) -> DictSnapshot:
    if rebuild_artifacts:
        from core.dictionary_builder import build_all

        build_all()

    manifest_path = MANIFEST_PATH
    if not manifest_path.is_file():
        raise DictionaryValidationError(f"Không tìm thấy manifest: {manifest_path}")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise DictionaryValidationError("manifest_v1.json thiếu files object.")
    manifest_sha256 = _sha256(manifest_path)
    files = manifest["files"]
    for key in ("icd10", "yhct"):
        if key not in files or not isinstance(files[key], Mapping):
            raise DictionaryValidationError(f"Manifest thiếu artifact bắt buộc: {key}")

    artifact_payloads: dict[str, dict[str, Any]] = {}
    artifact_metadata: dict[str, dict[str, Any]] = {}
    for key, spec in files.items():
        if not isinstance(spec, Mapping):
            raise DictionaryValidationError(f"Manifest spec không hợp lệ cho {key}.")
        artifact_path, relative_name = _safe_relative_path(DICT_DIR, spec.get("path"), f"manifest.files.{key}.path")
        payload = _verified_payload(artifact_path, spec)
        expected_kinds = {
            "icd10": "icd10_vietnam",
            "yhct": "yhct_clinical_forms",
            "controlled_vocabulary": "controlled_vocabulary",
        }
        expected_kind = expected_kinds.get(key)
        if expected_kind and payload.get("kind") != expected_kind:
            raise DictionaryValidationError(
                f"Artifact {relative_name} có kind={payload.get('kind')!r}, "
                f"cần {expected_kind!r}."
            )
        artifact_payloads[key] = payload
        artifact_metadata[key] = {
            "file": relative_name,
            "sha256": _sha256(artifact_path),
            "count": len(payload.get("entries") or []),
        }

    versions = {artifact_payloads[key].get("dictionary_version") for key in ("icd10", "yhct")}
    manifest_version = str(manifest.get("dictionary_version") or "").strip()
    if not manifest_version or versions != {manifest_version}:
        raise DictionaryValidationError(
            f"Các artifact không cùng dictionary_version với manifest: {versions} != {manifest_version!r}"
        )

    icd_source = _verify_source_snapshot(artifact_payloads["icd10"], "icd10_v1.json")
    yhct_source = _verify_source_snapshot(artifact_payloads["yhct"], "yhct_v1.json")

    state = _MutableDictionary()
    _load_icd(state, artifact_payloads["icd10"]["entries"])
    _load_yhct(state, artifact_payloads["yhct"]["entries"])
    _load_controlled_grammar_variants(state, artifact_payloads.get("controlled_vocabulary"))
    _apply_runtime_aliases(state, alias_rows, manifest_version)
    _build_accentless_index(state)
    max_words, max_words_by_first_token = _build_indexes(state)
    if not state.term_candidates:
        raise DictionaryValidationError(
            "Dictionary hợp lệ về JSON nhưng không có term runtime nào; "
            "từ chối publish snapshot rỗng."
        )

    with _snapshot_lock:
        previous_generation = _snapshot.generation if _snapshot else 0
    generation = previous_generation + 1
    metadata = {
        "schema_version": manifest.get("schema_version"),
        "dictionary_version": manifest_version,
        "snapshot_generation": generation,
        "loaded_terms": len(state.term_candidates),
        "unambiguous_terms": len(state.term_dict),
        "dictionary_candidates": sum(len(values) for values in state.term_candidates.values()),
        "ambiguous_terms": len(state.ambiguous_term_keys),
        "controlled_grammar_variants": sum(
            1
            for values in state.term_candidates.values()
            for value in values
            if value.get("alias_type") == "grammar_variant"
        ),
        "controlled_headword_variants": sum(
            1
            for values in state.term_candidates.values()
            for value in values
            if value.get("alias_type") == "headword_variant"
        ),
        "accentless_terms": len(state.accentless_term_dict),
        "single_token_terms": sum(1 for key in state.term_dict if len(key.split()) == 1),
        "categories": dict(Counter(value["cat"] for value in state.term_dict.values())),
        "manifest_file": MANIFEST_PATH.name,
        "manifest_sha256": manifest_sha256,
        "artifacts": artifact_metadata,
        "controlled_vocabulary_file": artifact_metadata.get("controlled_vocabulary", {}).get("file"),
        "source_files": {"icd10": icd_source, "yhct": yhct_source},
        "scope": list(manifest.get("scope") or []),
        "alias_rows_sha256": _alias_rows_hash(alias_rows),
        "alias_rows_count": len(alias_rows),
        "explicit_aliases": state.alias_loaded,
        "rejected_aliases": state.alias_rejected,
        "invalid_category_count": len(state.invalid_categories),
        "invalid_category_samples": state.invalid_categories[:20],
        "rule_score_type": "heuristic",
        "score_calibrated": False,
    }
    return DictSnapshot(
        term_dict=_freeze(state.term_dict),
        term_candidates=_freeze(dict(state.term_candidates)),
        accentless_term_dict=_freeze(state.accentless_term_dict),
        accentless_term_candidates=_freeze(dict(state.accentless_term_candidates)),
        ambiguous_term_keys=frozenset(state.ambiguous_term_keys),
        max_words=max_words,
        max_words_by_first_token=_freeze(max_words_by_first_token),
        generation=generation,
        metadata=_freeze(metadata),
    )


def _publish_snapshot(snapshot: DictSnapshot, alias_rows: tuple[dict[str, Any], ...]) -> None:
    global _snapshot, _runtime_alias_rows
    with _snapshot_lock:
        _snapshot = snapshot
        _runtime_alias_rows = alias_rows
    _record_load_success()
    log.info(
        "Published dictionary snapshot generation=%s version=%s terms=%s",
        snapshot.generation,
        snapshot.metadata.get("dictionary_version"),
        snapshot.metadata.get("loaded_terms"),
    )


def _build_and_publish(alias_rows: tuple[dict[str, Any], ...], *, rebuild_artifacts: bool = False) -> DictSnapshot:
    with _build_lock:
        try:
            snapshot = _build_snapshot(alias_rows, rebuild_artifacts=rebuild_artifacts)
        except DictionaryError as exc:
            _record_load_failure(exc)
            raise
        except Exception as exc:
            wrapped = DictionaryValidationError(f"Không thể xây dictionary snapshot: {exc}")
            _record_load_failure(wrapped)
            raise wrapped from exc
        _publish_snapshot(snapshot, alias_rows)
        return snapshot


def load_ner_dictionary(force_rebuild: bool = False) -> Mapping[str, Mapping[str, Any]]:
    """Load and publish a complete snapshot, preserving the previous one on error."""
    with _snapshot_lock:
        current = _snapshot
        alias_rows = _runtime_alias_rows
    if current is not None and not force_rebuild:
        return current.term_dict
    snapshot = _build_and_publish(alias_rows, rebuild_artifacts=force_rebuild)
    return snapshot.term_dict


def reload_ner_dictionary(force_rebuild: bool = False) -> Mapping[str, Mapping[str, Any]]:
    with _snapshot_lock:
        alias_rows = _runtime_alias_rows
    snapshot = _build_and_publish(alias_rows, rebuild_artifacts=force_rebuild)
    return snapshot.term_dict


def load_runtime_aliases(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> int:
    """Rebuild one complete snapshot with the supplied runtime aliases."""
    normalized_rows = _normalize_alias_rows(rows)
    snapshot = _build_and_publish(normalized_rows)
    return int(snapshot.metadata.get("explicit_aliases") or 0)
