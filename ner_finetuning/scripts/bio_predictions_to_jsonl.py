#!/usr/bin/env python3
"""Convert ViMedNER/PhoBERT token predictions into span-offset JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ALIASES = {"DIAGNOSTIC_PROCEDURE": "DIAGNOSTIC"}


def read_sentences(path: Path) -> list[list[tuple[str, str]]]:
    sentences: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip():
            if current:
                sentences.append(current); current = []
            continue
        parts = raw.rsplit(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"Malformed prediction line: {raw!r}")
        current.append((parts[0], parts[1]))
    if current:
        sentences.append(current)
    return sentences


def offsets(text: str, tokens: list[str]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    cursor = 0
    for token in tokens:
        start = text.find(token, cursor)
        if start < 0:
            raise ValueError(f"Cannot align token {token!r} after offset {cursor} in {text!r}")
        end = start + len(token)
        result.append((start, end))
        cursor = end
    return result


def decode(text: str, rows: list[tuple[str, str]]) -> list[dict]:
    token_offsets = offsets(text, [token for token, _ in rows])
    entities: list[dict] = []
    active_label = None
    active_start = active_end = -1

    def close() -> None:
        nonlocal active_label, active_start, active_end
        if active_label is not None:
            entities.append({"text": text[active_start:active_end], "label": active_label, "start": active_start, "end": active_end})
        active_label = None

    for (_, tag), (start, end) in zip(rows, token_offsets):
        if tag == "O":
            close(); continue
        if "-" not in tag:
            close(); continue
        prefix, label = tag.split("-", 1)
        label = ALIASES.get(label, label)
        if prefix == "B" or label != active_label:
            close(); active_label = label; active_start = start
        active_end = end
    close()
    return entities


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    gold = [json.loads(line) for line in args.gold.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    sentences = read_sentences(args.predictions)
    if len(sentences) != len(gold):
        raise ValueError(f"Sentence count mismatch: predictions={len(sentences)}, gold={len(gold)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for record, rows in zip(gold, sentences):
            output = {"id": record["id"], "input_text": record["input_text"], "entities": decode(record["input_text"], rows)}
            handle.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"Wrote {len(gold)} records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
