#!/usr/bin/env python3
"""Compare Prompt V1 one-stage with Prompt V2 Extractor -> Verifier."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from evaluate_medical_three_systems import evaluate_system, read_jsonl  # noqa: E402


def pct(value: float) -> str:
    return f"{100 * value:.2f}"


def report(results: dict, removed_count: int) -> str:
    lines = [
        "# Prompt V1 vs Prompt V2 on the same 100 Dev records",
        "",
        "| System | Match | TP | FP | FN | Precision | Recall | F1 | Macro-F1 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system, modes in results.items():
        for mode in ("exact", "relaxed"):
            value = modes[mode]
            micro = value["micro"]
            lines.append(
                f"| {system} | {mode} | {micro['tp']} | {micro['fp']} | {micro['fn']} | "
                f"{pct(micro['precision'])}% | {pct(micro['recall'])}% | {pct(micro['f1'])}% | {pct(value['macro']['f1'])}% |"
            )
    before = results["prompt_v1"]["exact"]["micro"]
    after = results["prompt_v2_two_stage"]["exact"]["micro"]
    lines.extend([
        "",
        "## Exact delta",
        "",
        f"- Precision: {pct(after['precision'] - before['precision'])} percentage points",
        f"- Recall: {pct(after['recall'] - before['recall'])} percentage points",
        f"- F1: {pct(after['f1'] - before['f1'])} percentage points",
        f"- FP: {after['fp'] - before['fp']:+d}",
        f"- FN: {after['fn'] - before['fn']:+d}",
        f"- Verifier decisions logged: {removed_count}",
        "",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=root / "ner_finetuning" / "processed" / "medical_gold_v1" / "dev.complete.internal.jsonl")
    parser.add_argument("--v1", type=Path, default=root / "ner_finetuning" / "experiment_runs" / "dev-pilot-100-v1" / "prompt_only.jsonl")
    parser.add_argument("--v2", type=Path, default=root / "ner_finetuning" / "experiment_runs" / "dev-prompt-v2-100-v1" / "predictions.jsonl")
    parser.add_argument("--removed", type=Path, default=root / "ner_finetuning" / "experiment_runs" / "dev-prompt-v2-100-v1" / "verifier_removed.jsonl")
    parser.add_argument("--output", type=Path, default=root / "ner_finetuning" / "evaluation_runs" / "dev-prompt-v2-100-v1")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--iou", type=float, default=0.5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gold = read_jsonl(args.gold)[:args.limit]
    systems = {"prompt_v1": read_jsonl(args.v1), "prompt_v2_two_stage": read_jsonl(args.v2)}
    results: dict[str, dict] = {}
    errors_by_system: dict[str, list[dict]] = {}
    for name, predictions in systems.items():
        metrics, errors = evaluate_system(gold, predictions, args.iou)
        results[name] = metrics
        errors_by_system[name] = errors
    removed = read_jsonl(args.removed) if args.removed.exists() else []
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "metrics.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output / "errors.json").write_text(json.dumps(errors_by_system, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    text = report(results, len(removed))
    (args.output / "comparison.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
