#!/usr/bin/env python3
"""Shared BIO loading, subword alignment and entity metrics for XLM-R scripts."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from supervised_ner_hf import _encode_single_sequence
from supervised_ner_utils import BIO_LABELS, ENTITY_LABELS, ID2LABEL, LABEL2ID


VALID_TAGS = set(BIO_LABELS) | {"IGN"}


def read_bio(path: Path) -> list[dict]:
    sentences: list[dict] = []
    tokens: list[str] = []
    tags: list[str] = []

    def close() -> None:
        if not tokens:
            return
        normalized = normalize_bio(tags, allow_ign=True)
        sentences.append({"id": f"{path.stem}:{len(sentences):06d}", "tokens": list(tokens), "tags": normalized})
        tokens.clear()
        tags.clear()

    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line:
            close()
            continue
        parts = line.rsplit(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"{path}:{line_number}: expected TOKEN TAG, got {raw!r}")
        token, tag = parts
        if tag not in VALID_TAGS:
            raise ValueError(f"{path}:{line_number}: unsupported BIO tag {tag!r}")
        tokens.append(token)
        tags.append(tag)
    close()
    if not sentences:
        raise ValueError(f"No BIO sentences found in {path}")
    return sentences


def normalize_bio(tags: list[str], allow_ign: bool = False) -> list[str]:
    normalized: list[str] = []
    previous_label: str | None = None
    for tag in tags:
        if tag == "IGN":
            if not allow_ign:
                raise ValueError("IGN is allowed for training masks, not Gold evaluation")
            normalized.append(tag)
            previous_label = None
            continue
        if tag == "O":
            normalized.append(tag)
            previous_label = None
            continue
        prefix, label = tag.split("-", 1)
        if prefix == "I" and previous_label != label:
            prefix = "B"
        normalized.append(f"{prefix}-{label}")
        previous_label = label
    return normalized


def encode_bio_sentences(sentences: list[dict], tokenizer, max_length: int) -> tuple[list[dict], list[dict]]:
    """Encode BIO sentences without cutting a multi-token entity between chunks."""
    features: list[dict] = []
    metadata: list[dict] = []
    special_count = tokenizer.num_special_tokens_to_add(pair=False)
    for sentence_index, sentence in enumerate(sentences):
        tokens = sentence["tokens"]
        tags = sentence["tags"]
        pieces_by_word = [tokenizer.tokenize(token) or [tokenizer.unk_token] for token in tokens]
        start = 0
        while start < len(tokens):
            end = start
            piece_count = 0
            while end < len(tokens) and piece_count + len(pieces_by_word[end]) + special_count <= max_length:
                piece_count += len(pieces_by_word[end])
                end += 1
            if end == start:
                raise ValueError(f"{sentence['id']}: token {tokens[start]!r} exceeds max_length={max_length}")
            if end < len(tokens) and tags[end].startswith("I-"):
                label = tags[end].split("-", 1)[1]
                while end > start and tags[end - 1] in {f"B-{label}", f"I-{label}"}:
                    end -= 1
            if end <= start:
                raise ValueError(f"{sentence['id']}: entity exceeds max_length={max_length}")

            flat_tokens: list[str] = []
            flat_word_ids: list[int] = []
            labels_without_specials: list[int] = []
            for local_word_id, pieces in enumerate(pieces_by_word[start:end]):
                flat_tokens.extend(pieces)
                flat_word_ids.extend([local_word_id] * len(pieces))
                tag = tags[start + local_word_id]
                labels_without_specials.extend([-100 if tag == "IGN" else LABEL2ID[tag]] + [-100] * (len(pieces) - 1))

            token_ids = tokenizer.convert_tokens_to_ids(flat_tokens)
            encoded, special_mask = _encode_single_sequence(tokenizer, token_ids)
            word_ids: list[int | None] = []
            labels: list[int] = []
            flat_index = 0
            for is_special in special_mask:
                if is_special:
                    word_ids.append(None)
                    labels.append(-100)
                else:
                    word_ids.append(flat_word_ids[flat_index])
                    labels.append(labels_without_specials[flat_index])
                    flat_index += 1
            if flat_index != len(flat_tokens) or len(encoded["input_ids"]) > max_length:
                raise ValueError(f"{sentence['id']}: invalid tokenizer alignment")
            features.append({**encoded, "labels": labels})
            metadata.append({
                "sentence_index": sentence_index,
                "word_start": start,
                "word_end": end,
                "word_ids": word_ids,
            })
            start = end
    return features, metadata


def predictions_to_bio(sentences: list[dict], metadata: list[dict], logits) -> list[list[str]]:
    import numpy as np

    predicted_ids = np.argmax(logits, axis=-1)
    output = [["O"] * len(sentence["tokens"]) for sentence in sentences]
    assigned: list[set[int]] = [set() for _ in sentences]
    for chunk_index, chunk in enumerate(metadata):
        sentence_index = chunk["sentence_index"]
        for token_index, local_word_id in enumerate(chunk["word_ids"]):
            if local_word_id is None:
                continue
            global_word_id = chunk["word_start"] + local_word_id
            if global_word_id in assigned[sentence_index]:
                continue
            assigned[sentence_index].add(global_word_id)
            output[sentence_index][global_word_id] = ID2LABEL[int(predicted_ids[chunk_index, token_index])]
    for sentence_index, sentence in enumerate(sentences):
        if len(assigned[sentence_index]) != len(sentence["tokens"]):
            raise ValueError(f"{sentence['id']}: predictions do not cover every source token")
    return [normalize_bio(tags) for tags in output]


def bio_entities(tags: list[str]) -> list[dict]:
    tags = normalize_bio(tags)
    entities: list[dict] = []
    current: dict | None = None
    for index, tag in enumerate(tags):
        if tag == "O":
            if current:
                entities.append(current)
                current = None
            continue
        prefix, label = tag.split("-", 1)
        if prefix == "B" or current is None or current["label"] != label:
            if current:
                entities.append(current)
            current = {"start": index, "end": index + 1, "label": label}
        else:
            current["end"] = index + 1
    if current:
        entities.append(current)
    return entities


def metric(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def exact_counts(gold_sequences: list[list[str]], pred_sequences: list[list[str]]) -> dict:
    counts = {label: Counter(tp=0, fp=0, fn=0) for label in ENTITY_LABELS}
    for gold_tags, pred_tags in zip(gold_sequences, pred_sequences):
        gold = {(item["start"], item["end"], item["label"]) for item in bio_entities(gold_tags)}
        pred = {(item["start"], item["end"], item["label"]) for item in bio_entities(pred_tags)}
        for label in ENTITY_LABELS:
            gold_label = {item for item in gold if item[2] == label}
            pred_label = {item for item in pred if item[2] == label}
            counts[label]["tp"] += len(gold_label & pred_label)
            counts[label]["fp"] += len(pred_label - gold_label)
            counts[label]["fn"] += len(gold_label - pred_label)
    return {label: metric(**counts[label]) for label in ENTITY_LABELS}


def seqeval_exact_metrics(gold_sequences: list[list[str]], pred_sequences: list[list[str]]) -> dict:
    counts = exact_counts(gold_sequences, pred_sequences)
    per_label = {
        label: {
            **counts[label],
            "support": counts[label]["tp"] + counts[label]["fn"],
        }
        for label in ENTITY_LABELS
    }
    totals = {key: sum(per_label[label][key] for label in ENTITY_LABELS) for key in ("tp", "fp", "fn")}
    micro = metric(**totals)

    # seqeval is optional because its legacy setup.py cannot be installed on
    # some newer Python runtimes. The span counter above implements the same
    # strict IOB2 entity-level exact-match definition.
    backend = "internal_strict_iob2"
    try:
        from seqeval.metrics import f1_score, precision_score, recall_score
        from seqeval.scheme import IOB2

        micro.update(
            precision=float(
                precision_score(gold_sequences, pred_sequences, mode="strict", scheme=IOB2, zero_division=0)
            ),
            recall=float(recall_score(gold_sequences, pred_sequences, mode="strict", scheme=IOB2, zero_division=0)),
            f1=float(f1_score(gold_sequences, pred_sequences, mode="strict", scheme=IOB2, zero_division=0)),
        )
        backend = "seqeval_strict_iob2"
    except ImportError:
        pass

    return {
        "micro": micro,
        "macro_f1": sum(per_label[label]["f1"] for label in ENTITY_LABELS) / len(ENTITY_LABELS),
        "per_label": per_label,
        "backend": backend,
    }


def relaxed_metrics(gold_sequences: list[list[str]], pred_sequences: list[list[str]]) -> dict:
    counts = {label: Counter(tp=0, fp=0, fn=0) for label in ENTITY_LABELS}
    for gold_tags, pred_tags in zip(gold_sequences, pred_sequences):
        gold = bio_entities(gold_tags)
        pred = bio_entities(pred_tags)
        candidates: list[tuple[int, int, int]] = []
        for pred_index, predicted in enumerate(pred):
            for gold_index, target in enumerate(gold):
                if predicted["label"] != target["label"]:
                    continue
                overlap = min(predicted["end"], target["end"]) - max(predicted["start"], target["start"])
                if overlap > 0:
                    candidates.append((overlap, pred_index, gold_index))
        used_pred: set[int] = set()
        used_gold: set[int] = set()
        for _, pred_index, gold_index in sorted(candidates, reverse=True):
            if pred_index in used_pred or gold_index in used_gold:
                continue
            used_pred.add(pred_index)
            used_gold.add(gold_index)
            counts[pred[pred_index]["label"]]["tp"] += 1
        for index, entity in enumerate(pred):
            if index not in used_pred:
                counts[entity["label"]]["fp"] += 1
        for index, entity in enumerate(gold):
            if index not in used_gold:
                counts[entity["label"]]["fn"] += 1
    per_label = {label: metric(**counts[label]) for label in ENTITY_LABELS}
    totals = {key: sum(per_label[label][key] for label in ENTITY_LABELS) for key in ("tp", "fp", "fn")}
    return {
        "micro": metric(**totals),
        "macro_f1": sum(per_label[label]["f1"] for label in ENTITY_LABELS) / len(ENTITY_LABELS),
        "per_label": per_label,
        "match": "same label and at least one overlapping source token; one-to-one matching",
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
