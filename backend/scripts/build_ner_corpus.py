"""Build a provisional ICD-10 NER + Concept-ID corpus from article abstracts.

The script deliberately keeps two representations for every match:
``text`` is the surface form found in the article, while ``canonical_term``
and ``concept_id`` come from the validated ICD-10 dictionary snapshot.  It
does not create concepts, use fuzzy suggestions, or call an AI model.

Examples:
    python backend/scripts/build_ner_corpus.py --limit 100
    python backend/scripts/build_ner_corpus.py --input-jsonl input.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config.env import load_backend_env  # noqa: E402

load_backend_env()

import mysql.connector  # noqa: E402

from core.ner_dict import get_dictionary_metadata, get_snapshot  # noqa: E402
from core.ner_engine import run_ner  # noqa: E402
from core.ner_evaluation import spans_to_bio  # noqa: E402
from core.ner_experiment import bio_text  # noqa: E402


LABEL_MAP = {
    "Bệnh Lý": "DISEASE",
    "Triệu Chứng": "SYMPTOM",
}
OUTPUT_DEFAULT = PROJECT_DIR / "text" / "corpus"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _db_config() -> dict[str, Any]:
    password = os.getenv("DB_PASSWORD", "")
    if not password:
        raise RuntimeError("Thiếu DB_PASSWORD; hoặc dùng --input-jsonl để chạy không cần MySQL.")
    return {
        "user": os.getenv("DB_USER", "root"),
        "password": password,
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "database": os.getenv("DB_NAME", "yhoc_corpus"),
        "port": int(os.getenv("DB_PORT", "3306")),
    }


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"JSONL lỗi tại dòng {line_number}: {path}") from exc
            if not isinstance(row, dict):
                raise RuntimeError(f"JSONL dòng {line_number} phải là object: {path}")
            yield row


def _load_articles(input_jsonl: Path | None, limit: int | None) -> list[dict[str, Any]]:
    if input_jsonl:
        rows = list(_read_jsonl(input_jsonl))
        if limit:
            rows = rows[:limit]
        return rows

    connection = cursor = None
    try:
        connection = mysql.connector.connect(**_db_config())
        cursor = connection.cursor(dictionary=True)
        query = (
            "SELECT id, title, abstract FROM articles "
            "WHERE abstract IS NOT NULL AND TRIM(abstract) <> '' ORDER BY id"
        )
        params: tuple[Any, ...] = ()
        if limit:
            query += " LIMIT %s"
            params = (limit,)
        cursor.execute(query, params)
        return list(cursor.fetchall())
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def _article_id(row: dict[str, Any], fallback: int) -> int:
    value = row.get("article_id", row.get("id", fallback))
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"article_id không hợp lệ: {value!r}") from exc


def _article_text(row: dict[str, Any]) -> str:
    for key in ("abstract", "text", "input_text"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _split_for_article(article_id: int) -> str:
    """Deterministic article-level split: 80% train, 10% dev, 10% test."""
    bucket = article_id % 10
    if bucket == 0:
        return "test"
    if bucket == 1:
        return "dev"
    return "train"


def _convert_entities(text: str, raw_entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    entities: list[dict[str, Any]] = []
    skipped = 0
    seen: set[tuple[int, int, str]] = set()
    for raw in raw_entities:
        concept_id = str(raw.get("concept_id") or "").strip()
        source = str(raw.get("concept_id_source") or "").strip().upper()
        category = str(raw.get("entity_type") or "").strip()
        label = LABEL_MAP.get(category)

        # The corpus requested here is ICD-10 anchored.  Runtime YHCT terms
        # remain available to the application but do not enter this dataset.
        if source != "ICD10" and not concept_id.startswith("ICD10:"):
            skipped += 1
            continue
        if not label:
            skipped += 1
            continue

        start = raw.get("start")
        end = raw.get("end")
        surface = str(raw.get("text") or "")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start or end > len(text):
            skipped += 1
            continue
        if text[start:end] != surface:
            skipped += 1
            continue

        identity = (start, end, concept_id)
        if identity in seen:
            continue
        seen.add(identity)
        entities.append({
            "text": surface,
            "surface_form": surface,
            "label": label,
            "start": start,
            "end": end,
            "concept_id": concept_id,
            "source_concept_id": concept_id,
            "concept_code": str(raw.get("icd_code") or "").strip(),
            "display_code": str(raw.get("display_code") or "").strip(),
            "canonical_term": str(raw.get("canonical_term") or "").strip(),
            "entity_type": category,
            "match_method": str(raw.get("matched_by") or "exact").strip(),
            "rule_score": raw.get("rule_score"),
            "score_type": str(raw.get("score_type") or "heuristic"),
            "score_calibrated": bool(raw.get("score_calibrated", False)),
            "status": "ACTIVE",
            "dictionary_source": "icd10_v1.json",
            "icd_chapter_code": str(raw.get("icd_chapter_code") or "").strip(),
            "icd_chapter_name": str(raw.get("icd_chapter_name") or "").strip(),
        })

    return sorted(entities, key=lambda item: (item["start"], item["end"], item["concept_id"])), skipped


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_conll(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    skipped_rows = 0
    for row in rows:
        # Use the evaluation tokenizer, which delegates to the Unicode-aware
        # rule-NER tokenizer and therefore preserves Vietnamese combining marks.
        tokens, issues = spans_to_bio(row["text"], row["entities"])
        if issues:
            skipped_rows += 1
            continue
        chunks.append(bio_text(tokens))
    path.write_text("".join(chunks), encoding="utf-8")
    return skipped_rows


def build_corpus(rows: list[dict[str, Any]], output_dir: Path) -> dict[str, Any]:
    snapshot = get_snapshot()
    dictionary = get_dictionary_metadata(snapshot)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict[str, Any]] = []
    skipped_entities = 0
    empty_articles = 0
    for index, source_row in enumerate(rows, 1):
        article_id = _article_id(source_row, index)
        text = _article_text(source_row)
        if not text:
            empty_articles += 1
            continue

        _, _, raw_entities, preprocessing_log = run_ner(
            text,
            enable_tone_restore=False,
            enable_noun_phrase=False,
            include_suggestions=False,
            allowed_sources={"ICD10"},
        )
        entities, skipped = _convert_entities(text, raw_entities)
        skipped_entities += skipped
        source_name = (
            "articles.abstract"
            if isinstance(source_row.get("abstract"), str)
            and source_row["abstract"].strip()
            else "input_jsonl"
        )
        all_rows.append({
            "article_id": article_id,
            "title": str(source_row.get("title") or ""),
            "text": text,
            "source": source_name,
            "source_checksum": _sha256_text(text),
            "entities": entities,
            "annotation_status": "PROVISIONAL",
            "dictionary": dictionary,
            "dictionary_version": dictionary.get("dictionary_version"),
            "manifest_sha256": dictionary.get("manifest_sha256"),
            "alias_rows_sha256": dictionary.get("alias_rows_sha256"),
            "snapshot_generation": preprocessing_log.get("dictionary", {}).get("snapshot_generation", snapshot.generation),
        })

    all_rows.sort(key=lambda row: row["article_id"])
    splits = {name: [row for row in all_rows if _split_for_article(row["article_id"]) == name] for name in ("train", "dev", "test")}

    _write_jsonl(output_dir / "controlled_vocabulary_preannotation.jsonl", all_rows)
    conll_skipped = {}
    for name, split_rows in splits.items():
        _write_jsonl(output_dir / f"{name}.jsonl", split_rows)
        conll_skipped[name] = _write_conll(output_dir / f"{name}.conll", split_rows)

    method_counts = Counter(
        entity["match_method"]
        for row in all_rows
        for entity in row["entities"]
    )
    label_counts = Counter(
        entity["label"]
        for row in all_rows
        for entity in row["entities"]
    )
    summary = {
        "schema_version": "1.0",
        "annotation_status": "PROVISIONAL",
        "source": "articles.abstract or input_jsonl",
        "dictionary_version": dictionary.get("dictionary_version"),
        "snapshot_generation": dictionary.get("snapshot_generation"),
        "manifest_sha256": dictionary.get("manifest_sha256"),
        "articles": len(all_rows),
        "entities": sum(len(row["entities"]) for row in all_rows),
        "empty_articles": empty_articles,
        "skipped_entities": skipped_entities,
        "conll_skipped_rows": conll_skipped,
        "splits": {name: {"articles": len(values), "entities": sum(len(row["entities"]) for row in values)} for name, values in splits.items()},
        "match_methods": dict(method_counts),
        "labels": dict(label_counts),
        "gold_required_before_training": True,
    }
    (output_dir / "corpus_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build provisional ICD-10 NER + Concept-ID corpus")
    parser.add_argument("--input-jsonl", type=Path, help="Optional JSONL with id/title/abstract; otherwise read MySQL articles")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit phải lớn hơn 0")
    if args.input_jsonl and not args.input_jsonl.exists():
        parser.error(f"Không tìm thấy input JSONL: {args.input_jsonl}")

    summary = build_corpus(_load_articles(args.input_jsonl, args.limit), args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
