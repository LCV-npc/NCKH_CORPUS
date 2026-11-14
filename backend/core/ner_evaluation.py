from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from core.ner_engine import NEREngine
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


def _label(entity: dict[str, Any]) -> str:
    return str(entity.get("label") or entity.get("entity_type") or "").strip().upper()


def _concept_id(entity: dict[str, Any]) -> str:
    return str(entity.get("concept_id") or "").strip()


def _same_type_and_concept(
    predicted: dict[str, Any],
    gold: dict[str, Any],
    require_concept_id: bool,
) -> bool:
    if _label(predicted) != _label(gold):
        return False
    if not require_concept_id:
        return True
    predicted_concept = _concept_id(predicted)
    gold_concept = _concept_id(gold)
    return bool(predicted_concept and gold_concept and predicted_concept == gold_concept)


def match_entities(
    predicted: list[dict],
    gold: list[dict],
    relaxed: bool,
    require_concept_id: bool = False,
) -> tuple[list[tuple[int, int]], set[int], set[int]]:
    candidates: list[tuple[float, int, int]] = []
    for pred_index, pred in enumerate(predicted):
        for gold_index, target in enumerate(gold):
            if not _same_type_and_concept(pred, target, require_concept_id):
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


def evaluate_system(
    gold_rows: list[dict],
    prediction_rows: list[dict],
    relaxed: bool,
    require_concept_id: bool = False,
    method_filter: str | None = None,
) -> tuple[dict, list[dict]]:
    predictions = {int(row["article_id"]): row for row in prediction_rows}
    counts = {label: [0, 0, 0] for label in LABELS}
    errors: list[dict] = []
    per_article: list[dict] = []
    for gold_row in gold_rows:
        article_id = int(gold_row["article_id"])
        gold = gold_row.get("entities", [])
        prediction = predictions.get(article_id, {})
        predicted = [
            entity
            for entity in prediction.get("entities", [])
            if method_filter is None or str(entity.get("matched_by") or "exact") == method_filter
        ]
        pairs, used_pred, used_gold = match_entities(
            predicted,
            gold,
            relaxed,
            require_concept_id=require_concept_id,
        )
        article_counts = {"tp": len(pairs), "fp": len(predicted) - len(pairs), "fn": len(gold) - len(pairs)}
        per_article.append({"article_id": article_id, **article_counts})
        for label in LABELS:
            tp = sum(1 for pred_index, _ in pairs if _label(predicted[pred_index]) == label)
            pred_total = sum(1 for item in predicted if _label(item) == label)
            gold_total = sum(1 for item in gold if _label(item) == label)
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
            if _label(pred) != _label(target):
                error_type = "LABEL_ERROR"
            elif require_concept_id and _concept_id(pred) != _concept_id(target):
                error_type = "CONCEPT_ERROR"
            else:
                error_type = "BOUNDARY_ERROR"
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


def _source_tokens(text: str) -> list[dict[str, Any]]:
    """Tokenize with the same Unicode-aware tokenizer used by rule NER."""
    return [
        {"text": token.word, "start": token.start, "end": token.end}
        for token in NEREngine()._tokenize(text)
    ]


def validate_entity_span(text: str, entity: dict[str, Any]) -> None:
    """Validate the repository-wide inclusive-start/exclusive-end contract."""
    start = entity.get("start")
    end = entity.get("end")
    surface = entity.get("text")
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValueError("Entity offset phải là số nguyên")
    if start < 0 or end <= start or end > len(text):
        raise ValueError("Entity offset không hợp lệ")
    if not isinstance(surface, str) or text[start:end] != surface:
        raise ValueError("Entity text không khớp offset văn bản gốc")


def spans_to_bio(text: str, entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert source-safe spans to BIO tokens and report boundary conflicts."""
    tokens = _source_tokens(text)
    issues: list[dict[str, Any]] = []
    for token in tokens:
        token["tag"] = "O"

    for entity in sorted(entities, key=lambda item: (item.get("start", -1), item.get("end", -1))):
        try:
            validate_entity_span(text, entity)
        except ValueError as exc:
            issues.append({"type": "INVALID_SPAN", "entity": entity, "detail": str(exc)})
            continue

        label = _label(entity)
        covered = [
            index
            for index, token in enumerate(tokens)
            if entity["start"] <= token["start"] and token["end"] <= entity["end"]
        ]
        if not covered or tokens[covered[0]]["start"] != entity["start"] or tokens[covered[-1]]["end"] != entity["end"]:
            issues.append({"type": "TOKEN_BOUNDARY", "entity": entity})
            continue

        if any(tokens[index]["tag"] != "O" for index in covered):
            issues.append({"type": "OVERLAP", "entity": entity})
            continue

        for offset, index in enumerate(covered):
            tokens[index]["tag"] = ("B-" if offset == 0 else "I-") + label

    return tokens, issues


def bio_to_spans(text: str, bio_tokens: list[dict[str, Any]] | list[str]) -> list[dict[str, Any]]:
    """Convert BIO labels back to source spans.

    A malformed ``I-X`` without an active ``X`` span is treated as ``B-X`` so
    the conversion remains deterministic while still producing a valid span.
    """
    source_tokens = _source_tokens(text)
    if bio_tokens and isinstance(bio_tokens[0], dict):
        tokens = bio_tokens  # type: ignore[assignment]
    else:
        labels = list(bio_tokens)
        if len(labels) != len(source_tokens):
            raise ValueError("Số BIO tag không khớp số token nguồn")
        tokens = [
            {**token, "tag": labels[index]}
            for index, token in enumerate(source_tokens)
        ]

    if len(tokens) != len(source_tokens):
        raise ValueError("BIO token count không khớp văn bản nguồn")

    spans: list[dict[str, Any]] = []
    active_label: str | None = None
    active_start: int | None = None
    active_end: int | None = None

    def close_active() -> None:
        nonlocal active_label, active_start, active_end
        if active_label is not None and active_start is not None and active_end is not None:
            spans.append({
                "text": text[active_start:active_end],
                "label": active_label,
                "start": active_start,
                "end": active_end,
            })
        active_label = None
        active_start = None
        active_end = None

    for token in tokens:
        tag = str(token.get("tag") or "O").upper()
        if tag == "O" or "-" not in tag:
            close_active()
            continue

        prefix, label = tag.split("-", 1)
        if not label:
            close_active()
            continue

        if prefix == "B" or active_label != label:
            close_active()
            active_label = label
            active_start = int(token["start"])
            active_end = int(token["end"])
        elif prefix == "I":
            active_end = int(token["end"])
        else:
            close_active()

    close_active()
    return spans


def _row_text(row: dict[str, Any]) -> str:
    for key in ("abstract", "input_text", "text"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    return ""


def _labels_for_tokens(text: str, entities: list[dict[str, Any]]) -> list[str]:
    tokens = _source_tokens(text)
    labels: list[str] = []
    for token in tokens:
        covering = [
            entity
            for entity in entities
            if int(entity.get("start", -1)) <= token["start"]
            and token["end"] <= int(entity.get("end", -1))
        ]
        if not covering:
            labels.append("O")
            continue
        chosen = max(
            covering,
            key=lambda entity: (
                int(entity.get("end", 0)) - int(entity.get("start", 0)),
                _label(entity),
            ),
        )
        labels.append(_label(chosen) or "O")
    return labels


def _entity_signature(entity: dict[str, Any], require_concept_id: bool = False) -> tuple[Any, ...]:
    signature: tuple[Any, ...] = (
        int(entity.get("start", -1)),
        int(entity.get("end", -1)),
        _label(entity),
    )
    if require_concept_id:
        signature += (_concept_id(entity),)
    return signature


def calculate_accuracy(
    gold_rows: list[dict[str, Any]],
    prediction_rows: list[dict[str, Any]],
    require_concept_id: bool = False,
) -> dict[str, Any]:
    """Calculate token and document accuracy as supplementary NER metrics."""
    predictions = {int(row["article_id"]): row for row in prediction_rows}
    token_total = 0
    token_correct = 0
    gold_labels: list[str] = []
    predicted_labels: list[str] = []
    document_correct = 0

    for gold_row in gold_rows:
        article_id = int(gold_row["article_id"])
        gold_entities = gold_row.get("entities", [])
        prediction = predictions.get(article_id, {})
        predicted_entities = prediction.get("entities", [])

        if sorted(_entity_signature(entity, require_concept_id) for entity in gold_entities) == sorted(
            _entity_signature(entity, require_concept_id) for entity in predicted_entities
        ):
            document_correct += 1

        text = _row_text(gold_row)
        if not text:
            continue
        gold_for_tokens = _labels_for_tokens(text, gold_entities)
        predicted_for_tokens = _labels_for_tokens(text, predicted_entities)
        token_total += len(gold_for_tokens)
        token_correct += sum(
            gold_label == predicted_label
            for gold_label, predicted_label in zip(gold_for_tokens, predicted_for_tokens)
        )
        gold_labels.extend(gold_for_tokens)
        predicted_labels.extend(predicted_for_tokens)

    document_total = len(gold_rows)
    document_accuracy = document_correct / document_total if document_total else 0.0
    if token_total:
        token_accuracy = token_correct / token_total
        classes = sorted(set(gold_labels))
        recalls = {
            label: sum(
                1
                for gold_label, predicted_label in zip(gold_labels, predicted_labels)
                if gold_label == label and predicted_label == label
            ) / gold_labels.count(label)
            for label in classes
            if gold_labels.count(label)
        }
        balanced_accuracy = sum(recalls.values()) / len(recalls) if recalls else 0.0
    else:
        token_accuracy = 0.0
        balanced_accuracy = 0.0
        recalls = {}

    return {
        "token": {
            "available": bool(token_total),
            "total_tokens": token_total,
            "correct_tokens": token_correct,
            "accuracy": token_accuracy,
            "balanced_accuracy": balanced_accuracy,
            "per_label_recall": recalls,
            "note": "Token accuracy includes O and is supplementary because O is usually dominant.",
        },
        "document_exact": {
            "available": bool(document_total),
            "correct_documents": document_correct,
            "total_documents": document_total,
            "accuracy": document_accuracy,
            "includes_concept_id": require_concept_id,
        },
    }


def _has_concept_ids(rows: list[dict[str, Any]]) -> bool:
    return any(
        _concept_id(entity)
        for row in rows
        for entity in row.get("entities", [])
    )


def _optional_concept_metrics(
    gold_rows: list[dict[str, Any]],
    prediction_rows: list[dict[str, Any]],
    relaxed: bool,
) -> dict[str, Any]:
    if not _has_concept_ids(gold_rows):
        return {
            "available": False,
            "reason": "Gold data chưa có concept_id; không fallback sang label-level metric.",
        }
    scores, _ = evaluate_system(
        gold_rows,
        prediction_rows,
        relaxed=relaxed,
        require_concept_id=True,
    )
    return {"available": True, **scores}


def _optional_concept_accuracy(
    gold_rows: list[dict[str, Any]],
    prediction_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not _has_concept_ids(gold_rows):
        return {
            "available": False,
            "reason": "Gold data chưa có concept_id; không fallback sang label-level accuracy.",
        }
    return {
        "available": True,
        **calculate_accuracy(gold_rows, prediction_rows, require_concept_id=True),
    }


def _method_metrics(
    gold_rows: list[dict[str, Any]],
    prediction_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    methods = sorted({
        str(entity.get("matched_by") or "exact")
        for row in prediction_rows
        for entity in row.get("entities", [])
        if str(entity.get("matched_by") or "exact") not in {"ambiguous", "fuzzy_candidate"}
    })
    return {
        method: {
            "exact": evaluate_system(gold_rows, prediction_rows, relaxed=False, method_filter=method)[0],
            "relaxed": evaluate_system(gold_rows, prediction_rows, relaxed=True, method_filter=method)[0],
        }
        for method in methods
    }


def evaluate_run(run_dir: Path, gold_path: Path) -> dict[str, Any]:
    gold_rows = read_jsonl(gold_path)
    results: dict[str, Any] = {}
    all_errors: list[dict] = []
    for system in SYSTEMS:
        predictions = read_jsonl(run_dir / system / "predictions.jsonl")
        exact, exact_errors = evaluate_system(gold_rows, predictions, relaxed=False)
        relaxed, relaxed_errors = evaluate_system(gold_rows, predictions, relaxed=True)
        results[system] = {
            "exact": exact,
            "relaxed": relaxed,
            "concept_exact": _optional_concept_metrics(gold_rows, predictions, relaxed=False),
            "concept_relaxed": _optional_concept_metrics(gold_rows, predictions, relaxed=True),
            "accuracy": calculate_accuracy(gold_rows, predictions),
            "concept_accuracy": _optional_concept_accuracy(gold_rows, predictions),
            "by_method": _method_metrics(gold_rows, predictions),
        }
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
            for mode in ("exact", "relaxed"):
                result = modes[mode]
                for label, values in result["per_label"].items():
                    writer.writerow([system, mode, label, values["tp"], values["fp"], values["fn"], values["precision"], values["recall"], values["f1"]])
                writer.writerow([system, mode, "MICRO", *[result["micro"][key] for key in ("tp", "fp", "fn", "precision", "recall", "f1")]])
                writer.writerow([system, mode, "MACRO", "", "", "", result["macro"]["precision"], result["macro"]["recall"], result["macro"]["f1"]])

    with (run_dir / "metrics_extra.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["system", "section", "mode", "method", "metric", "value"])
        for system, result in results.items():
            for section, concept_key in (("concept", "concept_exact"), ("concept", "concept_relaxed")):
                concept_result = result[concept_key]
                if not concept_result.get("available"):
                    writer.writerow([system, section, concept_key.removeprefix("concept_"), "", "available", False])
                    continue
                for metric_name in ("precision", "recall", "f1"):
                    writer.writerow([
                        system,
                        section,
                        concept_key.removeprefix("concept_"),
                        "ALL",
                        metric_name,
                        concept_result["micro"][metric_name],
                    ])

            for metric_name in ("accuracy", "balanced_accuracy"):
                writer.writerow([
                    system,
                    "token",
                    "",
                    "ALL",
                    metric_name,
                    result["accuracy"]["token"].get(metric_name, 0.0),
                ])
            writer.writerow([
                system,
                "document",
                "",
                "ALL",
                "accuracy",
                result["accuracy"]["document_exact"]["accuracy"],
            ])

            concept_accuracy = result["concept_accuracy"]
            if concept_accuracy.get("available"):
                writer.writerow([
                    system,
                    "concept_document",
                    "",
                    "ALL",
                    "accuracy",
                    concept_accuracy["document_exact"]["accuracy"],
                ])
                writer.writerow([
                    system,
                    "concept_token",
                    "",
                    "ALL",
                    "accuracy",
                    concept_accuracy["token"].get("accuracy", 0.0),
                ])

            for method, method_result in result["by_method"].items():
                for mode in ("exact", "relaxed"):
                    for metric_name in ("precision", "recall", "f1"):
                        writer.writerow([
                            system,
                            "matched_by",
                            mode,
                            method,
                            metric_name,
                            method_result[mode]["micro"][metric_name],
                        ])
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
        "| He thong | Che do | Macro-F1 | Micro-F1 | Concept-F1 | Token accuracy | Document accuracy | TP | FP | FN |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system, modes in results.items():
        for mode, result in modes.items():
            if mode not in {"exact", "relaxed"}:
                continue
            micro = result["micro"]
            concept = modes.get(f"concept_{mode}", {})
            concept_f1 = concept.get("micro", {}).get("f1") if concept.get("available") else None
            token_accuracy = modes["accuracy"]["token"].get("accuracy", 0.0)
            document_accuracy = modes["accuracy"]["document_exact"].get("accuracy", 0.0)
            concept_text = f"{concept_f1:.4f}" if concept_f1 is not None else "N/A"
            lines.append(
                f"| {system} | {mode} | {result['macro']['f1']:.4f} | {micro['f1']:.4f} | "
                f"{concept_text} | {token_accuracy:.4f} | {document_accuracy:.4f} | "
                f"{micro['tp']} | {micro['fp']} | {micro['fn']} |"
            )
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results

