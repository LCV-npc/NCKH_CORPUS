#!/usr/bin/env python3
"""Build the five-label ViMedNER gold with diagnostic-only VietBioNER support.

The official ViMedNER test split remains the evaluation gold. VietBioNER is a
partial annotation source after filtering, so it is added to training only and
is never allowed into the five-label test split.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


LABELS = ["DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC", "TREATMENT"]
VIMED_LABELS = {
    "ten_benh": "DISEASE",
    "trieu_chung_benh": "SYMPTOM",
    "nguyen_nhan_benh": "CAUSE",
    "bien_phap_chan_doan": "DIAGNOSTIC",
    "bien_phap_dieu_tri": "TREATMENT",
}
TOKEN_RE = re.compile(r"\w+(?:[-/]\w+)*|[^\w\s]", re.UNICODE)
SENTENCE_RE = re.compile(r".*?(?:[.!?](?=\s|$)|\n{2,}|$)", re.DOTALL)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text).casefold()).strip()


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def detokenize(tokens: list[str]) -> tuple[str, list[tuple[int, int]]]:
    right_attached = {".", ",", ";", ":", "!", "?", "%", ")", "]", "}", "…"}
    left_attached = {"(", "[", "{"}
    chunks: list[str] = []
    offsets: list[tuple[int, int]] = []
    cursor = 0
    previous = ""
    for token in tokens:
        if chunks and token not in right_attached and previous not in left_attached:
            chunks.append(" ")
            cursor += 1
        start = cursor
        chunks.append(token)
        cursor += len(token)
        offsets.append((start, cursor))
        previous = token
    return "".join(chunks), offsets


def read_vimed(path: Path, split: str) -> tuple[list[dict], list[dict]]:
    sentences: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    issues: list[dict] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line:
            if current:
                sentences.append(current)
                current = []
            continue
        parts = line.rsplit(maxsplit=1)
        if len(parts) != 2:
            issues.append({"source": str(path), "line": line_number, "type": "MALFORMED_BIO", "value": raw})
            continue
        current.append((unicodedata.normalize("NFC", parts[0]), parts[1]))
    if current:
        sentences.append(current)

    records: list[dict] = []
    for index, rows in enumerate(sentences):
        tokens = [row[0] for row in rows]
        text, offsets = detokenize(tokens)
        entities: list[dict] = []
        active_label: str | None = None
        active_start = -1
        active_end = -1

        def close() -> None:
            nonlocal active_label, active_start, active_end
            if active_label is not None:
                start = offsets[active_start][0]
                end = offsets[active_end][1]
                entities.append({"text": text[start:end], "label": active_label, "start": start, "end": end})
            active_label = None
            active_start = active_end = -1

        for token_index, (_, tag) in enumerate(rows):
            if tag == "O":
                close()
                continue
            if "-" not in tag:
                close()
                issues.append({"source": str(path), "sentence": index, "type": "INVALID_TAG", "value": tag})
                continue
            prefix, source_label = tag.split("-", 1)
            label = VIMED_LABELS.get(source_label)
            if label is None:
                close()
                issues.append({"source": str(path), "sentence": index, "type": "UNKNOWN_LABEL", "value": tag})
                continue
            if prefix == "B" or label != active_label:
                if prefix == "I" and label != active_label:
                    issues.append({"source": str(path), "sentence": index, "type": "ORPHAN_I", "value": tag})
                close()
                active_label = label
                active_start = token_index
            active_end = token_index
        close()
        records.append({
            "id": f"vimedner:{split}:{index:06d}",
            "source": "ViMedNER",
            "source_split": split,
            "input_text": text,
            "entities": entities,
            "annotation_scope": LABELS,
            "is_partial_annotation": False,
            "checksum": checksum(normalize(text)),
        })
    return records, issues


def parse_brat_ann(path: Path, text: str) -> tuple[list[dict], list[dict]]:
    entities: list[dict] = []
    issues: list[dict] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw.startswith("T"):
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            issues.append({"source": str(path), "line": line_number, "type": "MALFORMED_BRAT", "value": raw})
            continue
        descriptor = parts[1].split(maxsplit=1)
        if len(descriptor) != 2 or descriptor[0] != "DiagnosticProcedure":
            continue
        segments = descriptor[1].split(";")
        if len(segments) > 1:
            # A flat BIO sequence cannot faithfully represent discontinuous BRAT
            # entities. Keep the audit trail and exclude them from auto-conversion.
            issues.append({"source": str(path), "line": line_number, "type": "DISCONTINUOUS_EXCLUDED", "value": parts[0]})
            continue
        for segment in segments:
            try:
                start_text, end_text = segment.split()
                start, end = int(start_text), int(end_text)
            except ValueError:
                issues.append({"source": str(path), "line": line_number, "type": "INVALID_OFFSET", "value": segment})
                continue
            expected = parts[2]
            surface = text[start:end] if 0 <= start < end <= len(text) else ""
            if surface != expected:
                window_start = max(0, start - 200)
                window_end = min(len(text), max(end, start) + 200)
                candidates: list[int] = []
                cursor = text.find(expected, window_start, window_end)
                while cursor >= 0:
                    candidates.append(cursor)
                    cursor = text.find(expected, cursor + 1, window_end)
                if len(candidates) != 1:
                    global_candidates = [match.start() for match in re.finditer(re.escape(expected), text)]
                    candidates = global_candidates if len(global_candidates) == 1 else []
                if len(candidates) == 1:
                    original = [start, end]
                    start = candidates[0]
                    end = start + len(expected)
                    surface = expected
                    issues.append({"source": str(path), "line": line_number, "type": "OFFSET_REALIGNED", "value": parts[0], "original": original, "corrected": [start, end]})
                else:
                    issue_type = "OFFSET_OUT_OF_RANGE" if not 0 <= start < end <= len(text) else "BRAT_TEXT_MISMATCH"
                    issues.append({"source": str(path), "line": line_number, "type": issue_type, "value": parts[0], "expected": expected, "actual": surface})
                    continue
            entities.append({"text": surface, "label": "DIAGNOSTIC", "start": start, "end": end, "brat_id": parts[0]})
    return entities, issues


def sentence_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for match in SENTENCE_RE.finditer(text):
        start, end = match.span()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            ranges.append((start, end))
    return ranges


def select_non_overlapping(entities: list[dict]) -> tuple[list[dict], list[dict]]:
    selected: list[dict] = []
    rejected: list[dict] = []
    for item in sorted(entities, key=lambda e: (-(e["end"] - e["start"]), e["start"], e["end"])):
        if any(item["start"] < other["end"] and other["start"] < item["end"] for other in selected):
            rejected.append(item)
        else:
            selected.append(item)
    return sorted(selected, key=lambda e: (e["start"], e["end"])), rejected


def read_vietbio(brat_dir: Path) -> tuple[list[dict], list[dict]]:
    records: list[dict] = []
    issues: list[dict] = []
    canonical = brat_dir / "Annotator_A"
    for ann_path in sorted(canonical.glob("*.ann"), key=lambda p: p.stem):
        if ann_path.stem.startswith("dup_"):
            continue
        txt_path = ann_path.with_suffix(".txt")
        if not txt_path.exists():
            issues.append({"source": str(ann_path), "type": "MISSING_TEXT"})
            continue
        # BRAT exported offsets use LF line endings. Preserve Unicode codepoints,
        # but normalize Windows newlines before applying the annotation offsets.
        text = txt_path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        document_entities, ann_issues = parse_brat_ann(ann_path, text)
        issues.extend(ann_issues)
        if not document_entities:
            continue
        ranges = sentence_ranges(text)
        for sent_index, (sent_start, sent_end) in enumerate(ranges):
            contained = [entity for entity in document_entities if sent_start <= entity["start"] and entity["end"] <= sent_end]
            if not contained:
                continue
            local = [{**entity, "start": entity["start"] - sent_start, "end": entity["end"] - sent_start} for entity in contained]
            local, rejected = select_non_overlapping(local)
            for entity in rejected:
                issues.append({"source": str(ann_path), "type": "OVERLAP_REJECTED", "entity": entity})
            sentence = text[sent_start:sent_end]
            for entity in local:
                entity.pop("brat_id", None)
                if sentence[entity["start"]:entity["end"]] != entity["text"]:
                    raise ValueError(f"Offset mismatch in {ann_path}:{sent_index}")
            records.append({
                "id": f"vietbioner:{ann_path.stem}:{sent_index:04d}",
                "source": "VietBioNER",
                "source_split": "diagnostic_supplement",
                "input_text": sentence,
                "entities": local,
                "annotation_scope": ["DIAGNOSTIC"],
                "is_partial_annotation": True,
                "checksum": checksum(normalize(sentence)),
            })
    return records, issues


def deduplicate(test: list[dict], train: list[dict], supplement: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    seen: set[str] = set()
    removed: list[dict] = []
    outputs: list[list[dict]] = []
    for split_name, rows in (("test", test), ("ann_train", train), ("diagnostic_supplement", supplement)):
        kept: list[dict] = []
        for row in rows:
            key = row["checksum"]
            if key in seen:
                removed.append({"id": row["id"], "split": split_name, "reason": "DUPLICATE_NORMALIZED_TEXT", "checksum": key})
                continue
            seen.add(key)
            kept.append(row)
        outputs.append(kept)
    return outputs[0], outputs[1], outputs[2], removed


def entity_counts(rows: Iterable[dict]) -> dict[str, int]:
    counts = Counter(entity["label"] for row in rows for entity in row["entities"])
    return {label: counts[label] for label in LABELS}


def tokenize_with_offsets(text: str) -> list[tuple[str, int, int]]:
    return [(match.group(), match.start(), match.end()) for match in TOKEN_RE.finditer(text)]


def to_bio(row: dict, partial_unknown: bool = False) -> tuple[str, list[dict]]:
    tokens = tokenize_with_offsets(row["input_text"])
    entities = sorted(row["entities"], key=lambda e: (e["start"], e["end"]))
    lines: list[str] = []
    issues: list[dict] = []
    covered_entities: set[int] = set()
    for token, start, end in tokens:
        containing = [(index, entity) for index, entity in enumerate(entities) if entity["start"] <= start and end <= entity["end"]]
        if len(containing) > 1:
            issues.append({"id": row["id"], "type": "TOKEN_MULTI_ENTITY", "token": token, "start": start, "end": end})
        if containing:
            index, entity = containing[0]
            prefix = "B" if start == entity["start"] else "I"
            tag = f"{prefix}-{entity['label']}"
            covered_entities.add(index)
        else:
            tag = "IGN" if partial_unknown else "O"
        lines.append(f"{token} {tag}")
    for index, entity in enumerate(entities):
        if index not in covered_entities:
            issues.append({"id": row["id"], "type": "ENTITY_NOT_TOKEN_ALIGNED", "entity": entity})
    return "\n".join(lines) + "\n\n", issues


def write_bio(path: Path, rows: list[dict], partial_unknown: bool = False) -> list[dict]:
    blocks: list[str] = []
    issues: list[dict] = []
    for row in rows:
        block, row_issues = to_bio(row, partial_unknown=partial_unknown)
        blocks.append(block)
        issues.extend(row_issues)
    path.write_text("".join(blocks), encoding="utf-8")
    return issues


def validate(rows: Iterable[dict]) -> None:
    for row in rows:
        previous_end = -1
        for entity in row["entities"]:
            if entity["label"] not in LABELS:
                raise ValueError(f"{row['id']}: invalid label {entity['label']}")
            if row["input_text"][entity["start"]:entity["end"]] != entity["text"]:
                raise ValueError(f"{row['id']}: span mismatch")
            if entity["start"] < previous_end:
                raise ValueError(f"{row['id']}: overlapping entities")
            previous_end = entity["end"]


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vimed", type=Path, default=root / "ViMedNer" / "data")
    parser.add_argument("--vietbio-brat", type=Path, default=root / "VietBioNER" / "data_brat")
    parser.add_argument("--output", type=Path, default=root / "ner_finetuning" / "processed" / "medical_gold_v1")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    all_vimed: dict[str, list[dict]] = {}
    issues: list[dict] = []
    for split in ("train", "dev", "test"):
        rows, split_issues = read_vimed(args.vimed / f"{split}.txt", split)
        all_vimed[split] = rows
        issues.extend(split_issues)
    supplement, brat_issues = read_vietbio(args.vietbio_brat)
    issues.extend(brat_issues)

    test, ann_train, supplement, duplicates = deduplicate(
        all_vimed["test"], all_vimed["train"] + all_vimed["dev"], supplement
    )
    validate(test + ann_train + supplement)
    merged_train = ann_train + supplement
    complete_train = [row for row in ann_train if row["source_split"] == "train"]
    complete_dev = [row for row in ann_train if row["source_split"] == "dev"]

    write_jsonl(args.output / "test.internal.jsonl", test)
    write_jsonl(args.output / "ann_train.complete.internal.jsonl", ann_train)
    write_jsonl(args.output / "train.complete.internal.jsonl", complete_train)
    write_jsonl(args.output / "dev.complete.internal.jsonl", complete_dev)
    write_jsonl(args.output / "diagnostic_supplement.partial.internal.jsonl", supplement)
    write_jsonl(args.output / "ann_train.merged.internal.jsonl", merged_train)
    write_jsonl(args.output / "conversion_issues.jsonl", issues)
    write_jsonl(args.output / "duplicates_removed.jsonl", duplicates)

    bio_issues: list[dict] = []
    bio_issues.extend(write_bio(args.output / "test.txt", test))
    bio_issues.extend(write_bio(args.output / "ann_train.complete.txt", ann_train))
    bio_issues.extend(write_bio(args.output / "train.txt", complete_train))
    bio_issues.extend(write_bio(args.output / "dev.txt", complete_dev))
    # IGN is intentionally not O: it marks tokens outside DIAGNOSTIC as unknown.
    bio_issues.extend(write_bio(args.output / "diagnostic_supplement.partial.txt", supplement, partial_unknown=True))
    write_jsonl(args.output / "bio_conversion_issues.jsonl", bio_issues)
    (args.output / "labels.txt").write_text(
        "O\n" + "\n".join(f"{prefix}-{label}" for label in LABELS for prefix in ("B", "I")) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "dataset": "ViMedNER gold + VietBioNER diagnostic-only training supplement",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "schema": LABELS,
        "split_policy": "ViMedNER official test is frozen; train+dev form ann_train; VietBioNER partial labels are train-only",
        "test_is_complete_five_label_gold": True,
        "vietbioner_in_test": False,
        "counts": {
            "test": {"records": len(test), "entities": entity_counts(test)},
            "ann_train_complete": {"records": len(ann_train), "entities": entity_counts(ann_train)},
            "train_complete": {"records": len(complete_train), "entities": entity_counts(complete_train)},
            "dev_complete": {"records": len(complete_dev), "entities": entity_counts(complete_dev)},
            "diagnostic_supplement_partial": {"records": len(supplement), "entities": entity_counts(supplement)},
            "ann_train_merged": {"records": len(merged_train), "entities": entity_counts(merged_train)},
        },
        "duplicates_removed": len(duplicates),
        "conversion_issues": len(issues),
        "bio_conversion_issues": len(bio_issues),
        "notes": [
            "Only DiagnosticProcedure from VietBioNER is retained and mapped to DIAGNOSTIC.",
            "Annotator_A is canonical; dup_* IAA files and Annotator_B are excluded.",
            "VietBioNER non-DIAGNOSTIC tokens use IGN, not O, because they are not exhaustively annotated.",
            "Use ann_train.complete.txt directly with the original ViMedNER trainer.",
            "Using the partial supplement requires a loss mask that maps IGN to -100.",
        ],
    }
    manifest["test_checksum"] = checksum((args.output / "test.internal.jsonl").read_text(encoding="utf-8"))
    manifest["train_checksum"] = checksum((args.output / "ann_train.merged.internal.jsonl").read_text(encoding="utf-8"))
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
