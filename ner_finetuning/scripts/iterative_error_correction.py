#!/usr/bin/env python3
"""Automate Dev-only error-driven prompt learning without weight updates."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from error_driven_utils import build_prompt, compare_error_rounds, generate_error_log, read_jsonl, write_jsonl  # noqa: E402
from evaluate_medical_three_systems import evaluate_system  # noqa: E402
from generate_dev_error_log import ensure_dev_only  # noqa: E402
from run_prompt_v2_two_stage import run as run_two_stage  # noqa: E402


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def metric_view(result: dict) -> dict:
    return {
        mode: {
            "micro": result[mode]["micro"],
            "macro": result[mode]["macro"],
            "per_label": result[mode]["per_label"],
        }
        for mode in ("exact", "relaxed")
    }


def print_round(round_number: int, metrics: dict, error_summary: dict, previous: dict | None) -> None:
    exact = metrics["exact"]["micro"]
    relaxed = metrics["relaxed"]["micro"]
    previous_errors = previous["error_summary"]["total_errors"] if previous else None
    delta = error_summary["total_errors"] - previous_errors if previous_errors is not None else 0
    print(f"\nRound {round_number}")
    print("| Exact TP | Exact FP | Exact FN | Precision | Recall | F1 | Relaxed F1 | Errors | Delta |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    print(
        f"| {exact['tp']} | {exact['fp']} | {exact['fn']} | {100*exact['precision']:.2f}% | "
        f"{100*exact['recall']:.2f}% | {100*exact['f1']:.2f}% | {100*relaxed['f1']:.2f}% | "
        f"{error_summary['total_errors']} | {delta:+d} |"
    )
    counts = error_summary["by_type"]
    print(
        "Errors: "
        + ", ".join(f"{name}={counts.get(name, 0)}" for name in ("sai_bien", "sai_nhan", "nhan_du", "bo_sot"))
    )


def print_transition(transition: dict) -> None:
    summary = transition["summary"]
    print(
        "Transitions: "
        f"resolved={summary['resolved']}, persistent={summary['persistent']}, "
        f"changed={summary['changed']}, new={summary['new']}"
    )


def predictions_are_complete(path: Path, expected_ids: set[str]) -> tuple[bool, int]:
    if not path.exists():
        return False, len(expected_ids)
    rows = read_jsonl(path)
    successful = {str(row.get("id")) for row in rows if not row.get("error")}
    failed = sum(1 for row in rows if row.get("error"))
    return successful == expected_ids and failed == 0, len(expected_ids - successful) + failed


def run_round(
    args: argparse.Namespace,
    round_number: int,
    prompt_path: Path,
    round_root: Path,
    dev_rows: list[dict],
) -> Path:
    round_id = f"round_{round_number:02d}"
    expected_ids = {str(row["id"]) for row in dev_rows}
    base_options = dict(
        input=args.dev,
        extractor_prompt=prompt_path,
        verifier_prompt=args.verifier_prompt,
        output_root=round_root,
        run_id=round_id,
        model=args.model,
        api_key_env=args.api_key_env,
        limit=args.limit,
        timeout=args.timeout,
        retries=args.api_retries,
        delay=args.delay,
        parent_checkpoint=args.parent_checkpoint if round_number == 1 else f"round_{round_number-1:02d}",
        note=f"Error-driven Dev round {round_number}; no test access",
    )
    run_dir = round_root / round_id
    for pass_index in range(args.api_retry_passes):
        options = SimpleNamespace(**base_options, resume=run_dir.exists())
        run_two_stage(options)
        complete, missing = predictions_are_complete(run_dir / "predictions.jsonl", expected_ids)
        if complete:
            return run_dir
        print(f"Round {round_number}: retry pass {pass_index + 1}, incomplete_or_failed={missing}")
        base_options["delay"] = max(float(base_options["delay"]), args.retry_delay)
    raise RuntimeError(f"Round {round_number} still has failed/missing predictions after {args.api_retry_passes} passes")


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", type=Path, default=root / "ner_finetuning" / "processed" / "medical_gold_v1" / "dev.complete.internal.jsonl")
    parser.add_argument("--base-prompt", type=Path, default=root / "ner_finetuning" / "prompts" / "extractor_v2_vi.txt")
    parser.add_argument("--verifier-prompt", type=Path, default=root / "ner_finetuning" / "prompts" / "verifier_v2_vi.txt")
    parser.add_argument("--output-root", type=Path, default=root / "ner_finetuning" / "error_driven_runs")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("error-driven-%Y%m%d-%H%M%S"))
    parser.add_argument("--model", default="gemini-flash-lite-latest")
    parser.add_argument("--api-key-env", default="GEMINI_API_KEY")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--target-precision", type=float, default=0.70)
    parser.add_argument("--max-examples", type=int, default=20)
    parser.add_argument("--retain-error-rounds", type=int, default=2)
    parser.add_argument("--max-prompt-chars", type=int, default=24000)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--retry-delay", type=float, default=4.0)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--api-retries", type=int, default=5)
    parser.add_argument("--api-retry-passes", type=int, default=3)
    parser.add_argument("--parent-checkpoint", default="dev-prompt-v2-100-v1")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.max_rounds <= 20:
        raise ValueError("--max-rounds must be between 1 and 20")
    if not 0 < args.target_precision <= 1:
        raise ValueError("--target-precision must be in (0, 1]")
    if args.limit <= 0:
        raise ValueError("--limit must be positive; use an explicit frozen Dev subset")

    dev_rows = read_jsonl(args.dev)[:args.limit]
    ensure_dev_only(args.dev, dev_rows)
    run_root = args.output_root / args.run_id
    if run_root.exists():
        raise FileExistsError(f"Iteration run already exists: {run_root}")
    prompts_dir = run_root / "prompts"
    errors_dir = run_root / "errors"
    rounds_dir = run_root / "rounds"
    metrics_dir = run_root / "metrics"
    for path in (prompts_dir, errors_dir, rounds_dir, metrics_dir):
        path.mkdir(parents=True, exist_ok=True)

    base_prompt = args.base_prompt.read_text(encoding="utf-8-sig").strip() + "\n"
    current_prompt_path = prompts_dir / "prompt_v2.txt"
    current_prompt_path.write_text(base_prompt, encoding="utf-8")
    history = {
        "run_id": args.run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dev": str(args.dev.resolve()),
        "dev_checksum": digest(args.dev.read_text(encoding="utf-8")),
        "dev_records": len(dev_rows),
        "test_accessed": False,
        "target_precision": args.target_precision,
        "max_rounds": args.max_rounds,
        "rounds": [],
        "stop_reason": None,
    }
    history_path = run_root / "iteration_history.json"
    error_files: list[Path] = []

    for round_number in range(1, args.max_rounds + 1):
        run_dir = run_round(args, round_number, current_prompt_path, rounds_dir, dev_rows)
        prediction_path = run_dir / "predictions.jsonl"
        predictions = read_jsonl(prediction_path)
        metrics, _ = evaluate_system(dev_rows, predictions, args.iou)
        metric_path = metrics_dir / f"metrics_round{round_number}.json"
        metric_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        error_rows, error_summary = generate_error_log(dev_rows, predictions, round_number)
        error_path = errors_dir / f"dev_errors_round{round_number}.jsonl"
        write_jsonl(error_path, error_rows)
        (errors_dir / f"dev_errors_round{round_number}.summary.json").write_text(
            json.dumps(error_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        error_files.append(error_path)

        previous = history["rounds"][-1] if history["rounds"] else None
        transition = None
        if len(error_files) > 1:
            transition = compare_error_rounds(read_jsonl(error_files[-2]), error_rows)
            transition_path = errors_dir / f"transitions_round{round_number-1}_to_round{round_number}.json"
            transition_path.write_text(json.dumps(transition, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        delta_by_type = {
            name: error_summary["by_type"].get(name, 0) - (previous["error_summary"]["by_type"].get(name, 0) if previous else 0)
            for name in ("sai_bien", "sai_nhan", "nhan_du", "bo_sot")
        }
        round_history = {
            "round": round_number,
            "prompt_version": round_number + 1,
            "prompt": str(current_prompt_path.resolve()),
            "prompt_checksum": digest(current_prompt_path.read_text(encoding="utf-8")),
            "prediction": str(prediction_path.resolve()),
            "metrics": metric_view(metrics),
            "error_log": str(error_path.resolve()),
            "error_summary": error_summary,
            "error_delta_by_type": delta_by_type,
            "error_transition": transition["summary"] if transition else None,
        }
        history["rounds"].append(round_history)
        print_round(round_number, metrics, error_summary, previous)
        if transition:
            print_transition(transition)

        exact_precision = metrics["exact"]["micro"]["precision"]
        if exact_precision >= args.target_precision:
            history["stop_reason"] = "target_precision_reached"
        elif previous and error_summary["total_errors"] >= previous["error_summary"]["total_errors"]:
            history["stop_reason"] = "error_count_saturated"
        elif round_number >= args.max_rounds:
            history["stop_reason"] = "max_rounds_reached"

        if history["stop_reason"]:
            history["completed_at"] = datetime.now(timezone.utc).isoformat()
            history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            break

        retained_files = error_files[-args.retain_error_rounds:]
        prompt_error_rows: list[dict] = []
        for path in retained_files:
            prompt_error_rows.extend(read_jsonl(path))
        next_prompt, selected = build_prompt(
            base_prompt, prompt_error_rows,
            max_examples=args.max_examples,
            max_chars=args.max_prompt_chars,
        )
        next_version = round_number + 2
        next_prompt_path = prompts_dir / f"prompt_v{next_version}.txt"
        next_prompt_path.write_text(next_prompt, encoding="utf-8")
        selection_manifest = [
            {key: item[key] for key in ("pattern_key", "frequency", "latest_round", "error_type", "label")}
            for item in selected
        ]
        (prompts_dir / f"prompt_v{next_version}.selection.json").write_text(
            json.dumps(selection_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        round_history["next_prompt"] = str(next_prompt_path.resolve())
        round_history["selected_error_examples"] = len(selected)
        round_history["next_prompt_chars"] = len(next_prompt)
        current_prompt_path = next_prompt_path
        history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\nIteration history: {history_path}")
    print(f"Stop reason: {history['stop_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
