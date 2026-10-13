#!/usr/bin/env python3
"""Run reproducible AI/dictionary/Gold-lexicon NER over sampled PDF abstracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from config.env import load_backend_env  # noqa: E402

load_backend_env()

from core.ai_label import DICTIONARY_TO_AI_CATEGORY, _dictionary_candidates  # noqa: E402
from core.language_validation import assess_metadata, decide_admission, select_pdf_text_for_language  # noqa: E402
from core.ner_dict import dictionary_metadata, normalize_match_text  # noqa: E402
from core.ner_engine import NEREngine  # noqa: E402
from core.ner_experiment import (  # noqa: E402
    GEMINI_MODEL,
    LABELS,
    LABEL_PRIORITY,
    align_surface_entities,
    append_jsonl,
    bio_text,
    checksum,
    entities_to_bio,
    gemini_extract,
    read_jsonl,
    tokenize,
)
from pdf_extractor import extract_from_pdf_path  # noqa: E402


PDF_ROOT = BACKEND / "Văn_Bản_Y_Tế_PDF" / "candidates" / "tapchinghiencuuyhoc.vn" / "2021"
GOLD_PATH = ROOT / "text" / "benchmark_combined" / "gold.jsonl"
OUTPUT_ROOT = ROOT / "text" / "pdf_2021_experiment"
SOURCE_RANK = {"MEDICAL_DICTIONARY": 0, "GOLD_LEXICON": 1, "AI": 2}
LABEL_FROM_DICTIONARY = {
    "Bệnh lý": "DISEASE",
    "Triệu chứng": "SYMPTOM",
}

PROMPT = """Bạn là hệ thống nhận dạng thực thể y khoa tiếng Việt.
Trích xuất đúng nguyên văn trong đoạn văn năm loại thực thể sau:
- DISEASE: bệnh, hội chứng hoặc tình trạng bệnh lý.
- SYMPTOM: dấu hiệu, triệu chứng hoặc biểu hiện lâm sàng.
- CAUSE: tác nhân, nguyên nhân hoặc yếu tố trực tiếp gây bệnh.
- DIAGNOSTIC_PROCEDURE: xét nghiệm, thăm dò hoặc phương pháp chẩn đoán.
- TREATMENT: thuốc, thủ thuật hoặc phương pháp điều trị.

Chỉ trả JSON dạng {"entities":[{"text":"...","label":"DISEASE"}]}.
Không thêm Markdown hoặc giải thích. Không suy diễn, không đổi cách viết và không trả chuỗi
không xuất hiện nguyên văn. Nếu cùng một thực thể xuất hiện nhiều lần, liệt kê từng lần theo
thứ tự xuất hiện. Không gán yếu tố nguy cơ chung thành CAUSE nếu văn bản không mô tả quan hệ gây bệnh.
"""

KEYWORD_RE = re.compile(r"^\s*(?:từ\s*kh[oó]a|keywords?)\s*[:：]", re.IGNORECASE)
ABSTRACT_HEADING_RE = re.compile(r"^\s*(?:t[oó]m\s*tắt|abstract|summary)\s*[:：]?\s*$", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def overwrite_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
    except Exception:
        return None


def abstract_from_layout(result: dict[str, Any]) -> tuple[str, str]:
    """Use page-layout blocks when a journal prints an unheaded abstract."""
    parsed = str(result.get("abstract") or "").strip()
    if parsed:
        return parsed, "SECTION_PARSER"
    blocks = list(result.get("blocks") or [])
    keyword_index = next((index for index, block in enumerate(blocks) if KEYWORD_RE.match(str(block.get("text") or ""))), None)
    if keyword_index is None or keyword_index == 0:
        return "", "NOT_FOUND"
    keyword = blocks[keyword_index]
    page = keyword.get("page")
    explicit = next(
        (
            index for index in range(keyword_index - 1, -1, -1)
            if blocks[index].get("page") == page and ABSTRACT_HEADING_RE.match(str(blocks[index].get("text") or ""))
        ),
        None,
    )
    if explicit is not None:
        chosen = blocks[explicit + 1:keyword_index]
        method = "LAYOUT_EXPLICIT_HEADING"
    else:
        chosen_reversed: list[dict[str, Any]] = []
        next_block = keyword
        for block in reversed(blocks[:keyword_index]):
            if block.get("page") != page:
                break
            bbox = block.get("bbox") or {}
            next_bbox = next_block.get("bbox") or {}
            gap = float(next_bbox.get("y0", 0)) - float(bbox.get("y1", 0))
            font_size = float(block.get("font_size") or 10)
            next_font = float(next_block.get("font_size") or font_size)
            if chosen_reversed and (gap > max(9.0, font_size * 0.95) or abs(font_size - next_font) > 1.5):
                break
            if bool(block.get("bold")) and chosen_reversed:
                break
            chosen_reversed.append(block)
            next_block = block
        chosen = list(reversed(chosen_reversed))
        method = "LAYOUT_BEFORE_KEYWORDS"
    text = " ".join(str(block.get("text") or "").strip() for block in chosen)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 120 or len(text) > 5000:
        return "", "LAYOUT_INVALID_LENGTH"
    return text, method


def token_keys(text: str) -> tuple[str, ...]:
    return tuple(normalize_match_text(token["text"]) for token in tokenize(text) if normalize_match_text(token["text"]))


def build_gold_lexicon() -> tuple[dict[tuple[str, ...], dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    engine = NEREngine()
    labels_by_term: dict[str, Counter[str]] = defaultdict(Counter)
    surfaces_by_term: dict[str, Counter[str]] = defaultdict(Counter)
    for row in read_jsonl(GOLD_PATH):
        for entity in row.get("entities", []):
            label = str(entity.get("label") or "")
            surface = str(entity.get("text") or "").strip()
            normalized = normalize_match_text(surface)
            if label in LABELS and normalized:
                labels_by_term[normalized][label] += 1
                surfaces_by_term[normalized][surface] += 1

    ambiguous: list[dict[str, Any]] = []
    lexicon: dict[tuple[str, ...], dict[str, Any]] = {}
    for normalized, label_counts in labels_by_term.items():
        surfaces = surfaces_by_term[normalized]
        if len(label_counts) > 1:
            ambiguous.append({
                "normalized_term": normalized,
                "representative_surface": surfaces.most_common(1)[0][0],
                "label_counts": dict(sorted(label_counts.items())),
                "surface_counts": dict(surfaces.most_common()),
                "total_occurrences": sum(label_counts.values()),
            })
            continue
        keys = token_keys(normalized)
        if not keys:
            continue
        label = next(iter(label_counts))
        candidate = {
            "normalized_term": normalized,
            "representative_surface": surfaces.most_common(1)[0][0],
            "label": label,
            "gold_frequency": sum(label_counts.values()),
        }
        existing = lexicon.get(keys)
        if existing and existing["label"] != label:
            ambiguous.append({
                "normalized_term": normalized,
                "representative_surface": candidate["representative_surface"],
                "label_counts": {existing["label"]: existing["gold_frequency"], label: candidate["gold_frequency"]},
                "surface_counts": dict(surfaces.most_common()),
                "total_occurrences": existing["gold_frequency"] + candidate["gold_frequency"],
            })
            lexicon.pop(keys, None)
        elif not existing or candidate["gold_frequency"] > existing["gold_frequency"]:
            lexicon[keys] = candidate

    ambiguous.sort(key=lambda row: (-row["total_occurrences"], row["normalized_term"]))
    stats = {
        "source": str(GOLD_PATH.relative_to(ROOT)),
        "source_checksum": file_sha256(GOLD_PATH),
        "unambiguous_terms": len(lexicon),
        "ambiguous_terms": len(ambiguous),
        "terms_by_label": dict(Counter(item["label"] for item in lexicon.values())),
    }
    return lexicon, ambiguous, stats


def gold_matches(text: str, lexicon: dict[tuple[str, ...], dict[str, Any]], engine: NEREngine) -> list[dict[str, Any]]:
    del engine
    tokens = [
        {**token, "key": normalize_match_text(token["text"])}
        for token in tokenize(text)
        if normalize_match_text(token["text"])
    ]
    by_first: dict[str, list[tuple[tuple[str, ...], dict[str, Any]]]] = defaultdict(list)
    for keys, info in lexicon.items():
        by_first[keys[0]].append((keys, info))
    for choices in by_first.values():
        choices.sort(key=lambda item: (-len(item[0]), item[1]["normalized_term"]))

    candidates: list[dict[str, Any]] = []
    for index, token in enumerate(tokens):
        for keys, info in by_first.get(token["key"], []):
            width = len(keys)
            chunk = tokens[index:index + width]
            if len(chunk) == width and tuple(part["key"] for part in chunk) == keys:
                start, end = chunk[0]["start"], chunk[-1]["end"]
                candidates.append({
                    "text": text[start:end], "label": info["label"], "start": start, "end": end,
                    "sources": ["GOLD_LEXICON"], "gold_frequency": info["gold_frequency"],
                    "normalized_term": info["normalized_term"],
                })
    return resolve_with_source_priority(candidates)


def medical_dictionary_matches(text: str) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for item in _dictionary_candidates(text):
        ai_category = DICTIONARY_TO_AI_CATEGORY.get(str(item.get("dictionary_type") or ""))
        label = LABEL_FROM_DICTIONARY.get(ai_category or "")
        if not label:
            continue
        start, end = int(item["start"]), int(item["end"])
        entities.append({
            "text": text[start:end], "label": label, "start": start, "end": end,
            "sources": ["MEDICAL_DICTIONARY"], "dictionary_type": item.get("dictionary_type", ""),
            "code": item.get("code", ""), "matched_by": item.get("matched_by", "dictionary"),
        })
    return entities


def merge_exact(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[int, int, str], dict[str, Any]] = {}
    for entity in entities:
        key = (int(entity["start"]), int(entity["end"]), str(entity["label"]))
        if key not in merged:
            merged[key] = dict(entity)
            merged[key]["sources"] = list(entity.get("sources", []))
        else:
            merged[key]["sources"] = sorted(set(merged[key]["sources"]) | set(entity.get("sources", [])))
            for field in ("dictionary_type", "code", "matched_by", "gold_frequency", "normalized_term"):
                if entity.get(field) not in (None, ""):
                    merged[key][field] = entity[field]
    return sorted(merged.values(), key=lambda item: (item["start"], item["end"], item["label"]))


def resolve_with_source_priority(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = merge_exact(entities)
    ordered = sorted(
        merged,
        key=lambda item: (
            min(SOURCE_RANK.get(source, 9) for source in item.get("sources", [""])),
            -(item["end"] - item["start"]), LABEL_PRIORITY[item["label"]], item["start"], item["end"],
        ),
    )
    chosen: list[dict[str, Any]] = []
    for item in ordered:
        if not any(item["start"] < old["end"] and old["start"] < item["end"] for old in chosen):
            chosen.append(item)
    return sorted(chosen, key=lambda item: (item["start"], item["end"], item["label"]))


def classify_union(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = merge_exact(entities)
    for item in merged:
        sources = set(item.get("sources", []))
        overlaps = [
            other for other in merged
            if other is not item and item["start"] < other["end"] and other["start"] < item["end"]
        ]
        label_conflict = any(other["label"] != item["label"] for other in overlaps)
        boundary_variant = any(
            other["label"] == item["label"] and (other["start"], other["end"]) != (item["start"], item["end"])
            for other in overlaps
        )
        if label_conflict:
            status = "LABEL_CONFLICT"
        elif boundary_variant:
            status = "BOUNDARY_VARIANT"
        elif len(sources) > 1:
            status = "MULTI_SOURCE_MATCH"
        elif "GOLD_LEXICON" in sources:
            status = "GOLD_LEXICON_ONLY"
        elif "MEDICAL_DICTIONARY" in sources:
            status = "MEDICAL_DICTIONARY_ONLY"
        else:
            status = "AI_ONLY"
        item["status"] = status
        item["has_label_conflict"] = label_conflict
        item["has_boundary_variant"] = boundary_variant
    return merged


def select_articles(count: int, seed: int, run_dir: Path) -> list[dict[str, Any]]:
    candidates = sorted(PDF_ROOT.glob("*.pdf"), key=lambda path: path.name.casefold())
    random.Random(seed).shuffle(candidates)
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for shuffled_index, path in enumerate(candidates, start=1):
        print(f"[sample {shuffled_index}/{len(candidates)}] {path.name}", flush=True)
        result = extract_from_pdf_path(str(path), source_hint=str(path))
        abstract, abstract_method = abstract_from_layout(result)
        title = str(result.get("title") or "").strip()
        reason = ""
        decision: dict[str, Any] | None = None
        if result.get("error"):
            reason = "PDF_EXTRACTION_ERROR"
        elif not abstract:
            reason = "EMPTY_ABSTRACT"
        else:
            metadata = assess_metadata(title, abstract)
            admission = decide_admission(metadata, select_pdf_text_for_language(result.get("body"), result.get("full_text")))
            decision = admission.as_dict()
            # The experiment consumes only the abstract. Full-PDF language is
            # retained as audit evidence but does not reject a Vietnamese abstract.
            if metadata.language != "vi":
                reason = "NOT_VIETNAMESE"
        common = {
            "shuffled_index": shuffled_index,
            "pdf_path": str(path.relative_to(ROOT)),
            "pdf_name": path.name,
            "pdf_sha256": file_sha256(path),
            "title": title,
            "abstract_method": abstract_method,
            "language_decision": decision,
        }
        if reason:
            rejected.append({**common, "reason": reason, "error": result.get("error", "")})
            print(f"  rejected: {reason}", flush=True)
            continue
        article_id = f"pdf-{len(selected) + 1:03d}"
        selected.append({
            **common, "article_id": article_id, "selection_order": len(selected) + 1,
            "abstract": abstract, "abstract_sha256": checksum(abstract), "abstract_length": len(abstract),
        })
        print(f"  selected as {article_id} ({len(abstract)} chars)", flush=True)
        if len(selected) >= count:
            break
    if len(selected) != count:
        raise RuntimeError(f"Chỉ chọn được {len(selected)}/{count} abstract hợp lệ")
    overwrite_jsonl(run_dir / "input" / "articles.jsonl", selected)
    overwrite_jsonl(run_dir / "input" / "rejected.jsonl", rejected)
    return selected


def retry_seconds(exc: Exception, attempt: int) -> float:
    matches = re.findall(r"retry(?:Delay)?[^0-9]*(\d+(?:\.\d+)?)", str(exc), flags=re.IGNORECASE)
    return max([float(value) for value in matches] + [min(60.0, 5.0 * (2 ** attempt))]) + 1.0


def call_ai(text: str, retries: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, float]:
    started = time.perf_counter()
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = gemini_extract(text, PROMPT)
            aligned, issues = align_surface_entities(text, raw)
            return aligned, issues, attempt + 1, round(time.perf_counter() - started, 3)
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            wait = retry_seconds(exc, attempt)
            print(f"  retry {attempt + 1}/{retries} after {wait:.1f}s: {exc}", flush=True)
            time.sleep(wait)
    raise RuntimeError(str(last_error))


def annotated(entity: dict[str, Any], article: dict[str, Any], configuration: str) -> dict[str, Any]:
    return {
        "article_id": article["article_id"], "pdf_name": article["pdf_name"],
        "abstract_sha256": article["abstract_sha256"], "configuration": configuration,
        **entity,
    }


def process_article(
    article: dict[str, Any], lexicon: dict[tuple[str, ...], dict[str, Any]], engine: NEREngine,
    retries: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = article["abstract"]
    issues: list[dict[str, Any]] = []
    ai, ai_issues, attempts, latency = call_ai(text, retries)
    for item in ai:
        item["sources"] = ["AI"]
    issues.extend({"article_id": article["article_id"], "stage": "AI_ALIGNMENT", **item} for item in ai_issues)
    medical = medical_dictionary_matches(text)
    ai_dictionary_raw = merge_exact(ai + medical)
    ai_dictionary_flat = resolve_with_source_priority(ai_dictionary_raw)
    gold_flat = gold_matches(text, lexicon, engine)
    union_raw = classify_union(ai_dictionary_raw + gold_flat)
    combined_flat = resolve_with_source_priority(union_raw)
    for item in combined_flat:
        match = next(row for row in union_raw if (row["start"], row["end"], row["label"]) == (item["start"], item["end"], item["label"]))
        item["status"] = match["status"]
    return {
        "article_id": article["article_id"], "pdf_name": article["pdf_name"], "title": article["title"],
        "abstract": text, "abstract_sha256": article["abstract_sha256"],
        "model": GEMINI_MODEL, "attempts": attempts, "latency_seconds": latency,
        "ai_entities": ai, "medical_dictionary_entities": medical,
        "ai_dictionary_raw": ai_dictionary_raw, "ai_dictionary_entities": ai_dictionary_flat,
        "gold_lexicon_entities": gold_flat, "combined_raw": union_raw, "combined_entities": combined_flat,
    }, issues


def rebuild_local_result(row: dict[str, Any], article: dict[str, Any], lexicon: dict[tuple[str, ...], dict[str, Any]], engine: NEREngine) -> dict[str, Any]:
    text = article["abstract"]
    ai = row.get("ai_entities", [])
    for item in ai:
        item["sources"] = ["AI"]
    medical = medical_dictionary_matches(text)
    ai_dictionary_raw = merge_exact(ai + medical)
    gold_flat = gold_matches(text, lexicon, engine)
    union_raw = classify_union(ai_dictionary_raw + gold_flat)
    combined_flat = resolve_with_source_priority(union_raw)
    for item in combined_flat:
        match = next(value for value in union_raw if (value["start"], value["end"], value["label"]) == (item["start"], item["end"], item["label"]))
        item["status"] = match["status"]
    return {
        **row,
        "medical_dictionary_entities": medical,
        "ai_dictionary_raw": ai_dictionary_raw,
        "ai_dictionary_entities": resolve_with_source_priority(ai_dictionary_raw),
        "gold_lexicon_entities": gold_flat,
        "combined_raw": union_raw,
        "combined_entities": combined_flat,
    }


def write_conll(run_dir: Path, articles: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    variants = {
        "ai_dictionary": "ai_dictionary_entities",
        "gold_lexicon": "gold_lexicon_entities",
        "combined": "combined_entities",
    }
    for directory, field in variants.items():
        chunks: list[str] = []
        for article in articles:
            row = results.get(article["article_id"])
            if not row:
                continue
            tokens, bio_issues = entities_to_bio(article["abstract"], row[field])
            chunks.append(bio_text(tokens))
            issues.extend({"article_id": article["article_id"], "stage": f"BIO_{directory.upper()}", **item} for item in bio_issues)
        target = run_dir / directory / "test.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(chunks), encoding="utf-8", newline="\n")
    return issues


def counts_for(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    entity_counts = Counter(entity["label"] for row in rows for entity in row.get(field, []))
    article_counts = Counter()
    for row in rows:
        for label in {entity["label"] for entity in row.get(field, [])}:
            article_counts[label] += 1
    return {
        "entities_by_label": {label: entity_counts[label] for label in LABELS},
        "total_entities": sum(entity_counts.values()),
        "articles_with_entities_by_label": {label: article_counts[label] for label in LABELS},
        "articles_with_any_entity": sum(bool(row.get(field)) for row in rows),
    }


def materialize(run_dir: Path, articles: list[dict[str, Any]], rows: list[dict[str, Any]], issues: list[dict[str, Any]], ambiguous: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    by_id = {row["article_id"]: row for row in rows}
    complete = [by_id[item["article_id"]] for item in articles if item["article_id"] in by_id]
    details: list[dict[str, Any]] = []
    combined_rows: list[dict[str, Any]] = []
    new_candidates: list[dict[str, Any]] = []
    for article in articles:
        row = by_id.get(article["article_id"])
        if not row:
            continue
        for field, configuration in (
            ("ai_dictionary_entities", "AI + CURRENT DICTIONARY"),
            ("gold_lexicon_entities", "GOLD LEXICON"),
        ):
            details.extend(annotated(entity, article, configuration) for entity in row[field])
        ai_keys = {(e["start"], e["end"], e["label"]) for e in row["ai_dictionary_raw"]}
        for entity in row["combined_raw"]:
            combined_rows.append(annotated({**entity, "result_kind": "RAW_UNION"}, article, "COMBINED RAW"))
        for entity in row["combined_entities"]:
            combined_rows.append(annotated({**entity, "result_kind": "FLAT_FINAL"}, article, "COMBINED FLAT"))
        for entity in row["gold_lexicon_entities"]:
            key = (entity["start"], entity["end"], entity["label"])
            if key not in ai_keys:
                overlaps = [e for e in row["ai_dictionary_raw"] if entity["start"] < e["end"] and e["start"] < entity["end"]]
                status = "GOLD_LEXICON_ONLY"
                if any(e["label"] != entity["label"] for e in overlaps):
                    status = "LABEL_CONFLICT"
                elif overlaps:
                    status = "BOUNDARY_VARIANT"
                new_candidates.append(annotated({**entity, "candidate_status": status}, article, "NEW GOLD CANDIDATE"))

    overwrite_jsonl(run_dir / "entity_details.jsonl", details)
    overwrite_jsonl(run_dir / "combined_results.jsonl", combined_rows)
    overwrite_jsonl(run_dir / "new_gold_candidates.jsonl", new_candidates)
    overwrite_jsonl(run_dir / "ambiguous_gold_terms.jsonl", ambiguous)
    overwrite_jsonl(run_dir / "issues.jsonl", issues)
    overwrite_jsonl(run_dir / "sample_manifest.jsonl", articles)
    bio_issues = write_conll(run_dir, articles, by_id)
    if bio_issues:
        issues.extend(bio_issues)
        overwrite_jsonl(run_dir / "issues.jsonl", issues)
    summary = {
        "run_id": run_dir.name,
        "selected_articles": len(articles),
        "completed_articles": len(complete),
        "failed_articles": len(articles) - len(complete),
        "ai_dictionary": counts_for(complete, "ai_dictionary_entities"),
        "gold_lexicon": counts_for(complete, "gold_lexicon_entities"),
        "combined": counts_for(complete, "combined_entities"),
        "new_gold_candidates": {
            "entities_by_label": {label: sum(item["label"] == label for item in new_candidates) for label in LABELS},
            "total_entities": len(new_candidates),
        },
        "ambiguous_gold_terms_excluded": len(ambiguous),
        "issue_count": len(issues),
    }
    atomic_json(run_dir / "summary.json", summary)
    manifest["updated_at"] = now_iso()
    manifest["status"] = "completed" if len(complete) == len(articles) else "completed_with_errors"
    manifest["summary"] = summary
    atomic_json(run_dir / "manifest.json", manifest)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260924)
    parser.add_argument("--delay", type=float, default=4.5)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--resume", help="Existing run id")
    parser.add_argument("--sample-only", action="store_true", help="Create the deterministic input snapshot without API calls")
    parser.add_argument("--max-articles", type=int, help="Process only the first N sampled articles (pilot/resume testing)")
    parser.add_argument("--rebuild-local", action="store_true", help="Rebuild dictionary/Gold/combined outputs without API calls")
    args = parser.parse_args()

    run_id = args.resume or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    run_dir = OUTPUT_ROOT / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    lexicon, ambiguous, lexicon_stats = build_gold_lexicon()
    engine = NEREngine()

    article_path = run_dir / "input" / "articles.jsonl"
    articles = read_jsonl(article_path) if args.resume and article_path.exists() else select_articles(args.count, args.seed, run_dir)
    if len(articles) != args.count:
        raise RuntimeError(f"Run có {len(articles)} bài, khác --count={args.count}")
    prompt_path = run_dir / "config" / "prompt.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(PROMPT, encoding="utf-8", newline="\n")
    atomic_json(run_dir / "config" / "gold_lexicon.json", lexicon_stats)
    atomic_json(run_dir / "config" / "medical_dictionary.json", dictionary_metadata)

    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {
        "run_id": run_id, "created_at": now_iso(), "status": "running", "seed": args.seed,
        "requested_count": args.count, "pdf_root": str(PDF_ROOT.relative_to(ROOT)),
        "candidate_pdf_count": len(list(PDF_ROOT.glob("*.pdf"))), "ignored_extensionless_count": sum(path.suffix == "" for path in PDF_ROOT.iterdir() if path.is_file()),
        "model": GEMINI_MODEL, "temperature": 0, "prompt_sha256": checksum(PROMPT),
        "gold_lexicon": lexicon_stats, "medical_dictionary": dictionary_metadata,
        "git_commit": git_commit(), "delay_seconds": args.delay, "max_retries": args.retries,
    }
    manifest["status"] = "running"
    manifest["updated_at"] = now_iso()
    atomic_json(manifest_path, manifest)

    if args.sample_only:
        manifest["status"] = "sampled"
        manifest["updated_at"] = now_iso()
        atomic_json(manifest_path, manifest)
        print(json.dumps({"run_id": run_id, "run_dir": str(run_dir), "selected_articles": len(articles)}, ensure_ascii=False), flush=True)
        return 0

    prediction_path = run_dir / "predictions.jsonl"
    issue_path = run_dir / "runtime_issues.jsonl"
    existing_rows = read_jsonl(prediction_path)
    if args.rebuild_local:
        article_by_id = {article["article_id"]: article for article in articles}
        existing_rows = [rebuild_local_result(row, article_by_id[row["article_id"]], lexicon, engine) for row in existing_rows]
        overwrite_jsonl(prediction_path, existing_rows)
        issues = read_jsonl(issue_path)
        summary = materialize(run_dir, articles, existing_rows, issues, ambiguous, manifest)
        print(json.dumps({"run_dir": str(run_dir), "summary": summary}, ensure_ascii=False, indent=2), flush=True)
        return 0 if summary["completed_articles"] == args.count else 2
    completed = {row["article_id"] for row in existing_rows}
    issues = read_jsonl(issue_path)
    processing_articles = articles[:args.max_articles] if args.max_articles else articles
    for index, article in enumerate(processing_articles, start=1):
        if article["article_id"] in completed:
            print(f"[{index}/{len(articles)}] skip {article['article_id']} (completed)", flush=True)
            continue
        print(f"[{index}/{len(articles)}] {article['article_id']} {article['pdf_name']}", flush=True)
        try:
            row, row_issues = process_article(article, lexicon, engine, args.retries)
            append_jsonl(prediction_path, row)
            for issue in row_issues:
                append_jsonl(issue_path, issue)
            existing_rows.append(row)
            issues.extend(row_issues)
            completed.add(article["article_id"])
            print(
                f"  AI+dict={len(row['ai_dictionary_entities'])} gold={len(row['gold_lexicon_entities'])} combined={len(row['combined_entities'])} latency={row['latency_seconds']}s",
                flush=True,
            )
        except Exception as exc:
            issue = {"article_id": article["article_id"], "type": "API_FAILURE", "error": str(exc), "at": now_iso()}
            append_jsonl(issue_path, issue)
            issues.append(issue)
            print(f"  FAILED: {exc}", flush=True)
        if index < len(processing_articles):
            time.sleep(args.delay)

    summary = materialize(run_dir, articles, existing_rows, issues, ambiguous, manifest)
    print(json.dumps({"run_dir": str(run_dir), "summary": summary}, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["completed_articles"] == args.count else 2


if __name__ == "__main__":
    raise SystemExit(main())
