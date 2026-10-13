#!/usr/bin/env python3
"""Evaluate and compare two XLM-R checkpoints on exactly the same BIO Dev file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluate_bio_checkpoint import evaluate_checkpoint
from supervised_ner_utils import ENTITY_LABELS


def pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def comparison(name_a: str, name_b: str, a: dict, b: dict) -> str:
    lines = [
        "| Checkpoint | Precision | Recall | Exact F1 | Macro-F1 | Relaxed F1 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, result in ((name_a, a), (name_b, b)):
        exact = result["exact"]
        relaxed = result["relaxed"]
        lines.append(
            f"| {name} | {pct(exact['micro']['precision'])} | {pct(exact['micro']['recall'])} | "
            f"{pct(exact['micro']['f1'])} | {pct(exact['macro_f1'])} | {pct(relaxed['micro']['f1'])} |"
        )
    lines.extend([
        "",
        "| Label | " + name_a + " Exact F1 | " + name_b + " Exact F1 | Delta | " + name_a + " Relaxed F1 | " + name_b + " Relaxed F1 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for label in ENTITY_LABELS:
        exact_a = a["exact"]["per_label"][label]["f1"]
        exact_b = b["exact"]["per_label"][label]["f1"]
        relaxed_a = a["relaxed"]["per_label"][label]["f1"]
        relaxed_b = b["relaxed"]["per_label"][label]["f1"]
        lines.append(
            f"| {label} | {pct(exact_a)} | {pct(exact_b)} | {100 * (exact_b-exact_a):+.2f} pp | "
            f"{pct(relaxed_a)} | {pct(relaxed_b)} |"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-a", type=Path, default=Path("xlmr-vimed-only-v1"))
    parser.add_argument("--checkpoint-b", type=Path, default=Path("xlmr-vimed+viebio-v1"))
    parser.add_argument("--name-a", default="xlmr-vimed-only-v1")
    parser.add_argument("--name-b", default="xlmr-vimed+viebio-v1")
    parser.add_argument("--dev", type=Path, default=Path("dev.txt"))
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result_a = evaluate_checkpoint(args.checkpoint_a, args.dev, args.max_length, args.batch_size)
    result_b = evaluate_checkpoint(args.checkpoint_b, args.dev, args.max_length, args.batch_size)
    text = comparison(args.name_a, args.name_b, result_a, result_b)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({args.name_a: result_a, args.name_b: result_b}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.output.with_suffix(".md").write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
