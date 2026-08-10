"""Build the versioned rule-and-dictionary resources used by manual NER.

The builder is intentionally separate from application startup.  Source files are
never modified; validated JSON artifacts are written atomically next to the legacy
dictionaries and selected through ``manifest_v1.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR.parent
PROJECT_DIR = BACKEND_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
DICT_DIR = BASE_DIR / "Tu Dien Y Hoc"
CUSTOM_DIR = BACKEND_DIR / "Kho Ngữ Liệu Y Học Tiếng Việt"

ICD_SOURCE = DATA_DIR / "phu-lu-c-1-danh-mu-c-icd-10-thay-the-dmdc-phie-n-ba-n-6.xlsx"
YHCT_SOURCE = DATA_DIR / "PhuLuc1.pdf"
LEGACY_CUSTOM = CUSTOM_DIR / "Từ_Điển.json"

ICD_OUTPUT = DICT_DIR / "icd10_v1.json"
YHCT_OUTPUT = DICT_DIR / "yhct_v1.json"
CUSTOM_OUTPUT = CUSTOM_DIR / "Từ_Điển_v1.json"
MANIFEST_OUTPUT = DICT_DIR / "manifest_v1.json"

SCHEMA_VERSION = "1.0"
DICTIONARY_VERSION = "2026.08.09-v1"


def _sha256(path: Path) -> str:
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


def build_custom_aliases(legacy_path: Path = LEGACY_CUSTOM) -> dict[str, Any]:
    from core.custom_entities import inject_custom_entities

    collected: dict[tuple[str, str], dict[str, Any]] = {}

    def collect(term: str, category: str, code: str, label: str = "", source: str = "") -> None:
        clean_term = _clean(term)
        if not clean_term or len(clean_term) < 3:
            return
        normalized_category = "Triệu Chứng" if category.casefold() == "triệu chứng" else category
        if normalized_category not in {"Bệnh Lý", "Triệu Chứng", "Đông Y / YHCT"}:
            normalized_category = "Bệnh Lý"
        key = (clean_term.casefold(), code.strip().upper())
        collected[key] = {
            "term": clean_term,
            "canonical_term": _clean(label) or clean_term,
            "type": normalized_category,
            "code": code.strip().upper(),
            "active_for_ner": True,
            "ambiguous": False,
            "case_sensitive": clean_term.isupper() and len(clean_term) <= 6,
            "source": source or "legacy_custom_entities",
        }

    inject_custom_entities(collect)
    if legacy_path.exists():
        legacy = json.loads(legacy_path.read_text(encoding="utf-8-sig"))
        for item in legacy:
            collect(item.get("term", ""), item.get("type", "Bệnh Lý"), item.get("code", ""), source="legacy_json")

    ambiguous_terms = {"giang mai", "u ác tính", "tổn thương"}
    entries = sorted(collected.values(), key=lambda item: (item["term"].casefold(), item["code"]))
    for item in entries:
        if item["term"].casefold() in ambiguous_terms:
            item["active_for_ner"] = False
            item["ambiguous"] = True

    return {
        "schema_version": SCHEMA_VERSION,
        "dictionary_version": DICTIONARY_VERSION,
        "kind": "custom_aliases",
        "source": {
            "legacy_file": legacy_path.name,
            "legacy_sha256": _sha256(legacy_path) if legacy_path.exists() else "",
        },
        "counts": {
            "all": len(entries),
            "active": sum(bool(e["active_for_ner"]) for e in entries),
            "ambiguous": sum(bool(e["ambiguous"]) for e in entries),
        },
        "entries": entries,
    }


def build_all() -> dict[str, Any]:
    icd = build_icd10()
    yhct = build_yhct()
    custom = build_custom_aliases()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    _atomic_json(ICD_OUTPUT, icd)
    _atomic_json(YHCT_OUTPUT, yhct)
    _atomic_json(CUSTOM_OUTPUT, custom)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dictionary_version": DICTIONARY_VERSION,
        "generated_at": generated_at,
        "scope": ["Bệnh Lý", "Triệu Chứng", "Đông Y / YHCT"],
        "files": {
            "icd10": {"path": ICD_OUTPUT.name, "sha256": _sha256(ICD_OUTPUT), "count": len(icd["entries"])},
            "yhct": {"path": YHCT_OUTPUT.name, "sha256": _sha256(YHCT_OUTPUT), "count": len(yhct["entries"])},
            "custom": {
                "path": str(CUSTOM_OUTPUT.relative_to(BACKEND_DIR)).replace("\\", "/"),
                "sha256": _sha256(CUSTOM_OUTPUT),
                "count": len(custom["entries"]),
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
