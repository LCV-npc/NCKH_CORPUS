#!/usr/bin/env python3
"""Evaluate one or more NER systems with exact and relaxed entity matching."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


LABELS = ["DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC_PROCEDURE", "TREATMENT"]


def read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                records.append(
                    {
                        "id": f"__invalid_line_{line_number}",
                        "_invalid_json": f"{path}:{line_number}: {exc}",
                    }
                )
    return records


def parse_entities(record: dict) -> list[dict]:
    if "_invalid_json" in record:
        raise ValueError(record["_invalid_json"])
    if isinstance(record.get("entities"), list):
        return record["entities"]
    payload = record.get("output_text")
    if isinstance(payload, str):
        parsed = json.loads(payload)
        return parsed.get("entities", [])
    contents = record.get("contents")
    if isinstance(contents, list):
        for message in reversed(contents):
            if message.get("role") == "model":
                text = message.get("parts", [{}])[0].get("text", "")
                return json.loads(text).get("entities", [])
    return []


def record_id(record: dict, index: int) -> str:
    return str(record.get("id", index))


def record_text(record: dict) -> str:
    if "input_text" in record:
        return str(record["input_text"])
    for message in record.get("contents", []):
        if message.get("role") == "user":
            value = message.get("parts", [{}])[0].get("text", "")
            return value.removeprefix("Văn bản: ")
    return ""


def add_offsets(text: str, entities: list[dict]) -> tuple[list[dict], int]:
    result: list[dict] = []
    cursor_by_text: dict[str, int] = defaultdict(int)
    unaligned = 0
    for entity in entities:
        item = dict(entity)
        if isinstance(item.get("start"), int) and isinstance(item.get("end"), int):
            result.append(item)
            continue
        surface = str(item.get("text", ""))
        start = text.find(surface, cursor_by_text[surface]) if surface else -1
        if start < 0:
            unaligned += 1
            continue
        item["start"] = start
        item["end"] = start + len(surface)
        cursor_by_text[surface] = item["end"]
        result.append(item)
    return result, unaligned


def span_iou(left: dict, right: dict) -> float:
    intersection = max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))
    if intersection == 0:
        return 0.0
    union = max(left["end"], right["end"]) - min(left["start"], right["start"])
    return intersection / union


def maximum_matching(edges: list[list[int]], right_size: int) -> int:
    right_match = [-1] * right_size

    def augment(left_index: int, seen: set[int]) -> bool:
        for right_index in edges[left_index]:
            if right_index in seen:
                continue
            seen.add(right_index)
            if right_match[right_index] < 0 or augment(right_match[right_index], seen):
                right_match[right_index] = left_index
                return True
        return False

    return sum(1 for left_index in range(len(edges)) if augment(left_index, set()))


def matched_count(predicted: list[dict], gold: list[dict], mode: str, iou_threshold: float) -> int:
    edges: list[list[int]] = []
    for pred in predicted:
        candidates: list[tuple[float, int]] = []
        for index, target in enumerate(gold):
            if pred.get("label") != target.get("label"):
                continue
            if mode == "exact":
                score = 1.0 if (pred["start"], pred["end"]) == (target["start"], target["end"]) else 0.0
            else:
                score = span_iou(pred, target)
            if score >= (1.0 if mode == "exact" else iou_threshold):
                candidates.append((score, index))
        candidates.sort(reverse=True)
        edges.append([index for _, index in candidates])
    return maximum_matching(edges, len(gold))


def metric(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def evaluate(gold_records: list[dict], pred_records: list[dict], mode: str, iou: float) -> dict:
    predictions = {record_id(record, index): record for index, record in enumerate(pred_records)}
    counts = {label: [0, 0, 0] for label in LABELS}
    unaligned = 0
    missing_records = 0
    parse_errors = 0
    invalid_labels = 0
    for index, gold_record in enumerate(gold_records):
        sample_id = record_id(gold_record, index)
        text = record_text(gold_record)
        gold, gold_unaligned = add_offsets(text, parse_entities(gold_record))
        pred_record = predictions.get(sample_id)
        if pred_record is None and index < len(pred_records):
            pred_record = pred_records[index]
        if pred_record is None:
            predicted = []
            missing_records += 1
        else:
            try:
                raw_predicted = parse_entities(pred_record)
            except (json.JSONDecodeError, TypeError, ValueError):
                raw_predicted = []
                parse_errors += 1
            invalid_labels += sum(1 for item in raw_predicted if item.get("label") not in LABELS)
            raw_predicted = [item for item in raw_predicted if item.get("label") in LABELS]
            predicted, pred_unaligned = add_offsets(text, raw_predicted)
            unaligned += pred_unaligned
        unaligned += gold_unaligned
        for label in LABELS:
            gold_label = [item for item in gold if item.get("label") == label]
            pred_label = [item for item in predicted if item.get("label") == label]
            tp = matched_count(pred_label, gold_label, mode, iou)
            counts[label][0] += tp
            counts[label][1] += len(pred_label) - tp
            counts[label][2] += len(gold_label) - tp

    per_label = {label: metric(*counts[label]) for label in LABELS}
    total = [sum(counts[label][i] for label in LABELS) for i in range(3)]
    macro = {
        key: sum(per_label[label][key] for label in LABELS) / len(LABELS)
        for key in ("precision", "recall", "f1")
    }
    return {
        "mode": mode,
        "iou_threshold": iou if mode == "relaxed" else None,
        "per_label": per_label,
        "micro": metric(*total),
        "macro": macro,
        "unaligned_entities": unaligned,
        "missing_prediction_records": missing_records,
        "prediction_parse_errors": parse_errors,
        "invalid_label_entities": invalid_labels,
        "valid_json_rate": (len(gold_records) - parse_errors) / len(gold_records) if gold_records else 0.0,
    }


def markdown_table(results: dict) -> str:
    lines = [
        "| Hệ thống | Chế độ | DISEASE F1 | SYMPTOM F1 | CAUSE F1 | DIAGNOSTIC F1 | TREATMENT F1 | Macro-F1 | Micro-F1 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system, modes in results.items():
        for mode in ("exact", "relaxed"):
            result = modes[mode]
            values = [100 * result["per_label"][label]["f1"] for label in LABELS]
            lines.append(
                f"| {system} | {mode} | "
                + " | ".join(f"{value:.2f}" for value in values)
                + f" | {100 * result['macro']['f1']:.2f} | {100 * result['micro']['f1']:.2f} |"
            )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument(
        "--system",
        action="append",
        default=[],
        metavar="NAME=FILE",
        help="Prediction JSONL; pass four times for the four-system comparison",
    )
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=0, help="Evaluate only the first N gold records")
    parser.add_argument("--output", type=Path, default=Path("evaluation.json"))
    parser.add_argument("--markdown", type=Path, default=Path("evaluation.md"))
    return parser.parse_args()


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    if not 0 < args.iou <= 1:
        raise ValueError("--iou must be in (0, 1]")
    systems: dict[str, Path] = {}
    for value in args.system:
        if "=" not in value:
            raise ValueError(f"Invalid --system value: {value!r}; expected NAME=FILE")
        name, filename = value.split("=", 1)
        systems[name] = Path(filename)
    if not systems:
        raise ValueError("At least one --system NAME=FILE is required")

    gold = read_jsonl(args.gold)
    if args.limit > 0:
        gold = gold[: args.limit]
    results: dict[str, dict] = {}
    for name, path in systems.items():
        predicted = read_jsonl(path)
        results[name] = {
            "exact": evaluate(gold, predicted, "exact", args.iou),
            "relaxed": evaluate(gold, predicted, "relaxed", args.iou),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(markdown_table(results), encoding="utf-8")
    print(markdown_table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
