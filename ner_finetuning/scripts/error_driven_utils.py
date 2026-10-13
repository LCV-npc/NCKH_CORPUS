#!/usr/bin/env python3
"""Utilities for error-driven prompt learning in Vietnamese medical NER."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(SCRIPT_DIR))

from evaluate_medical_three_systems import align_entities, raw_entities, read_jsonl, iou  # noqa: E402


ERROR_TYPES = ("sai_bien", "sai_nhan", "nhan_du", "bo_sot")
LABELS = ("DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC", "TREATMENT")
SECTION_MARKER = "## Các lỗi đã gặp ở vòng trước — rút kinh nghiệm"


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def normalize_pattern(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text).casefold()).strip()


def entity_view(entity: dict | None) -> dict | None:
    if entity is None:
        return None
    return {
        "text": str(entity["text"]),
        "label": str(entity["label"]),
        "start": int(entity["start"]),
        "end": int(entity["end"]),
    }


def exact_pairs(predicted: list[dict], gold: list[dict]) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    gold_by_key: dict[tuple, list[int]] = defaultdict(list)
    for index, entity in enumerate(gold):
        gold_by_key[(entity["start"], entity["end"], entity["label"])].append(index)
    used_gold: set[int] = set()
    for pred_index, entity in enumerate(predicted):
        key = (entity["start"], entity["end"], entity["label"])
        candidates = [index for index in gold_by_key.get(key, []) if index not in used_gold]
        if candidates:
            gold_index = candidates[0]
            pairs.append((pred_index, gold_index))
            used_gold.add(gold_index)
    return pairs


def pattern_key(error_type: str, predicted: dict | None, gold: dict | None) -> str:
    if error_type == "sai_bien":
        return "|".join((error_type, str((predicted or {}).get("label", "")), str((gold or {}).get("label", "")), normalize_pattern(str((gold or {}).get("text", "")))))
    if error_type == "sai_nhan":
        return "|".join((error_type, normalize_pattern(str((gold or {}).get("text", ""))), str((predicted or {}).get("label", "")), str((gold or {}).get("label", ""))))
    target = predicted if error_type == "nhan_du" else gold
    return "|".join((error_type, normalize_pattern(str((target or {}).get("text", ""))), str((target or {}).get("label", ""))))


def make_error(sample_id: str, sentence: str, error_type: str, predicted: dict | None, gold: dict | None, round_number: int) -> dict:
    predicted = entity_view(predicted)
    gold = entity_view(gold)
    return {
        "id": sample_id,
        "round": round_number,
        "sentence": sentence,
        "error_type": error_type,
        "predicted": predicted,
        "gold": gold,
        "pattern_key": pattern_key(error_type, predicted, gold),
    }


def classify_sentence(sample_id: str, sentence: str, predicted: list[dict], gold: list[dict], round_number: int) -> tuple[int, list[dict]]:
    pairs = exact_pairs(predicted, gold)
    matched_pred = {left for left, _ in pairs}
    matched_gold = {right for _, right in pairs}
    remaining_pred = [index for index in range(len(predicted)) if index not in matched_pred]
    remaining_gold = [index for index in range(len(gold)) if index not in matched_gold]
    errors: list[dict] = []
    consumed_pred: set[int] = set()
    consumed_gold: set[int] = set()

    # Same span with a different label is an unambiguous label error.
    for pred_index in remaining_pred:
        for gold_index in remaining_gold:
            if gold_index in consumed_gold:
                continue
            pred = predicted[pred_index]
            target = gold[gold_index]
            if (pred["start"], pred["end"]) == (target["start"], target["end"]) and pred["label"] != target["label"]:
                errors.append(make_error(sample_id, sentence, "sai_nhan", pred, target, round_number))
                consumed_pred.add(pred_index)
                consumed_gold.add(gold_index)
                break

    # Remaining overlapping spans are boundary errors. Pair by highest IoU.
    overlap_candidates: list[tuple[float, int, int]] = []
    for pred_index in remaining_pred:
        if pred_index in consumed_pred:
            continue
        for gold_index in remaining_gold:
            if gold_index in consumed_gold:
                continue
            score = iou(predicted[pred_index], gold[gold_index])
            if score > 0:
                overlap_candidates.append((score, pred_index, gold_index))
    for _, pred_index, gold_index in sorted(overlap_candidates, reverse=True):
        if pred_index in consumed_pred or gold_index in consumed_gold:
            continue
        errors.append(make_error(sample_id, sentence, "sai_bien", predicted[pred_index], gold[gold_index], round_number))
        consumed_pred.add(pred_index)
        consumed_gold.add(gold_index)

    for pred_index in remaining_pred:
        if pred_index not in consumed_pred:
            errors.append(make_error(sample_id, sentence, "nhan_du", predicted[pred_index], None, round_number))
    for gold_index in remaining_gold:
        if gold_index not in consumed_gold:
            errors.append(make_error(sample_id, sentence, "bo_sot", None, gold[gold_index], round_number))
    return len(pairs), errors


def generate_error_log(gold_rows: list[dict], prediction_rows: list[dict], round_number: int) -> tuple[list[dict], dict]:
    prediction_by_id = {str(row.get("id")): row for row in prediction_rows if not row.get("error")}
    errors: list[dict] = []
    tp = 0
    invalid_predictions = 0
    missing_records = 0
    for gold_row in gold_rows:
        sample_id = str(gold_row["id"])
        sentence = str(gold_row["input_text"])
        gold, gold_issues = align_entities(sentence, raw_entities(gold_row))
        if gold_issues:
            raise ValueError(f"Invalid Gold record {sample_id}: {gold_issues}")
        prediction_row = prediction_by_id.get(sample_id)
        if prediction_row is None:
            predicted = []
            missing_records += 1
        else:
            try:
                predicted, prediction_issues = align_entities(sentence, raw_entities(prediction_row))
                invalid_predictions += len(prediction_issues)
            except (ValueError, TypeError, json.JSONDecodeError):
                predicted = []
                invalid_predictions += 1
        sentence_tp, sentence_errors = classify_sentence(sample_id, sentence, predicted, gold, round_number)
        tp += sentence_tp
        errors.extend(sentence_errors)
    counts = Counter(row["error_type"] for row in errors)
    summary = {
        "round": round_number,
        "records": len(gold_rows),
        "tp": tp,
        "total_errors": len(errors),
        "by_type": {name: counts[name] for name in ERROR_TYPES},
        "missing_prediction_records": missing_records,
        "invalid_prediction_entities": invalid_predictions,
    }
    return errors, summary


def error_anchor(row: dict) -> str:
    """Return a stable identity for following one error target across rounds."""
    sample_id = str(row["id"])
    gold = row.get("gold")
    if gold:
        return f"gold|{sample_id}|{gold['start']}|{gold['end']}|{gold['label']}"
    predicted = row.get("predicted")
    if not predicted:
        raise ValueError(f"Error has neither Gold nor prediction: {row}")
    return f"pred|{sample_id}|{predicted['start']}|{predicted['end']}|{predicted['label']}"


def error_signature(row: dict) -> tuple:
    def entity_key(entity: dict | None) -> tuple | None:
        if not entity:
            return None
        return (entity["start"], entity["end"], entity["label"], normalize_pattern(entity["text"]))

    return (row["error_type"], entity_key(row.get("predicted")), entity_key(row.get("gold")))


def compare_error_rounds(previous_rows: list[dict], current_rows: list[dict]) -> dict:
    """Classify old errors as resolved, persistent or changed, and find new errors."""
    previous = {error_anchor(row): row for row in previous_rows}
    current = {error_anchor(row): row for row in current_rows}
    if len(previous) != len(previous_rows) or len(current) != len(current_rows):
        raise ValueError("Duplicate error anchors prevent deterministic round comparison")

    items: list[dict] = []
    for anchor in sorted(previous.keys() | current.keys()):
        old = previous.get(anchor)
        new = current.get(anchor)
        if old is None:
            status = "NEW"
        elif new is None:
            status = "RESOLVED"
        elif error_signature(old) == error_signature(new):
            status = "PERSISTENT"
        else:
            status = "CHANGED"
        items.append({"anchor": anchor, "status": status, "previous": old, "current": new})

    counts = Counter(item["status"] for item in items)
    previous_count = len(previous_rows)
    current_count = len(current_rows)
    return {
        "summary": {
            "previous_errors": previous_count,
            "current_errors": current_count,
            "resolved": counts["RESOLVED"],
            "persistent": counts["PERSISTENT"],
            "changed": counts["CHANGED"],
            "new": counts["NEW"],
            "net_error_delta": current_count - previous_count,
        },
        "items": items,
    }


def representative_label(row: dict) -> str:
    entity = row.get("gold") or row.get("predicted") or {}
    return str(entity.get("label", "UNKNOWN"))


def group_error_patterns(error_rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in error_rows:
        groups[str(row.get("pattern_key") or pattern_key(row["error_type"], row.get("predicted"), row.get("gold")))].append(row)
    candidates: list[dict] = []
    for key, rows in groups.items():
        latest = max(int(row.get("round", 0)) for row in rows)
        representative = max(rows, key=lambda row: (int(row.get("round", 0)), len(row.get("sentence", ""))))
        candidates.append({
            "pattern_key": key,
            "frequency": len(rows),
            "latest_round": latest,
            "error_type": representative["error_type"],
            "label": representative_label(representative),
            "representative": representative,
        })
    return sorted(candidates, key=lambda item: (-item["frequency"], -item["latest_round"], item["pattern_key"]))


def select_diverse_examples(error_rows: list[dict], max_examples: int = 20) -> list[dict]:
    if max_examples <= 0:
        return []
    candidates = group_error_patterns(error_rows)
    selected: list[dict] = []
    selected_keys: set[str] = set()
    type_counts: Counter = Counter()
    max_per_type = max(2, math.ceil(max_examples / 3))

    def take(candidate: dict) -> bool:
        if candidate["pattern_key"] in selected_keys or len(selected) >= max_examples:
            return False
        if type_counts[candidate["error_type"]] >= max_per_type:
            return False
        selected.append(candidate)
        selected_keys.add(candidate["pattern_key"])
        type_counts[candidate["error_type"]] += 1
        return True

    # Guarantee four error categories when available.
    for error_type in ERROR_TYPES:
        candidate = next((item for item in candidates if item["error_type"] == error_type), None)
        if candidate:
            take(candidate)
    # Then cover labels before filling by frequency.
    covered_labels = {item["label"] for item in selected}
    for label in LABELS:
        if label in covered_labels:
            continue
        candidate = next((item for item in candidates if item["label"] == label and item["pattern_key"] not in selected_keys), None)
        if candidate and take(candidate):
            covered_labels.add(label)
    for candidate in candidates:
        take(candidate)
    return selected


def quote(text: str) -> str:
    return text.replace("\n", " ").replace("'", "’")


def example_text(candidate: dict) -> str:
    row = candidate["representative"]
    sentence = quote(row["sentence"])
    predicted = row.get("predicted") or {}
    gold = row.get("gold") or {}
    frequency = candidate["frequency"]
    prefix = f"- [Lặp {frequency} lần] " if frequency > 1 else "- "
    if row["error_type"] == "sai_bien":
        return prefix + f"Câu: '{sentence}'. Sai: lấy '{quote(predicted['text'])}'. Đúng: chỉ nên lấy '{quote(gold['text'])}'."
    if row["error_type"] == "sai_nhan":
        return prefix + f"Câu: '{sentence}'. Cụm '{quote(gold['text'])}' bị gán nhãn {predicted['label']}, nhãn đúng là {gold['label']}."
    if row["error_type"] == "nhan_du":
        return prefix + f"Câu: '{sentence}'. Không nên gán nhãn cho '{quote(predicted['text'])}' vì đây không phải entity y khoa cụ thể theo Gold và ngữ cảnh này."
    return prefix + f"Câu: '{sentence}'. Cần nhận diện thêm '{quote(gold['text'])}' với nhãn {gold['label']}."


def strip_error_section(prompt: str) -> str:
    return prompt.split(SECTION_MARKER, 1)[0].rstrip()


def build_prompt(base_prompt: str, error_rows: list[dict], max_examples: int = 20, max_chars: int = 24000) -> tuple[str, list[dict]]:
    clean = strip_error_section(base_prompt)
    selected = select_diverse_examples(error_rows, max_examples=max_examples)
    header = "\n\n" + SECTION_MARKER + "\n"
    intro = "Các ví dụ dưới đây đến từ lỗi trên Dev ở vòng trước. Hãy áp dụng quy tắc, không sao chép máy móc entity ngoài câu hiện tại.\n"
    prompt = clean + header + intro
    kept: list[dict] = []
    for candidate in selected:
        line = example_text(candidate) + "\n"
        if len(prompt) + len(line) > max_chars:
            break
        prompt += line
        kept.append(candidate)
    return prompt.rstrip() + "\n", kept
