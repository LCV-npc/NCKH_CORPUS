from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from config.env import load_backend_env

    load_backend_env()
except (ImportError, OSError):
    # The builder is also importable as a small standalone utility in tests.
    # In that case process environment variables remain the source of config.
    pass

BASE_DIR = Path(__file__).resolve().parent #Khai báo đường dẫn
BACKEND_DIR = BASE_DIR.parent
PROJECT_DIR = BACKEND_DIR.parent


def _configured_path(name: str, default: Path) -> Path:
    """Resolve configured paths relative to ``backend/`` unless absolute."""
    raw_value = os.getenv(name, "").strip()
    path = Path(raw_value).expanduser() if raw_value else default
    if not path.is_absolute():
        path = BACKEND_DIR / path
    return path.resolve()


DATA_DIR = _configured_path("NCKH_DATA_DIR", PROJECT_DIR / "data")
DICT_DIR = _configured_path("NCKH_DICTIONARY_DIR", BASE_DIR / "Tu Dien Y Hoc")
ICD_SOURCE = DATA_DIR / "phu-lu-c-1-danh-mu-c-icd-10-thay-the-dmdc-phie-n-ba-n-6.xlsx"
YHCT_SOURCE = DATA_DIR / "PhuLuc1.pdf"
ICD_OUTPUT = DICT_DIR / "icd10_v1.json"
YHCT_OUTPUT = DICT_DIR / "yhct_v1.json"
CONTROLLED_OUTPUT = DICT_DIR / "controlled_vocabulary_v1.json"
MANIFEST_OUTPUT = DICT_DIR / "manifest_v1.json"

SCHEMA_VERSION = "1.1"
DICTIONARY_VERSION = "2026.11.14-cv2"
CONTROLLED_VARIANT_RULES = (
    ("các ", "grammar_variant", "remove_cac_prefix", 0.98),
    ("những ", "grammar_variant", "remove_nhung_prefix", 0.98),
    # ICD-10 Vietnamese labels sometimes carry the catalog headword
    # ``Bệnh lý`` while abstracts use the shorter, still complete disease
    # phrase (for example ``tăng huyết áp``).  This remains a deterministic
    # controlled variant; it is not substring or semantic matching.
    ("bệnh lý ", "headword_variant", "remove_benh_ly_prefix", 0.97),
)
CONTROLLED_VARIANT_PREFIXES = tuple(rule[0] for rule in CONTROLLED_VARIANT_RULES)
CONTROLLED_VARIANT_MIN_TOKENS = 2


def _sha256(path: Path) -> str: #Hàm nhận vào đường dẫn file và trả về chuỗi SHA-256.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none"}:
        return ""
    return unicodedata.normalize("NFC", re.sub(r"\s+", " ", text))


def _normalize_match_text(value: Any) -> str:
    """Keep the generated controlled vocabulary in the same lookup form as NER."""
    normalized = unicodedata.normalize("NFC", _clean(value)).casefold()
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    return " ".join(words)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        temp_path = Path(stream.name)
    temp_path.replace(path)


def _qualifier(display_code: str) -> str:
    if "†" in display_code:
        return "dagger"
    if "*" in display_code:
        return "asterisk"
    return ""


def _category_for_icd(code: str) -> tuple[str, bool]:
    head = code[:1].upper()
    if head == "R":
        return "Triệu Chứng", True
    if head in {"V", "W", "X", "Y", "Z"}:
        return "Không sử dụng NER", False
    return "Bệnh Lý", True


def _safe_aliases(name: str) -> list[str]:
    """Create conservative aliases; never truncate a diagnosis at a comma."""
    aliases: list[str] = []
    without_parens = _clean(re.sub(r"\s*\([^)]*\)", " ", name))
    if without_parens and without_parens.casefold() != name.casefold() and len(without_parens) >= 5:
        aliases.append(without_parens)

    lower = name.casefold()
    for prefix in ("u ác của ", "u ác tính của ", "u ác "):
        if lower.startswith(prefix):
            organ = _clean(name[len(prefix) :])
            organ = _clean(re.sub(r"\s*\([^)]*\)", " ", organ))
            if len(organ) >= 3 and "," not in organ:
                aliases.extend((f"ung thư {organ}", f"ung thư của {organ}"))
            break

    seen: set[str] = set()
    return [a for a in aliases if not (a.casefold() in seen or seen.add(a.casefold()))]


def build_icd10(source: Path = ICD_SOURCE) -> dict[str, Any]:
    import pandas as pd

    required = {
        "STT CHƯƠNG",
        "MÃ CHƯƠNG",
        "TÊN CHƯƠNG",
        "MÃ NHÓM CHÍNH",
        "TÊN NHÓM CHÍNH",
        "MÃ BỆNH",
        "MÃ BỆNH KHÔNG DẤU",
        "DISEASE NAME",
        "TÊN BỆNH",
        "GHI CHÚ",
        "NGÀY CẬP NHẬT",
    }
    frame = pd.read_excel(source, sheet_name="ICD10", header=2, dtype=str).fillna("")
    frame.columns = [_clean(c) for c in frame.columns]
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"ICD10 source is missing columns: {missing}")

    entries: list[dict[str, Any]] = []
    ids: set[str] = set()
    for index, row in frame.iterrows():
        display_code = _clean(row["MÃ BỆNH"])
        code = _clean(row["MÃ BỆNH KHÔNG DẤU"])
        source_name = _clean(row["TÊN BỆNH"])
        if not display_code or not code or not source_name:
            raise ValueError(f"ICD10 row {index + 4} has an empty code/name")

        qualifier = _qualifier(display_code)
        entry_id = f"ICD10:{code}:{qualifier or 'base'}"
        # The official sheet contains one exact duplicate code row (J13).  Keep
        # both source rows auditable without letting identifiers collide.
        if entry_id in ids:
            entry_id = f"{entry_id}:row{index + 4}"
        ids.add(entry_id)
        category, active = _category_for_icd(code)
        entries.append(
            {
                "id": entry_id,
                "canonical_term": source_name,
                "source_term": source_name,
                "aliases": _safe_aliases(source_name),
                "category": category,
                "code": code,
                "display_code": display_code,
                "qualifier": qualifier,
                "active_for_ner": active,
                "ambiguous": False,
                "name_en": _clean(row["DISEASE NAME"]),
                "chapter": {
                    "ordinal": _clean(row["STT CHƯƠNG"]),
                    "code": _clean(row["MÃ CHƯƠNG"]),
                    "name": _clean(row["TÊN CHƯƠNG"]),
                },
                "main_group": {
                    "code": _clean(row["MÃ NHÓM CHÍNH"]),
                    "name": _clean(row["TÊN NHÓM CHÍNH"]),
                },
                "note": _clean(row["GHI CHÚ"]),
                "updated_at_source": _clean(row["NGÀY CẬP NHẬT"]),
                "source": {"document": source.name, "sheet": "ICD10", "row": int(index + 4)},
            }
        )

    qualifier_counts = Counter(e["qualifier"] or "base" for e in entries)
    if len(entries) != 12219:
        raise ValueError(f"Expected 12219 ICD10 rows, got {len(entries)}")
    if qualifier_counts["dagger"] != 82 or qualifier_counts["asterisk"] != 776:
        raise ValueError(f"Unexpected ICD10 qualifiers: {dict(qualifier_counts)}")

    return {
        "schema_version": SCHEMA_VERSION,
        "dictionary_version": DICTIONARY_VERSION,
        "kind": "icd10_vietnam",
        "source": {"file": source.name, "sha256": _sha256(source), "sheet": "ICD10"},
        "counts": {
            "source_rows": len(entries),
            "unique_compact_codes": len({e["code"] for e in entries}),
            "dagger": qualifier_counts["dagger"],
            "asterisk": qualifier_counts["asterisk"],
            "active_for_ner": sum(bool(e["active_for_ner"]) for e in entries),
        },
        "entries": entries,
    }


def _table_groups(source: Path) -> list[dict[str, Any]]:
    import pdfplumber

    groups: list[dict[str, Any]] = []
    with pdfplumber.open(source) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables():
                current: dict[str, Any] | None = None
                for raw_row in table:
                    row = [_clean(cell) for cell in (raw_row or [])]
                    match = re.search(r"650\d{4}", " | ".join(row))
                    if match:
                        if current:
                            groups.append(current)
                        current = {"common_code": match.group(), "page": page_number, "rows": [row]}
                    elif current:
                        current["rows"].append(row)
                if current:
                    groups.append(current)
    return [g for g in groups if 6500000 <= int(g["common_code"]) <= 6500227]


def _values_at(group: dict[str, Any], indexes: Iterable[int]) -> str:
    values: list[str] = []
    for row in group["rows"]:
        for index in indexes:
            if index < len(row) and row[index] and row[index] not in values:
                values.append(row[index])
    return _clean(" ".join(values))


def _first_match(group: dict[str, Any], pattern: str, indexes: Iterable[int]) -> str:
    regex = re.compile(pattern)
    for row in group["rows"]:
        for index in indexes:
            if index < len(row) and regex.fullmatch(row[index] or ""):
                return row[index]
    return ""


def _repair_cell_text(text: str) -> str:
    text = _clean(text)
    # Some PDF cells end with an opening parenthesis because the last glyph is clipped.
    text = re.sub(r"\s*\([^)]*$", "", text).strip()
    return text


def build_yhct(source: Path = YHCT_SOURCE) -> dict[str, Any]:
    groups = _table_groups(source)
    codes = [int(g["common_code"]) for g in groups]
    expected = list(range(6500000, 6500228))
    if codes != expected:
        missing = sorted(set(expected) - set(codes))
        duplicates = [code for code, count in Counter(codes).items() if count > 1]
        raise ValueError(f"YHCT common-code sequence invalid; missing={missing}, duplicates={duplicates}")

    entries: list[dict[str, Any]] = []
    parent: dict[str, Any] | None = None
    for group in groups:
        common_code = group["common_code"]
        form_code = _first_match(group, r"U[0-9]+(?:\.[0-9]+)+", range(20, 24))
        if form_code:
            if not parent:
                raise ValueError(f"YHCT form {common_code} has no parent")
            form_name = _repair_cell_text(_values_at(group, (18, 19)))
            if not form_name:
                raise ValueError(f"YHCT form {common_code} has no name")
            generic = form_name.casefold() == "thể khác"
            entries.append(
                {
                    "id": f"YHCT:{common_code}",
                    "record_type": "clinical_form",
                    "common_code": common_code,
                    "canonical_term": form_name,
                    "category": "Đông Y / YHCT",
                    "code": form_code,
                    "display_code": form_code,
                    "parent_id": parent["id"],
                    "parent_yhhd_term": parent["yhhd_term"],
                    "parent_yhct_term": parent["yhct_term"],
                    "active_for_ner": not generic,
                    "ambiguous": generic,
                    "source": {"document": source.name, "page": group["page"]},
                }
            )
            continue

        yhhd = _repair_cell_text(_values_at(group, (7, 8)))
        icd_code = _first_match(group, r"[A-TV-Z][0-9]{2}(?:\.[0-9]+)?", range(9, 13))
        yhct = _repair_cell_text(_values_at(group, (13, 14)))
        u_code = _first_match(group, r"U[0-9]+(?:\.[0-9]+)+", range(15, 18))
        if not all((yhhd, icd_code, yhct, u_code)):
            raise ValueError(
                f"YHCT base {common_code} incomplete: yhhd={yhhd!r}, icd={icd_code!r}, "
                f"yhct={yhct!r}, u={u_code!r}"
            )
        parent = {
            "id": f"YHCT:{common_code}",
            "record_type": "base",
            "common_code": common_code,
            "canonical_term": f"{yhhd} ({yhct})",
            "yhhd_term": yhhd,
            "yhct_term": yhct,
            "category": "Đông Y / YHCT",
            "icd10_code": icd_code,
            "code": u_code,
            "display_code": u_code,
            "active_for_ner": True,
            "ambiguous": False,
            "source": {"document": source.name, "page": group["page"]},
        }
        entries.append(parent)

    base_count = sum(e["record_type"] == "base" for e in entries)
    form_count = sum(e["record_type"] == "clinical_form" for e in entries)
    if len(entries) != 228 or base_count != 38 or form_count != 190:
        raise ValueError(f"Unexpected YHCT counts: all={len(entries)}, base={base_count}, forms={form_count}")

    return {
        "schema_version": SCHEMA_VERSION,
        "dictionary_version": DICTIONARY_VERSION,
        "kind": "yhct_clinical_forms",
        "source": {"file": source.name, "sha256": _sha256(source), "pages": 11},
        "counts": {"all": len(entries), "base": base_count, "clinical_forms": form_count},
        "entries": entries,
    }


def build_controlled_vocabulary(
    icd: dict[str, Any],
    yhct: dict[str, Any],
) -> dict[str, Any]:
    """Build deterministic surface forms from validated dictionary entries.

    The controlled vocabulary is a derived artifact: it may change the text
    surface that is matched, but it never introduces a new Concept ID.  This
    first version intentionally supports only omission of a leading Vietnamese
    determiner from a complete multi-word canonical term.
    """
    # The controlled vocabulary for the NER training corpus intentionally uses
    # ICD-10 as its only Concept authority.  YHCT remains available to the
    # runtime dictionary, but cannot introduce a surface-to-concept mapping in
    # this corpus artifact.
    del yhct
    sources = (("ICD10", "icd10_v1.json", icd),)
    variants: dict[tuple[str, str, str], dict[str, Any]] = {}

    for concept_source, dictionary_file, payload in sources:
        for entry in payload.get("entries", []):
            if not entry.get("active_for_ner") or entry.get("ambiguous"):
                continue
            canonical = _clean(entry.get("canonical_term"))
            canonical_key = _normalize_match_text(canonical)
            for prefix, match_method, rule_id, rule_score in CONTROLLED_VARIANT_RULES:
                if not canonical_key.startswith(prefix):
                    continue
                variant = canonical[len(prefix):].strip()
                normalized_variant = _normalize_match_text(variant)
                if len(normalized_variant.split()) < CONTROLLED_VARIANT_MIN_TOKENS:
                    continue

                concept_id = _clean(entry.get("id"))
                category = _clean(entry.get("category")) or "Bệnh Lý"
                identity = (normalized_variant, concept_id, category)
                variants.setdefault(
                    identity,
                    {
                        "surface_form": variant,
                        "normalized_form": normalized_variant,
                        "canonical_term": canonical,
                        "concept_id": concept_id,
                        "concept_id_source": concept_source,
                        "concept_code": _clean(entry.get("code")),
                        "code": _clean(entry.get("code")),
                        "display_code": _clean(entry.get("display_code")),
                        "entity_type": "DISEASE" if category == "Bệnh Lý" else "SYMPTOM",
                        "category": category,
                        "match_method": match_method,
                        "rule_id": rule_id,
                        "rule_score": rule_score,
                        "score_type": "heuristic",
                        "score_calibrated": False,
                        "case_sensitive": False,
                        "allow_accentless": False,
                        "active_for_ner": True,
                        "ambiguous": False,
                        "status": "ACTIVE",
                        "dictionary_source": dictionary_file,
                        "source_concept_id": concept_id,
                        "icd_chapter_code": _clean((entry.get("chapter") or {}).get("code")) if isinstance(entry.get("chapter"), dict) else "",
                        "icd_chapter_name": _clean((entry.get("chapter") or {}).get("name")) if isinstance(entry.get("chapter"), dict) else "",
                        "dictionary_file": dictionary_file,
                    },
                )

    entries = sorted(
        variants.values(),
        key=lambda item: (item["normalized_form"], item["concept_id"], item["category"]),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "dictionary_version": DICTIONARY_VERSION,
        "kind": "controlled_vocabulary",
        "source": {
            "dictionaries": ["icd10_v1.json"],
            "rule": "omit_leading_controlled_prefix",
        },
        "rules": {
            "allowed_prefixes": list(CONTROLLED_VARIANT_PREFIXES),
            "prefix_rules": [
                {
                    "prefix": prefix,
                    "match_method": match_method,
                    "rule_id": rule_id,
                    "rule_score": rule_score,
                }
                for prefix, match_method, rule_id, rule_score in CONTROLLED_VARIANT_RULES
            ],
            "minimum_variant_tokens": CONTROLLED_VARIANT_MIN_TOKENS,
            "ambiguous_entries": "excluded",
            "fuzzy_or_semantic_matching": "excluded",
            "concept_authority": "icd10_v1.json",
        },
        "counts": {"entries": len(entries)},
        "entries": entries,
    }


def build_all() -> dict[str, Any]:
    icd = build_icd10()
    yhct = build_yhct()
    controlled = build_controlled_vocabulary(icd, yhct)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    _atomic_json(ICD_OUTPUT, icd)
    _atomic_json(YHCT_OUTPUT, yhct)
    _atomic_json(CONTROLLED_OUTPUT, controlled)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dictionary_version": DICTIONARY_VERSION,
        "generated_at": generated_at,
        "scope": ["Bệnh Lý", "Triệu Chứng", "Đông Y / YHCT"],
        "files": {
            "icd10": {"path": ICD_OUTPUT.name, "sha256": _sha256(ICD_OUTPUT), "count": len(icd["entries"])},
            "yhct": {"path": YHCT_OUTPUT.name, "sha256": _sha256(YHCT_OUTPUT), "count": len(yhct["entries"])},
            "controlled_vocabulary": {
                "path": CONTROLLED_OUTPUT.name,
                "sha256": _sha256(CONTROLLED_OUTPUT),
                "count": len(controlled["entries"]),
            },
        },
    }
    _atomic_json(MANIFEST_OUTPUT, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build versioned medical dictionaries")
    parser.add_argument("--check", action="store_true", help="Build and validate artifacts (same atomic output path)")
    parser.parse_args()
    manifest = build_all()
    print(json.dumps(manifest, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
