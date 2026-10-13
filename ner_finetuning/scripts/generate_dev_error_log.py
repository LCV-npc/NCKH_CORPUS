#!/usr/bin/env python3
"""Generate the required non-TP Dev error log from Gold and predictions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from error_driven_utils import generate_error_log, read_jsonl, write_jsonl


def ensure_dev_only(path: Path, rows: list[dict]) -> None:
    if "test" in str(path).casefold():
        raise ValueError("Refusing to use a path containing 'test'; this tool is Dev-only")
    bad = [row.get("id") for row in rows if ":dev:" not in str(row.get("id", "")) and row.get("source_split") != "dev"]
    if bad:
        raise ValueError(f"Non-Dev records detected, first IDs: {bad[:5]}")


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=root / "ner_finetuning" / "processed" / "medical_gold_v1" / "dev.complete.internal.jsonl")
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--round", type=int, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    gold = read_jsonl(args.gold)
    if args.limit > 0:
        gold = gold[:args.limit]
    ensure_dev_only(args.gold, gold)
    errors, summary = generate_error_log(gold, read_jsonl(args.predictions), args.round)
    write_jsonl(args.output, errors)
    summary_path = args.summary or args.output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
