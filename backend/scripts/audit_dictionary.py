"""Export reviewable reports for one-token and ambiguous dictionary terms."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config.env import load_backend_env

load_backend_env()

from core.ner_dict import DICT_DIR, get_snapshot


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _invalid_category_rows() -> list[dict[str, object]]:
    allowed = {"Bệnh Lý", "Triệu Chứng", "Đông Y / YHCT", "Không sử dụng NER"}
    manifest = json.loads((DICT_DIR / "manifest_v1.json").read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for source_key in ("icd10", "yhct"):
        spec = manifest["files"][source_key]
        payload = json.loads((DICT_DIR / spec["path"]).read_text(encoding="utf-8"))
        for index, entry in enumerate(payload.get("entries") or []):
            category = str(entry.get("category") or "").strip()
            if entry.get("active_for_ner") and category and category not in allowed:
                rows.append(
                    {
                        "source": source_key,
                        "entry_index": index,
                        "concept_id": entry.get("id", ""),
                        "code": entry.get("code", ""),
                        "canonical_term": entry.get("canonical_term", ""),
                        "category": category,
                    }
                )
    return rows


def export_reports(output_dir: Path) -> dict[str, int]:
    snapshot = get_snapshot()

    single_token_rows = []
    for key, info in sorted(snapshot.term_dict.items()):
        if len(key.split()) != 1:
            continue
        single_token_rows.append(
            {
                "surface_form": info.get("match_term", key),
                "normalized_form": key,
                "concept_id": info.get("concept_id", ""),
                "concept_code": info.get("code", ""),
                "concept_id_source": info.get("concept_id_source", ""),
                "category": info.get("cat", ""),
                "icd_chapter_code": info.get("icd_chapter_code", ""),
                "icd_chapter_name": info.get("icd_chapter_name", ""),
                "source": info.get("source", ""),
            }
        )

    ambiguous_rows = []
    for key in sorted(snapshot.ambiguous_term_keys):
        values = snapshot.term_candidates.get(key, ())
        ambiguous_rows.append(
            {
                "surface_form": values[0].get("match_term", key) if values else key,
                "normalized_form": key,
                "candidate_concept_ids": "|".join(
                    sorted({str(value.get("concept_id") or "") for value in values})
                ),
                "candidate_sources": "|".join(
                    sorted({str(value.get("concept_id_source") or "") for value in values})
                ),
                "concept_codes": "|".join(
                    sorted({str(value.get("code") or "") for value in values})
                ),
                "icd_chapter_codes": "|".join(
                    sorted({str(value.get("icd_chapter_code") or "") for value in values if value.get("icd_chapter_code")})
                ),
                "candidate_count": len(values),
            }
        )

    invalid_rows = _invalid_category_rows()
    _write_csv(
        output_dir / "single_token_terms.csv",
        list(single_token_rows[0]) if single_token_rows else [
            "surface_form", "normalized_form", "concept_id", "concept_code",
            "concept_id_source", "category", "icd_chapter_code",
            "icd_chapter_name", "source",
        ],
        single_token_rows,
    )
    _write_csv(
        output_dir / "ambiguous_terms.csv",
        [
            "surface_form", "normalized_form", "candidate_concept_ids",
            "candidate_sources", "concept_codes", "icd_chapter_codes",
            "candidate_count",
        ],
        ambiguous_rows,
    )
    _write_csv(
        output_dir / "invalid_categories.csv",
        ["source", "entry_index", "concept_id", "code", "canonical_term", "category"],
        invalid_rows,
    )
    return {
        "single_token_terms": len(single_token_rows),
        "ambiguous_terms": len(ambiguous_rows),
        "invalid_categories": len(invalid_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=BACKEND_DIR / "reports" / "dictionary",
    )
    args = parser.parse_args()
    counts = export_reports(args.output_dir)
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
