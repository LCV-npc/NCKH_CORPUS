#!/usr/bin/env python3
"""Build a reproducible merged VietBioNER + ViMedNer silver benchmark."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from core.ner_experiment import LABELS, bio_text, entities_to_bio  # noqa: E402


SOURCE = ROOT / "ner_finetuning" / "processed" / "combined_provisional"
OUTPUT = ROOT / "text" / "benchmark_combined"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def select_pilot(rows: list[dict], per_source: int = 50) -> list[dict]:
    selected: list[dict] = []
    for source in ("vietbioner", "vimedner"):
        candidates = [row for row in rows if row["source"] == source]
        chosen: list[dict] = []
        covered: set[str] = set()
        for row in candidates:
            row_labels = {item["label"] for item in row["entities"]}
            if row_labels - covered:
                chosen.append(row)
                covered.update(row_labels)
            if len(chosen) >= per_source:
                break
        chosen_ids = {row["id"] for row in chosen}
        chosen.extend(row for row in candidates if row["id"] not in chosen_ids and len(chosen) < per_source)
        selected.extend(chosen)
    return selected


def main() -> int:
    test_rows = read_jsonl(SOURCE / "test.internal.jsonl")
    train_rows = read_jsonl(SOURCE / "train.internal.jsonl")
    dev_rows = read_jsonl(SOURCE / "dev.internal.jsonl")
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    train_hashes = {digest(row["input_text"]) for row in train_rows}
    dev_hashes = {digest(row["input_text"]) for row in dev_rows}
    seen_hashes: set[str] = set()
    duplicate_ids: list[str] = []
    leakage: list[dict] = []
    issues: list[dict] = []
    normalized: list[dict] = []
    conll_blocks: list[str] = []
    index_rows: list[dict] = []
    label_counts: dict[str, Counter] = defaultdict(Counter)
    source_counts = Counter()
    line_number = 1

    for row in test_rows:
        sample_id = str(row["id"])
        source = str(row["source"])
        text = str(row["input_text"])
        text_hash = digest(text)
        if text_hash in seen_hashes:
            duplicate_ids.append(sample_id)
            continue
        seen_hashes.add(text_hash)
        if text_hash in train_hashes or text_hash in dev_hashes:
            leakage.append({"id": sample_id, "in_train": text_hash in train_hashes, "in_dev": text_hash in dev_hashes})
            continue
        entities = [
            {"text": item["text"], "label": item["label"], "start": item["start"], "end": item["end"]}
            for item in row.get("entities", []) if item.get("label") in LABELS
        ]
        tokens, token_issues = entities_to_bio(text, entities)
        if token_issues:
            issues.extend({"id": sample_id, **item} for item in token_issues)
        block = bio_text(tokens)
        start_line = line_number
        line_number += block.count("\n")
        gold_row = {
            "id": sample_id,
            "source": source,
            "input_text": text,
            "entities": entities,
            "checksum": text_hash,
            "benchmark_tier": "silver" if source == "vietbioner" else "corpus_gold",
        }
        normalized.append(gold_row)
        conll_blocks.append(block)
        index_rows.append({
            "id": sample_id,
            "source": source,
            "start_line": start_line,
            "end_line": line_number - 1,
            "token_count": len(tokens),
            "entity_count": len(entities),
            "checksum": text_hash,
        })
        source_counts[source] += 1
        label_counts[source].update(item["label"] for item in entities)

    write_jsonl(OUTPUT / "gold.jsonl", normalized)
    write_jsonl(OUTPUT / "article_index.jsonl", index_rows)
    write_jsonl(OUTPUT / "conversion_issues.jsonl", issues)
    write_jsonl(OUTPUT / "leakage_report.jsonl", leakage)
    (OUTPUT / "test.txt").write_text("".join(conll_blocks), encoding="utf-8")
    for source in sorted(source_counts):
        rows = [row for row in normalized if row["source"] == source]
        write_jsonl(OUTPUT / "by_source" / source / "gold.jsonl", rows)
        ids = {row["id"] for row in rows}
        blocks = [block for row, block in zip(normalized, conll_blocks) if row["id"] in ids]
        (OUTPUT / "by_source" / source / "test.txt").write_text("".join(blocks), encoding="utf-8")

    pilot_rows = select_pilot(normalized)
    write_jsonl(OUTPUT / "pilot_100" / "gold.jsonl", pilot_rows)
    block_by_id = {row["id"]: block for row, block in zip(normalized, conll_blocks)}
    pilot_blocks = [block_by_id[row["id"]] for row in pilot_rows]
    (OUTPUT / "pilot_100" / "test.txt").write_text("".join(pilot_blocks), encoding="utf-8")

    total_labels = Counter()
    for counts in label_counts.values():
        total_labels.update(counts)
    manifest = {
        "name": "VietBioNER-ViMedNer combined silver benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_split": "test",
        "schema": list(LABELS),
        "samples": len(normalized),
        "samples_by_source": dict(source_counts),
        "entities_by_label": {label: total_labels[label] for label in LABELS},
        "entities_by_source": {
            source: {label: label_counts[source][label] for label in LABELS}
            for source in sorted(source_counts)
        },
        "exact_test_duplicates_removed": len(duplicate_ids),
        "train_dev_leakage_removed": len(leakage),
        "conversion_issue_count": len(issues),
        "pilot_100": {
            "samples": len(pilot_rows),
            "samples_by_source": dict(Counter(row["source"] for row in pilot_rows)),
            "entities_by_label": dict(Counter(item["label"] for row in pilot_rows for item in row["entities"])),
        },
        "limitations": [
            "VietBioNER Symptom_and_Disease is split by heuristic, not expert re-annotation.",
            "VietBioNER has no CAUSE or TREATMENT annotation; those spans may be false negatives.",
            "Report combined and per-source metrics; do not describe the merged set as expert-validated gold.",
        ],
    }
    manifest["gold_checksum"] = digest((OUTPUT / "gold.jsonl").read_text(encoding="utf-8"))
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
