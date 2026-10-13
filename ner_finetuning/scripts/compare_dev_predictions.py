#!/usr/bin/env python3
"""Compare two NER prediction checkpoints on the same frozen Dev records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from error_driven_utils import compare_error_rounds, generate_error_log, read_jsonl, write_jsonl
from evaluate_medical_three_systems import LABELS, evaluate_system
from generate_dev_error_log import ensure_dev_only


def pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def markdown(before_name: str, after_name: str, results: dict, transition: dict) -> str:
    lines = [
        "# Dev checkpoint comparison",
        "",
        "| System | Precision | Recall | Micro-F1 | Macro-F1 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in (before_name, after_name):
        exact = results[name]["exact"]
        lines.append(
            f"| {name} | {pct(exact['micro']['precision'])} | {pct(exact['micro']['recall'])} | "
            f"{pct(exact['micro']['f1'])} | {pct(exact['macro']['f1'])} |"
        )

    lines.extend([
        "",
        "## Exact F1 by label",
        "",
        "| Label | Before | After | Delta |",
        "|---|---:|---:|---:|",
    ])
    for label in LABELS:
        before = results[before_name]["exact"]["per_label"][label]["f1"]
        after = results[after_name]["exact"]["per_label"][label]["f1"]
        lines.append(f"| {label} | {pct(before)} | {pct(after)} | {100 * (after-before):+.2f} pp |")

    summary = transition["summary"]
    lines.extend([
        "",
        "## Error transitions",
        "",
        "| Resolved | Persistent | Changed type/span | New | Net error delta |",
        "|---:|---:|---:|---:|---:|",
        f"| {summary['resolved']} | {summary['persistent']} | {summary['changed']} | "
        f"{summary['new']} | {summary['net_error_delta']:+d} |",
        "",
        "`RESOLVED` means an old error target is no longer an error. `CHANGED` means the same Gold entity "
        "is still wrong but in a different way. `NEW` means the candidate introduced an error absent from the baseline.",
        "",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=root / "ner_finetuning" / "processed" / "medical_gold_v1" / "dev.complete.internal.jsonl")
    parser.add_argument("--before", type=Path, required=True, help="Baseline predictions.jsonl")
    parser.add_argument("--after", type=Path, required=True, help="Candidate predictions.jsonl")
    parser.add_argument("--before-name", default="before")
    parser.add_argument("--after-name", default="after")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--iou", type=float, default=0.5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output_dir.exists():
        raise FileExistsError(f"Comparison checkpoint already exists: {args.output_dir}")
    gold = read_jsonl(args.gold)
    if args.limit:
        gold = gold[:args.limit]
    ensure_dev_only(args.gold, gold)
    before_predictions = read_jsonl(args.before)
    after_predictions = read_jsonl(args.after)

    results = {
        args.before_name: evaluate_system(gold, before_predictions, args.iou)[0],
        args.after_name: evaluate_system(gold, after_predictions, args.iou)[0],
    }
    before_errors, before_summary = generate_error_log(gold, before_predictions, 1)
    after_errors, after_summary = generate_error_log(gold, after_predictions, 2)
    transition = compare_error_rounds(before_errors, after_errors)

    args.output_dir.mkdir(parents=True)
    (args.output_dir / "metrics.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_jsonl(args.output_dir / "before_errors.jsonl", before_errors)
    write_jsonl(args.output_dir / "after_errors.jsonl", after_errors)
    (args.output_dir / "error_transitions.json").write_text(
        json.dumps(transition, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "before": before_summary,
        "after": after_summary,
        "transition": transition["summary"],
        "test_accessed": False,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = markdown(args.before_name, args.after_name, results, transition)
    (args.output_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
