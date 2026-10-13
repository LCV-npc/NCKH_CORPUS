#!/usr/bin/env python3
"""Pure-Python data and entity-metric helpers for supervised medical NER."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable


ENTITY_LABELS = ["DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC", "TREATMENT"]
BIO_LABELS = ["O"] + [f"{prefix}-{label}" for label in ENTITY_LABELS for prefix in ("B", "I")]
LABEL2ID = {label: index for index, label in enumerate(BIO_LABELS)}
ID2LABEL = {index: label for label, index in LABEL2ID.items()}
TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def tokenize_with_offsets(text: str) -> list[dict]:
    return [{"text": match.group(), "start": match.start(), "end": match.end()} for match in TOKEN_RE.finditer(text)]


def record_to_words(row: dict) -> tuple[list[dict], list[str]]:
    text = row["input_text"]
    words = tokenize_with_offsets(text)
    entities = sorted(row.get("entities", []), key=lambda entity: (entity["start"], entity["end"], entity["label"]))
    tags: list[str] = []
    outside_tag = "IGN" if row.get("is_partial_annotation") else "O"
    covered: set[int] = set()
    for word in words:
        owners = [
            (index, entity)
            for index, entity in enumerate(entities)
            if entity["start"] <= word["start"] and word["end"] <= entity["end"]
        ]
        if len(owners) > 1:
            raise ValueError(f"{row['id']}: token overlaps multiple entities: {word}")
        if not owners:
            tags.append(outside_tag)
            continue
        index, entity = owners[0]
        if entity["label"] not in ENTITY_LABELS:
            raise ValueError(f"{row['id']}: invalid entity label {entity['label']}")
        tags.append(f"{'B' if word['start'] == entity['start'] else 'I'}-{entity['label']}")
        covered.add(index)
    if len(covered) != len(entities):
        missing = [entity for index, entity in enumerate(entities) if index not in covered]
        raise ValueError(f"{row['id']}: entities are not token aligned: {missing[:3]}")
    return words, tags


def normalize_bio(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    previous_label: str | None = None
    for tag in tags:
        if tag == "O" or "-" not in tag:
            normalized.append("O")
            previous_label = None
            continue
        prefix, label = tag.split("-", 1)
        if label not in ENTITY_LABELS:
            normalized.append("O")
            previous_label = None
            continue
        if prefix == "I" and previous_label != label:
            prefix = "B"
        normalized.append(f"{prefix}-{label}")
        previous_label = label
    return normalized


def bio_to_entities(words: list[dict], tags: list[str], text: str) -> list[dict]:
    tags = normalize_bio(tags)
    entities: list[dict] = []
    current: dict | None = None
    for word, tag in zip(words, tags):
        if tag == "O":
            if current:
                current["text"] = text[current["start"]:current["end"]]
                entities.append(current)
                current = None
            continue
        prefix, label = tag.split("-", 1)
        if prefix == "B" or current is None or current["label"] != label:
            if current:
                current["text"] = text[current["start"]:current["end"]]
                entities.append(current)
            current = {"text": "", "label": label, "start": word["start"], "end": word["end"]}
        else:
            current["end"] = word["end"]
    if current:
        current["text"] = text[current["start"]:current["end"]]
        entities.append(current)
    return entities


def entity_counts_from_bio(gold_sequences: list[list[str]], pred_sequences: list[list[str]]) -> dict:
    counts = {label: Counter(tp=0, fp=0, fn=0) for label in ENTITY_LABELS}
    for gold_tags, pred_tags in zip(gold_sequences, pred_sequences):
        # Token-index spans are sufficient for exact entity matching in model selection.
        words = [{"start": i, "end": i + 1, "text": ""} for i in range(len(gold_tags))]
        text = " " * (len(words) + 1)
        gold = {(e["start"], e["end"], e["label"]) for e in bio_to_entities(words, gold_tags, text)}
        pred = {(e["start"], e["end"], e["label"]) for e in bio_to_entities(words, pred_tags, text)}
        for label in ENTITY_LABELS:
            gold_label = {item for item in gold if item[2] == label}
            pred_label = {item for item in pred if item[2] == label}
            counts[label]["tp"] += len(gold_label & pred_label)
            counts[label]["fp"] += len(pred_label - gold_label)
            counts[label]["fn"] += len(gold_label - pred_label)

    per_label: dict[str, dict] = {}
    for label, value in counts.items():
        tp, fp, fn = value["tp"], value["fp"], value["fn"]
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[label] = {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}
    totals = {key: sum(per_label[label][key] for label in ENTITY_LABELS) for key in ("tp", "fp", "fn")}
    micro_p = totals["tp"] / (totals["tp"] + totals["fp"]) if totals["tp"] + totals["fp"] else 0.0
    micro_r = totals["tp"] / (totals["tp"] + totals["fn"]) if totals["tp"] + totals["fn"] else 0.0
    micro_f1 = 2 * micro_p * micro_r / (micro_p + micro_r) if micro_p + micro_r else 0.0
    return {
        "per_label": per_label,
        "micro": {**totals, "precision": micro_p, "recall": micro_r, "f1": micro_f1},
        "macro": {
            key: sum(per_label[label][key] for label in ENTITY_LABELS) / len(ENTITY_LABELS)
            for key in ("precision", "recall", "f1")
        },
    }


def class_weights(rows: list[dict], outside_weight: float = 0.15) -> list[float]:
    counts = Counter()
    for row in rows:
        _, tags = record_to_words(row)
        counts.update(tag for tag in tags if tag in LABEL2ID)
    entity_total = sum(counts[label] for label in BIO_LABELS if label != "O")
    weights: list[float] = []
    for label in BIO_LABELS:
        if label == "O":
            weights.append(outside_weight)
        else:
            weights.append(entity_total / (max(1, counts[label]) * (len(BIO_LABELS) - 1)))
    mean_entity = sum(weights[1:]) / max(1, len(weights) - 1)
    return [weights[0]] + [value / mean_entity for value in weights[1:]]


def augment_with_synonyms(rows: list[dict], synonym_path: Path, labels: set[str], max_per_record: int = 1) -> list[dict]:
    """Create conservative Train-only replacements from a curated JSON mapping."""
    mapping = json.loads(synonym_path.read_text(encoding="utf-8-sig"))
    augmented: list[dict] = []
    for row in rows:
        made = 0
        for entity_index, entity in enumerate(row.get("entities", [])):
            if made >= max_per_record or entity["label"] not in labels:
                continue
            replacements = mapping.get(entity["text"], [])
            if isinstance(replacements, str):
                replacements = [replacements]
            for replacement in replacements[:1]:
                replacement = str(replacement).strip()
                if not replacement or replacement == entity["text"]:
                    continue
                start, end = entity["start"], entity["end"]
                text = row["input_text"][:start] + replacement + row["input_text"][end:]
                delta = len(replacement) - (end - start)
                entities: list[dict] = []
                for index, original in enumerate(row["entities"]):
                    item = dict(original)
                    if index == entity_index:
                        item.update(text=replacement, start=start, end=start + len(replacement))
                    elif item["start"] >= end:
                        item["start"] += delta
                        item["end"] += delta
                    entities.append(item)
                augmented.append({
                    **row,
                    "id": f"{row['id']}:aug:{entity_index}",
                    "input_text": text,
                    "entities": entities,
                    "augmentation": {"type": "curated_synonym", "from": entity["text"], "to": replacement},
                })
                made += 1
                break
    return augmented
