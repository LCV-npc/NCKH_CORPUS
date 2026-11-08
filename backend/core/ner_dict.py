"""Versioned medical dictionary loader for the rule-only manual NER pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
DICT_DIR = BASE_DIR / "Tu Dien Y Hoc"
MANIFEST_PATH = DICT_DIR / "manifest_v1.json"
DATA_DIR = PROJECT_DIR.parent / "data"
ALLOWED_CATEGORIES = {"Bệnh Lý", "Triệu Chứng", "Đông Y / YHCT"}
COLOR_MAP = {
    "Bệnh Lý": "#86efac",
    "Triệu Chứng": "#93c5fd",
    "Đông Y / YHCT": "#fde047",
}

# Exact lookup and the deliberately smaller no-diacritic lookup.
term_dict: dict[str, dict[str, Any]] = {}
# All candidates are retained so a surface form can be marked uncertain when
# the versioned dictionaries contain more than one Concept ID for it.  The
# legacy ``term_dict`` remains the fast index for unambiguous terms only.
term_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
accentless_term_dict: dict[str, dict[str, Any]] = {}
accentless_term_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
ambiguous_term_keys: set[str] = set()
dictionary_metadata: dict[str, Any] = {}
_runtime_alias_keys: set[str] = set()
_loaded = False


def get_color(category: str) -> str:
    return COLOR_MAP.get(_normalize_category(category), "#e2e8f0")


def _normalize_category(value: str) -> str:
    raw = unicodedata.normalize("NFC", str(value or "")).strip().casefold()
    if raw in {"triệu chứng", "trieu chung", "symptom"}:
        return "Triệu Chứng"
    if "yhct" in raw or "đông y" in raw or "dong y" in raw:
        return "Đông Y / YHCT"
    return "Bệnh Lý"


def icd10_entity_type(code: str) -> Optional[str]:
    compact = str(code or "").strip().upper().replace(".", "")
    if compact.startswith("R"):
        return "Triệu Chứng"
    if compact.startswith(("V", "W", "X", "Y", "Z")):
        return None
    return "Bệnh Lý"


def normalize_match_text(text: str) -> str:
    """Normalize only for lookup; offsets always refer to the untouched source."""
    normalized = unicodedata.normalize("NFC", str(text or "")).casefold()
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    return " ".join(words)


def strip_diacritics(text: str) -> str:
    normalized = normalize_match_text(text).replace("đ", "d")
    return "".join(
        char for char in unicodedata.normalize("NFD", normalized)
        if unicodedata.category(char) != "Mn"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _add(
    term: str,
    category: str,
    code: str,
    label_vn: str = "",
    source: str = "",
    **metadata: Any,
) -> None:
    key = normalize_match_text(term)
    if len(key) < 2 or not re.search(r"[^\W\d_]", key, flags=re.UNICODE):
        return
    if metadata.get("active_for_ner") is False or metadata.get("ambiguous") is True:
        return
    category = _normalize_category(category)
    if category not in ALLOWED_CATEGORIES:
        return
    candidate = {
        "cat": category,
        # ``code`` is the compact ICD/YHCT code used by the legacy schema;
        # ``concept_id`` is the stable identifier from the versioned JSON
        # artifact and must be used when a term is linked across workflows.
        "concept_id": str(metadata.get("concept_id") or code or "").strip(),
        "concept_id_source": str(metadata.get("concept_id_source") or source or "").strip(),
        "code": str(code or "").strip(),
        "display_code": str(metadata.get("display_code") or code or "").strip(),
        "label_vn": str(label_vn or metadata.get("canonical_term") or term).strip(),
        "canonical_term": str(metadata.get("canonical_term") or label_vn or term).strip(),
        # Keep the original spelling so the NER engine can preserve meaningful
        # punctuation (for example: "Yêu thống)") in the highlighted span.
        "match_term": str(term).strip(),
        "source": source or str(metadata.get("source") or ""),
        "alias_type": str(metadata.get("alias_type") or "canonical"),
        "case_sensitive": bool(metadata.get("case_sensitive", False)),
        "allow_accentless": bool(metadata.get("allow_accentless", False)),
        "is_dagger": bool(metadata.get("is_dagger", False) or "†" in str(code)),
        "is_asterisk": bool(metadata.get("is_asterisk", False) or "*" in str(code)),
        "qualifier": str(metadata.get("qualifier") or ""),
    }
    # Deterministic priority: approved/runtime aliases, YHCT composite, then
    # ICD canonical.  Priority only replaces the same Concept ID; different
    # concepts are retained as an ambiguity set instead of being overwritten.
    priority = {"approved_alias": 4, "custom_alias": 3, "yhct": 2, "icd10": 1}
    identity = (candidate["concept_id"], candidate["cat"])
    values = term_candidates[key]
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
        if priority.get(existing.get("source", ""), 0) > priority.get(candidate.get("source", ""), 0):
            return
        values[existing_index] = candidate
    else:
        values.append(candidate)

    if len(values) == 1:
        term_dict[key] = values[0]
        ambiguous_term_keys.discard(key)
    else:
        term_dict.pop(key, None)
        ambiguous_term_keys.add(key)


def _verified_payload(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Không tìm thấy từ điển: {path}")
    actual_hash = _sha256(path)
    if actual_hash != expected.get("sha256"):
        raise ValueError(f"Sai SHA-256 cho {path.name}: {actual_hash}")
    payload = _read_json(path)
    entries = payload.get("entries", [])
    if len(entries) != int(expected.get("count", -1)):
        raise ValueError(f"Sai số mục cho {path.name}: {len(entries)}")
    return payload


def _verify_source_snapshot(payload: dict[str, Any], artifact_name: str) -> dict[str, str]:
    """Check that a versioned artifact still represents its source in ``data``.

    The application reads validated, versioned JSON artifacts for speed and
    deterministic matching.  Their ``source`` block is the link back to the
    original ICD-10 Excel/YHCT PDF supplied in the repository.  If someone
    updates a source file without rebuilding the artifact, continuing would
    silently label text with a stale dictionary, so fail loudly instead.
    """
    source = payload.get("source") or {}
    filename = str(source.get("file") or "").strip()
    expected_hash = str(source.get("sha256") or "").strip()
    if not filename or not expected_hash:
        return {}

    source_path = DATA_DIR / filename
    if not source_path.is_file():
        raise FileNotFoundError(
            f"Không tìm thấy nguồn {artifact_name} trong data/: {source_path}. "
            "Không thể xác minh từ điển đang dùng."
        )
    actual_hash = _sha256(source_path)
    if actual_hash != expected_hash:
        raise ValueError(
            f"Nguồn {source_path.name} đã thay đổi sau khi tạo {artifact_name}. "
            "Hãy chạy `python -m core.dictionary_builder` để tái tạo từ điển trước khi gán nhãn."
        )
    return {
        "path": str(source_path),
        "sha256": actual_hash,
    }


def _load_icd(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        if not entry.get("active_for_ner") or entry.get("ambiguous"):
            continue
        category = entry.get("category") or icd10_entity_type(entry.get("code", ""))
        if not category:
            continue
        common = dict(
            concept_id=entry.get("id"),
            concept_id_source="ICD10",
            display_code=entry.get("display_code"),
            qualifier=entry.get("qualifier"),
            is_dagger=bool(entry.get("qualifier") == "†"),
            is_asterisk=bool(entry.get("qualifier") == "*"),
            canonical_term=entry.get("canonical_term"),
        )
        _add(entry["canonical_term"], category, entry["code"], entry["canonical_term"], "icd10", **common)
        for alias in entry.get("aliases") or []:
            value = alias.get("term") if isinstance(alias, dict) else alias
            if value:
                _add(value, category, entry["code"], entry["canonical_term"], "icd10", alias_type="alias", **common)


def _load_yhct(entries: list[dict[str, Any]]) -> None:
    active_forms = [e for e in entries if e.get("record_type") == "clinical_form" and e.get("active_for_ner") and not e.get("ambiguous")]
    form_codes: dict[str, set[str]] = defaultdict(set)
    for entry in active_forms:
        form_codes[normalize_match_text(entry["canonical_term"])].add(entry["code"])

    for entry in entries:
        if not entry.get("active_for_ner") or entry.get("ambiguous"):
            continue
        if entry.get("record_type") == "base":
            common = dict(
                concept_id=entry.get("id"),
                concept_id_source="YHCT",
                display_code=entry.get("display_code"),
                canonical_term=entry.get("canonical_term"),
            )
            _add(entry["canonical_term"], "Đông Y / YHCT", entry["code"], entry["canonical_term"], "yhct", **common)
            _add(entry.get("yhct_term", ""), "Đông Y / YHCT", entry["code"], entry["canonical_term"], "yhct", alias_type="alias", **common)
            _add(
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
            )
            continue

        form = entry["canonical_term"]
        common = dict(
            concept_id=entry.get("id"),
            concept_id_source="YHCT",
            display_code=entry.get("display_code"),
            canonical_term=form,
            alias_type="clinical_form",
        )
        if len(form_codes[normalize_match_text(form)]) == 1:
            _add(form, "Đông Y / YHCT", entry["code"], form, "yhct", **common)
        for parent in (entry.get("parent_yhct_term"), entry.get("parent_yhhd_term")):
            if parent:
                _add(f"{parent} {form}", "Đông Y / YHCT", entry["code"], form, "yhct", **common)


def _build_accentless_index() -> None:
    accentless_term_dict.clear()
    accentless_term_candidates.clear()
    collisions: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for key, values in term_candidates.items():
        for info in values:
            collisions[strip_diacritics(key)].append((key, info))

    for no_tone, entries in collisions.items():
        values = entries
        if all(no_tone == key for key, _ in values):
            continue
        if any(len(key.split()) < 2 and not info.get("allow_accentless") for key, info in values):
            continue
        targets = {(v.get("concept_id") or v.get("code"), v.get("cat")) for _, v in values}
        if len(targets) != 1:
            continue
        chosen = dict(sorted(values, key=lambda item: (-len(item[0]), item[0]))[0][1])
        chosen["alias_type"] = "accentless"
        accentless_term_candidates[no_tone] = [chosen]
        accentless_term_dict[no_tone] = chosen

    # Keep the old function's collision behavior explicit: accentless terms
    # are only indexed when every spelling resolves to one Concept ID.
    return


def load_ner_dictionary(force_rebuild: bool = False) -> dict[str, dict[str, Any]]:
    global _loaded, dictionary_metadata
    if _loaded and not force_rebuild:
        return term_dict
    if force_rebuild:
        from core.dictionary_builder import build_all
        build_all()
    term_dict.clear()
    term_candidates.clear()
    accentless_term_dict.clear()
    accentless_term_candidates.clear()
    ambiguous_term_keys.clear()

    manifest = _read_json(MANIFEST_PATH)
    files = manifest["files"]
    icd = _verified_payload(DICT_DIR / files["icd10"]["path"], files["icd10"])
    yhct = _verified_payload(DICT_DIR / files["yhct"]["path"], files["yhct"])
    versions = {icd.get("dictionary_version"), yhct.get("dictionary_version")}
    if versions != {manifest.get("dictionary_version")}:
        raise ValueError(f"Các từ điển không cùng phiên bản: {versions}")

    icd_source = _verify_source_snapshot(icd, files["icd10"]["path"])
    yhct_source = _verify_source_snapshot(yhct, files["yhct"]["path"])

    _load_icd(icd["entries"])
    _load_yhct(yhct["entries"])
    _build_accentless_index()
    dictionary_metadata.clear()
    dictionary_metadata.update({
        "schema_version": manifest.get("schema_version"),
        "dictionary_version": manifest.get("dictionary_version"),
        "loaded_terms": len(term_candidates),
        "unambiguous_terms": len(term_dict),
        "dictionary_candidates": sum(len(values) for values in term_candidates.values()),
        "ambiguous_terms": len(ambiguous_term_keys),
        "accentless_terms": len(accentless_term_dict),
        "categories": dict(Counter(value["cat"] for value in term_dict.values())),
        "manifest": str(MANIFEST_PATH),
        "source_files": {
            "icd10": icd_source,
            "yhct": yhct_source,
        },
        "scope": list(manifest.get("scope") or []),
    })
    _loaded = True
    log.info("Loaded rule dictionary %s: %d exact, %d accentless", dictionary_metadata["dictionary_version"], len(term_dict), len(accentless_term_dict))
    return term_dict


def reload_ner_dictionary() -> dict[str, dict[str, Any]]:
    global _loaded
    _loaded = False
    return load_ner_dictionary()


def load_runtime_aliases(rows: list[dict[str, Any]]) -> int:
    """Overlay reviewer-approved aliases without mutating versioned source files.

    Aliases are deliberately loaded from a separate table. This keeps the
    checksum-protected ICD/YHCT artifacts immutable while allowing the review
    team to grow a controlled vocabulary over time.
    """
    global _runtime_alias_keys
    for key in _runtime_alias_keys:
        values = term_candidates.get(key, [])
        values = [value for value in values if value.get("source") != "approved_alias"]
        if values:
            term_candidates[key] = values
            if len(values) == 1:
                term_dict[key] = values[0]
                ambiguous_term_keys.discard(key)
            else:
                term_dict.pop(key, None)
                ambiguous_term_keys.add(key)
        else:
            term_candidates.pop(key, None)
            term_dict.pop(key, None)
            ambiguous_term_keys.discard(key)
        no_tone = strip_diacritics(key)
        accentless_term_dict.pop(no_tone, None)
        accentless_term_candidates.pop(no_tone, None)
    _runtime_alias_keys = set()

    by_identity: dict[str, dict[str, Any]] = {}
    for values in term_candidates.values():
        for info in values:
            for value in (info.get("concept_id"), info.get("code")):
                identity = str(value or "").strip().casefold()
                if identity and identity not in by_identity:
                    by_identity[identity] = info

    loaded = 0
    for row in rows or []:
        alias = str(row.get("surface_form") or "").strip()
        code = str(row.get("concept_code") or "").strip()
        concept_id = str(row.get("concept_id") or "").strip()
        key = normalize_match_text(alias)
        base = by_identity.get(concept_id.casefold()) or by_identity.get(code.casefold())
        if not key or not base or key in term_candidates:
            continue
        info = dict(base)
        info.update({
            "match_term": alias,
            "canonical_term": str(row.get("canonical_term") or base.get("canonical_term") or "").strip(),
            "label_vn": str(row.get("canonical_term") or base.get("label_vn") or alias).strip(),
            "alias_type": str(row.get("alias_type") or "approved_alias"),
            "source": "approved_alias",
            "concept_id": base.get("concept_id") or concept_id or code,
            "concept_id_source": base.get("concept_id_source") or "approved_alias",
        })
        term_candidates[key] = [info]
        term_dict[key] = info
        _runtime_alias_keys.add(key)
        loaded += 1

    _build_accentless_index()

    dictionary_metadata["approved_aliases"] = loaded
    log.info("Loaded %d reviewer-approved dictionary aliases", loaded)
    return loaded


load_ner_dictionary()
