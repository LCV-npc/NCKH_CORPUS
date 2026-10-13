#!/usr/bin/env python3
"""Evaluate three Vietnamese medical NER systems and emit detailed errors."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


LABELS = ["DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC", "TREATMENT"]
ALIASES = {"DIAGNOSTIC_PROCEDURE": "DIAGNOSTIC", "bien_phap_chan_doan": "DIAGNOSTIC"}


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            rows.append({"id": f"__invalid_{line_number}", "_parse_error": str(exc)})
    return rows


def raw_entities(row: dict) -> list[dict]:
    if row.get("_parse_error"):
        raise ValueError(row["_parse_error"])
    if isinstance(row.get("entities"), list):
        return row["entities"]
    value = row.get("output_text")
    if isinstance(value, str):
        return json.loads(value).get("entities", [])
    return []


def align_entities(text: str, entities: list[dict]) -> tuple[list[dict], list[dict]]:
    aligned: list[dict] = []
    issues: list[dict] = []
    cursors: dict[str, int] = defaultdict(int)
    for original in entities:
        item = dict(original)
        item["label"] = ALIASES.get(str(item.get("label")), str(item.get("label")))
        if item["label"] not in LABELS:
            issues.append({"type": "INVALID_LABEL", "entity": item})
            continue
        if isinstance(item.get("start"), int) and isinstance(item.get("end"), int):
            start, end = item["start"], item["end"]
            if not 0 <= start < end <= len(text) or text[start:end] != item.get("text"):
                issues.append({"type": "NON_VERBATIM", "entity": item})
                continue
        else:
            surface = str(item.get("text", ""))
            start = text.find(surface, cursors[surface]) if surface else -1
            if start < 0:
                issues.append({"type": "NON_VERBATIM", "entity": item})
                continue
            end = start + len(surface)
            item["start"], item["end"] = start, end
            cursors[surface] = end
        aligned.append({"text": text[item["start"]:item["end"]], "label": item["label"], "start": item["start"], "end": item["end"]})
    unique: dict[tuple, dict] = {}
    for item in aligned:
        key = (item["start"], item["end"], item["label"])
        if key in unique:
            issues.append({"type": "DUPLICATE", "entity": item})
        else:
            unique[key] = item
    return sorted(unique.values(), key=lambda e: (e["start"], e["end"], e["label"])), issues


def iou(left: dict, right: dict) -> float:
    intersection = max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))
    union = max(left["end"], right["end"]) - min(left["start"], right["start"])
    return intersection / union if intersection and union else 0.0


def match(pred: list[dict], gold: list[dict], mode: str, threshold: float) -> list[tuple[int, int]]:
    candidates: list[tuple[float, int, int]] = []
    for pi, p in enumerate(pred):
        for gi, g in enumerate(gold):
            if p["label"] != g["label"]:
                continue
            score = 1.0 if (p["start"], p["end"]) == (g["start"], g["end"]) else (iou(p, g) if mode == "relaxed" else 0.0)
            if score >= (1.0 if mode == "exact" else threshold):
                candidates.append((score, pi, gi))
    pairs: list[tuple[int, int]] = []
    used_p: set[int] = set()
    used_g: set[int] = set()
    for _, pi, gi in sorted(candidates, reverse=True):
        if pi not in used_p and gi not in used_g:
            pairs.append((pi, gi))
            used_p.add(pi)
            used_g.add(gi)
    return pairs


def metric(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def classify_errors(sample_id: str, text: str, pred: list[dict], gold: list[dict], exact_pairs: list[tuple[int, int]]) -> list[dict]:
    matched_p = {pair[0] for pair in exact_pairs}
    matched_g = {pair[1] for pair in exact_pairs}
    remaining_p = [i for i in range(len(pred)) if i not in matched_p]
    remaining_g = [i for i in range(len(gold)) if i not in matched_g]
    errors: list[dict] = []
    consumed_p: set[int] = set()
    consumed_g: set[int] = set()

    # First identify same-span wrong-label cases.
    for pi in remaining_p:
        for gi in remaining_g:
            if gi in consumed_g or (pred[pi]["start"], pred[pi]["end"]) != (gold[gi]["start"], gold[gi]["end"]):
                continue
            if pred[pi]["label"] != gold[gi]["label"]:
                errors.append({"id": sample_id, "type": "LABEL_ERROR", "predicted": pred[pi], "gold": gold[gi], "context": text[max(0, gold[gi]["start"]-50):min(len(text), gold[gi]["end"]+50)]})
                consumed_p.add(pi); consumed_g.add(gi)
                break
    # Then same-label overlaps with incorrect boundaries.
    for pi in remaining_p:
        if pi in consumed_p:
            continue
        best = None
        for gi in remaining_g:
            if gi in consumed_g or pred[pi]["label"] != gold[gi]["label"]:
                continue
            score = iou(pred[pi], gold[gi])
            if score > 0 and (best is None or score > best[0]):
                best = (score, gi)
        if best:
            gi = best[1]
            errors.append({"id": sample_id, "type": "BOUNDARY_ERROR", "predicted": pred[pi], "gold": gold[gi], "iou": best[0], "context": text[max(0, gold[gi]["start"]-50):min(len(text), gold[gi]["end"]+50)]})
            consumed_p.add(pi); consumed_g.add(gi)
    for pi in remaining_p:
        if pi not in consumed_p:
            errors.append({"id": sample_id, "type": "FP", "predicted": pred[pi], "gold": None, "context": text[max(0, pred[pi]["start"]-50):min(len(text), pred[pi]["end"]+50)]})
    for gi in remaining_g:
        if gi not in consumed_g:
            errors.append({"id": sample_id, "type": "FN", "predicted": None, "gold": gold[gi], "context": text[max(0, gold[gi]["start"]-50):min(len(text), gold[gi]["end"]+50)]})
    return errors


def evaluate_system(gold_rows: list[dict], pred_rows: list[dict], threshold: float) -> tuple[dict, list[dict]]:
    pred_by_id = {str(row.get("id")): row for row in pred_rows}
    counts = {mode: {label: [0, 0, 0] for label in LABELS} for mode in ("exact", "relaxed")}
    errors: list[dict] = []
    quality = Counter()
    for gold_row in gold_rows:
        sample_id = str(gold_row["id"])
        text = gold_row["input_text"]
        gold, gold_issues = align_entities(text, raw_entities(gold_row))
        if gold_issues:
            raise ValueError(f"Invalid gold {sample_id}: {gold_issues}")
        pred_row = pred_by_id.get(sample_id)
        if pred_row is None:
            pred = []
            quality["MISSING_RECORD"] += 1
        else:
            try:
                pred, pred_issues = align_entities(text, raw_entities(pred_row))
            except (ValueError, TypeError, json.JSONDecodeError):
                pred, pred_issues = [], [{"type": "INVALID_JSON"}]
            for issue in pred_issues:
                quality[issue["type"]] += 1
                errors.append({"id": sample_id, **issue, "context": ""})
        mode_pairs: dict[str, list[tuple[int, int]]] = {}
        for mode in ("exact", "relaxed"):
            pairs = match(pred, gold, mode, threshold)
            mode_pairs[mode] = pairs
            for label in LABELS:
                tp = sum(1 for pi, gi in pairs if pred[pi]["label"] == label and gold[gi]["label"] == label)
                p_total = sum(1 for item in pred if item["label"] == label)
                g_total = sum(1 for item in gold if item["label"] == label)
                counts[mode][label][0] += tp
                counts[mode][label][1] += p_total - tp
                counts[mode][label][2] += g_total - tp
        errors.extend(classify_errors(sample_id, text, pred, gold, mode_pairs["exact"]))

    results: dict[str, dict] = {}
    for mode in ("exact", "relaxed"):
        per_label = {label: metric(*counts[mode][label]) for label in LABELS}
        totals = [sum(counts[mode][label][index] for label in LABELS) for index in range(3)]
        results[mode] = {
            "per_label": per_label,
            "micro": metric(*totals),
            "macro": {key: sum(per_label[label][key] for label in LABELS) / len(LABELS) for key in ("precision", "recall", "f1")},
            "iou_threshold": threshold if mode == "relaxed" else None,
        }
    results["quality"] = dict(quality)
    return results, errors


def markdown(results: dict) -> str:
    lines = [
        "| System | Match | Precision | Recall | Micro-F1 | Macro-F1 | DISEASE F1 | SYMPTOM F1 | CAUSE F1 | DIAGNOSTIC F1 | TREATMENT F1 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system, modes in results.items():
        for mode in ("exact", "relaxed"):
            value = modes[mode]
            fields = [100 * value["per_label"][label]["f1"] for label in LABELS]
            lines.append(
                f"| {system} | {mode} | {100*value['micro']['precision']:.2f} | {100*value['micro']['recall']:.2f} | "
                f"{100*value['micro']['f1']:.2f} | {100*value['macro']['f1']:.2f} | " + " | ".join(f"{item:.2f}" for item in fields) + " |"
            )
    return "\n".join(lines) + "\n"


def summarize_errors(errors: list[dict]) -> dict:
    summary: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for error in errors:
        system = str(error.get("system", "unknown"))
        error_type = str(error.get("type", "UNKNOWN"))
        target = error.get("gold") or error.get("predicted") or error.get("entity") or {}
        label = str(target.get("label", "UNSPECIFIED"))
        summary[system][error_type][label] += 1
    return {
        system: {error_type: dict(sorted(labels.items())) for error_type, labels in sorted(types.items())}
        for system, types in sorted(summary.items())
    }


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=root / "ner_finetuning" / "processed" / "medical_gold_v1" / "test.internal.jsonl")
    parser.add_argument("--system", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--output-root", type=Path, default=root / "ner_finetuning" / "evaluation_runs")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--limit", type=int, default=0, help="Evaluate only the first N gold records")
    parser.add_argument("--note", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.system:
        raise ValueError("Pass --system NAME=PATH for each of the three systems")
    systems: dict[str, Path] = {}
    for value in args.system:
        if "=" not in value:
            raise ValueError(f"Invalid --system: {value}")
        name, path = value.split("=", 1)
        systems[name] = Path(path)
    run_dir = args.output_root / args.run_id
    if run_dir.exists():
        raise FileExistsError(f"Checkpoint already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    gold = read_jsonl(args.gold)
    if args.limit > 0:
        gold = gold[:args.limit]
    all_results: dict[str, dict] = {}
    all_errors: list[dict] = []
    for name, path in systems.items():
        result, errors = evaluate_system(gold, read_jsonl(path), args.iou)
        all_results[name] = result
        all_errors.extend({"system": name, **error} for error in errors)
        system_dir = run_dir / name
        system_dir.mkdir()
        (system_dir / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_errors(system_dir / "errors.jsonl", errors)
    (run_dir / "metrics.json").write_text(json.dumps(all_results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "report.md").write_text(markdown(all_results), encoding="utf-8")
    write_errors(run_dir / "errors.jsonl", all_errors)
    error_summary = summarize_errors(all_errors)
    (run_dir / "error_summary.json").write_text(json.dumps(error_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (run_dir / "errors.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["system", "id", "type", "label", "text", "start", "end", "gold_label", "gold_text", "context"])
        writer.writeheader()
        for error in all_errors:
            predicted = error.get("predicted") or error.get("entity") or {}
            target = error.get("gold") or {}
            writer.writerow({"system": error.get("system", ""), "id": error.get("id", ""), "type": error.get("type", ""), "label": predicted.get("label", ""), "text": predicted.get("text", ""), "start": predicted.get("start", ""), "end": predicted.get("end", ""), "gold_label": target.get("label", ""), "gold_text": target.get("text", ""), "context": error.get("context", "")})
    manifest = {"run_id": args.run_id, "created_at": datetime.now(timezone.utc).isoformat(), "gold": str(args.gold.resolve()), "systems": {name: str(path.resolve()) for name, path in systems.items()}, "iou": args.iou, "note": args.note}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(markdown(all_results))
    print(f"Checkpoint: {run_dir}")
    return 0


def write_errors(path: Path, errors: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for error in errors:
            handle.write(json.dumps(error, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
