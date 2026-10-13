from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from core.ner_experiment import LABELS, SYSTEMS, append_jsonl, read_jsonl


def iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    intersection = max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))
    union = max(left["end"], right["end"]) - min(left["start"], right["start"])
    return intersection / union if union and intersection else 0.0


def metric(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def match_entities(predicted: list[dict], gold: list[dict], relaxed: bool) -> tuple[list[tuple[int, int]], set[int], set[int]]:
    candidates: list[tuple[float, int, int]] = []
    for pred_index, pred in enumerate(predicted):
        for gold_index, target in enumerate(gold):
            if pred["label"] != target["label"]:
                continue
            score = iou(pred, target)
            valid = score >= 0.5 if relaxed else (pred["start"], pred["end"]) == (target["start"], target["end"])
            if valid:
                candidates.append((score, pred_index, gold_index))
    pairs: list[tuple[int, int]] = []
    used_pred: set[int] = set()
    used_gold: set[int] = set()
    for _, pred_index, gold_index in sorted(candidates, reverse=True):
        if pred_index not in used_pred and gold_index not in used_gold:
            pairs.append((pred_index, gold_index))
            used_pred.add(pred_index)
            used_gold.add(gold_index)
    return pairs, used_pred, used_gold


def evaluate_system(gold_rows: list[dict], prediction_rows: list[dict], relaxed: bool) -> tuple[dict, list[dict]]:
    predictions = {int(row["article_id"]): row for row in prediction_rows}
    counts = {label: [0, 0, 0] for label in LABELS}
    errors: list[dict] = []
    per_article: list[dict] = []
    for gold_row in gold_rows:
        article_id = int(gold_row["article_id"])
        gold = gold_row.get("entities", [])
        prediction = predictions.get(article_id, {})
        predicted = prediction.get("entities", [])
        pairs, used_pred, used_gold = match_entities(predicted, gold, relaxed)
        article_counts = {"tp": len(pairs), "fp": len(predicted) - len(pairs), "fn": len(gold) - len(pairs)}
        per_article.append({"article_id": article_id, **article_counts})
        for label in LABELS:
            tp = sum(1 for pred_index, _ in pairs if predicted[pred_index]["label"] == label)
            pred_total = sum(1 for item in predicted if item["label"] == label)
            gold_total = sum(1 for item in gold if item["label"] == label)
            counts[label][0] += tp
            counts[label][1] += pred_total - tp
            counts[label][2] += gold_total - tp
        unmatched_pred = [index for index in range(len(predicted)) if index not in used_pred]
        unmatched_gold = [index for index in range(len(gold)) if index not in used_gold]
        claimed_pred: set[int] = set()
        claimed_gold: set[int] = set()
        for pred_index in unmatched_pred:
            pred = predicted[pred_index]
            overlaps = [(iou(pred, gold[gold_index]), gold_index) for gold_index in unmatched_gold if iou(pred, gold[gold_index]) > 0]
            if not overlaps:
                continue
            score, gold_index = max(overlaps)
            target = gold[gold_index]
            error_type = "BOUNDARY_ERROR" if pred["label"] == target["label"] else "LABEL_ERROR"
            errors.append({"article_id": article_id, "type": error_type, "predicted": pred, "gold": target, "iou": score})
            claimed_pred.add(pred_index)
            claimed_gold.add(gold_index)
        for pred_index in unmatched_pred:
            if pred_index not in claimed_pred:
                errors.append({"article_id": article_id, "type": "FP", "predicted": predicted[pred_index], "gold": None})
        for gold_index in unmatched_gold:
            if gold_index not in claimed_gold:
                errors.append({"article_id": article_id, "type": "FN", "predicted": None, "gold": gold[gold_index]})
        if prediction.get("error"):
            errors.append({"article_id": article_id, "type": "API_FAILURE", "detail": prediction["error"]})
    per_label = {label: metric(*counts[label]) for label in LABELS}
    totals = [sum(counts[label][index] for label in LABELS) for index in range(3)]
    macro = {
        key: sum(per_label[label][key] for label in LABELS) / len(LABELS)
        for key in ("precision", "recall", "f1")
    }
    return {"per_label": per_label, "micro": metric(*totals), "macro": macro, "per_article": per_article}, errors


def evaluate_run(run_dir: Path, gold_path: Path) -> dict[str, Any]:
    gold_rows = read_jsonl(gold_path)
    results: dict[str, Any] = {}
    all_errors: list[dict] = []
    for system in SYSTEMS:
        predictions = read_jsonl(run_dir / system / "predictions.jsonl")
        exact, exact_errors = evaluate_system(gold_rows, predictions, relaxed=False)
        relaxed, relaxed_errors = evaluate_system(gold_rows, predictions, relaxed=True)
        results[system] = {"exact": exact, "relaxed": relaxed}
        all_errors.extend({"system": system, "mode": "exact", **row} for row in exact_errors)
        all_errors.extend({"system": system, "mode": "relaxed", **row} for row in relaxed_errors)
        article_dir = run_dir / system / "article_reports"
        article_dir.mkdir(parents=True, exist_ok=True)
        grouped: dict[int, list[dict]] = defaultdict(list)
        for row in exact_errors:
            grouped[int(row["article_id"])].append(row)
        for article_id, rows in grouped.items():
            (article_dir / f"{article_id:04d}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    (run_dir / "metrics.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (run_dir / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["system", "mode", "label", "tp", "fp", "fn", "precision", "recall", "f1"])
        for system, modes in results.items():
            for mode, result in modes.items():
                for label, values in result["per_label"].items():
                    writer.writerow([system, mode, label, values["tp"], values["fp"], values["fn"], values["precision"], values["recall"], values["f1"]])
                writer.writerow([system, mode, "MICRO", *[result["micro"][key] for key in ("tp", "fp", "fn", "precision", "recall", "f1")]])
                writer.writerow([system, mode, "MACRO", "", "", "", result["macro"]["precision"], result["macro"]["recall"], result["macro"]["f1"]])
    errors_path = run_dir / "errors.jsonl"
    if errors_path.exists():
        errors_path.unlink()
    for row in all_errors:
        append_jsonl(errors_path, row)
    with (run_dir / "errors.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["system", "mode", "article_id", "type", "predicted", "gold", "iou"])
        for row in all_errors:
            writer.writerow([row.get("system"), row.get("mode"), row.get("article_id"), row.get("type"), json.dumps(row.get("predicted"), ensure_ascii=False), json.dumps(row.get("gold"), ensure_ascii=False), row.get("iou")])
    lines = [
        "# Bao cao danh gia NER", "",
        "| He thong | Che do | Macro-F1 | Micro-F1 | TP | FP | FN |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for system, modes in results.items():
        for mode, result in modes.items():
            micro = result["micro"]
            lines.append(f"| {system} | {mode} | {result['macro']['f1']:.4f} | {micro['f1']:.4f} | {micro['tp']} | {micro['fp']} | {micro['fn']} |")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results

