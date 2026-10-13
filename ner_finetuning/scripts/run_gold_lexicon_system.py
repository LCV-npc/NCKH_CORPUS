#!/usr/bin/env python3
"""Apply a train-only Gold entity lexicon to a Dev split without label leakage."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from core.ner_dict import normalize_match_text  # noqa: E402
from core.ner_experiment import tokenize  # noqa: E402


LABELS = {"DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC", "TREATMENT"}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def token_keys(text: str) -> tuple[str, ...]:
    return tuple(
        key
        for token in tokenize(text)
        if (key := normalize_match_text(token["text"]))
    )


def build_lexicon(train_rows: list[dict]) -> tuple[dict[tuple[str, ...], dict], dict]:
    labels_by_term: dict[str, Counter[str]] = defaultdict(Counter)
    surfaces_by_term: dict[str, Counter[str]] = defaultdict(Counter)
    for row in train_rows:
        if str(row.get("source_split", "")).lower() in {"dev", "test"}:
            raise ValueError("Gold lexicon source must not contain Dev/Test records")
        for entity in row.get("entities", []):
            label = str(entity.get("label", ""))
            surface = str(entity.get("text", "")).strip()
            normalized = normalize_match_text(surface)
            if label in LABELS and normalized:
                labels_by_term[normalized][label] += 1
                surfaces_by_term[normalized][surface] += 1

    lexicon: dict[tuple[str, ...], dict] = {}
    ambiguous = 0
    for normalized, counts in labels_by_term.items():
        if len(counts) != 1:
            ambiguous += 1
            continue
        keys = token_keys(normalized)
        if not keys:
            continue
        label = next(iter(counts))
        candidate = {
            "normalized_term": normalized,
            "label": label,
            "frequency": sum(counts.values()),
            "surface": surfaces_by_term[normalized].most_common(1)[0][0],
        }
        existing = lexicon.get(keys)
        if existing and existing["label"] != label:
            ambiguous += 1
            lexicon.pop(keys, None)
        elif not existing or candidate["frequency"] > existing["frequency"]:
            lexicon[keys] = candidate

    stats = {
        "unambiguous_terms": len(lexicon),
        "ambiguous_terms_removed": ambiguous,
        "terms_by_label": dict(sorted(Counter(item["label"] for item in lexicon.values()).items())),
    }
    return lexicon, stats


def match_text(text: str, lexicon: dict[tuple[str, ...], dict]) -> list[dict]:
    tokens = [
        {**token, "key": normalize_match_text(token["text"])}
        for token in tokenize(text)
        if normalize_match_text(token["text"])
    ]
    by_first: dict[str, list[tuple[tuple[str, ...], dict]]] = defaultdict(list)
    for keys, info in lexicon.items():
        by_first[keys[0]].append((keys, info))
    for choices in by_first.values():
        choices.sort(key=lambda item: (-len(item[0]), -item[1]["frequency"], item[1]["normalized_term"]))

    candidates: list[dict] = []
    for index, token in enumerate(tokens):
        for keys, info in by_first.get(token["key"], []):
            chunk = tokens[index:index + len(keys)]
            if len(chunk) != len(keys) or tuple(part["key"] for part in chunk) != keys:
                continue
            start, end = chunk[0]["start"], chunk[-1]["end"]
            candidates.append({
                "text": text[start:end],
                "label": info["label"],
                "start": start,
                "end": end,
                "source": "TRAIN_GOLD_LEXICON",
                "gold_frequency": info["frequency"],
            })

    # Flat NER: longest span wins; frequency resolves equal-span conflicts.
    selected: list[dict] = []
    for item in sorted(candidates, key=lambda e: (-(e["end"] - e["start"]), -e["gold_frequency"], e["start"])):
        if any(item["start"] < kept["end"] and kept["start"] < item["end"] for kept in selected):
            continue
        selected.append(item)
    return sorted(selected, key=lambda e: (e["start"], e["end"], e["label"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    train_rows = read_jsonl(args.train)
    input_rows = read_jsonl(args.input)
    if args.limit > 0:
        input_rows = input_rows[:args.limit]
    if any(str(row.get("source_split", "")).lower() == "test" for row in input_rows):
        raise ValueError("This comparison is Dev-only; Test input is forbidden")

    lexicon, stats = build_lexicon(train_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in input_rows:
            result = {
                "id": row["id"],
                "system": "gold_lexicon_train_only",
                "input_text": row["input_text"],
                "entities": match_text(row["input_text"], lexicon),
            }
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    manifest = {
        "system": "gold_lexicon_train_only",
        "train": str(args.train.resolve()),
        "input": str(args.input.resolve()),
        "records": len(input_rows),
        **stats,
    }
    args.output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
