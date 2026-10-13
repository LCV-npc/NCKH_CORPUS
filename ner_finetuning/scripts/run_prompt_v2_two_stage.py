#!/usr/bin/env python3
"""Run Prompt V2 Extractor -> Verifier on a frozen Dev subset."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from run_medical_three_systems import call_gemini, load_env_file, read_jsonl  # noqa: E402


LABELS = ["DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC", "TREATMENT"]
ENTITY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "entities": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING"},
                    "label": {"type": "STRING", "enum": LABELS},
                    "start_offset": {"type": "INTEGER"},
                    "end_offset": {"type": "INTEGER"},
                },
                "required": ["text", "label", "start_offset", "end_offset"],
            },
        }
    },
    "required": ["entities"],
}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_successful(path: Path) -> tuple[list[dict], set[str]]:
    if not path.exists():
        return [], set()
    successful: list[dict] = []
    ids: set[str] = set()
    for row in read_jsonl(path):
        if not row.get("error"):
            sample_id = str(row.get("id"))
            if sample_id not in ids:
                successful.append(row)
                ids.add(sample_id)
    write_jsonl(path, successful)
    return successful, ids


def occurrences(text: str, surface: str) -> list[int]:
    result: list[int] = []
    cursor = text.find(surface)
    while cursor >= 0:
        result.append(cursor)
        cursor = text.find(surface, cursor + 1)
    return result


def canonicalize(text: str, raw_entities: list[dict], stage: str) -> tuple[list[dict], list[dict]]:
    entities: list[dict] = []
    issues: list[dict] = []
    seen: set[tuple] = set()
    for raw in raw_entities:
        surface = str(raw.get("text", ""))
        label = str(raw.get("label", ""))
        if not surface or label not in LABELS:
            issues.append({"stage": stage, "type": "INVALID_ENTITY", "entity": raw})
            continue
        start = raw.get("start_offset", raw.get("start"))
        end = raw.get("end_offset", raw.get("end"))
        valid = isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text) and text[start:end] == surface
        if not valid:
            hits = occurrences(text, surface)
            if len(hits) == 1:
                original = [start, end]
                start = hits[0]
                end = start + len(surface)
                issues.append({"stage": stage, "type": "OFFSET_CORRECTED", "entity": raw, "original": original, "corrected": [start, end]})
            else:
                issues.append({"stage": stage, "type": "NON_VERBATIM_OR_AMBIGUOUS", "entity": raw, "occurrences": hits})
                continue
        key = (start, end, label)
        if key in seen:
            issues.append({"stage": stage, "type": "DUPLICATE", "entity": raw})
            continue
        seen.add(key)
        entities.append({"text": surface, "label": label, "start_offset": start, "end_offset": end, "start": start, "end": end})
    entities.sort(key=lambda item: (item["start"], item["end"], item["label"]))
    return entities, issues


def enforce_flat(entities: list[dict]) -> tuple[list[dict], list[dict]]:
    selected: list[dict] = []
    rejected: list[dict] = []
    # V2 explicitly prefers the smallest complete noun phrase.
    for entity in sorted(entities, key=lambda item: (item["end"] - item["start"], item["start"], item["label"])):
        if any(entity["start"] < old["end"] and old["start"] < entity["end"] for old in selected):
            rejected.append(entity)
        else:
            selected.append(entity)
    return sorted(selected, key=lambda item: (item["start"], item["end"], item["label"])), rejected


def verifier_input(text: str, candidates: list[dict]) -> str:
    compact = [{key: item[key] for key in ("text", "label", "start_offset", "end_offset")} for item in candidates]
    return "Câu gốc:\n" + text + "\n\nỨng viên Extractor:\n" + json.dumps({"entities": compact}, ensure_ascii=False, separators=(",", ":"))


def final_entities(entities: list[dict]) -> list[dict]:
    return [{key: item[key] for key in ("text", "label", "start_offset", "end_offset", "start", "end")} for item in entities]


def run(args: argparse.Namespace) -> Path:
    root = Path(__file__).resolve().parents[2]
    load_env_file(root / ".env")
    load_env_file(root / "backend" / ".env")
    api_key = os.getenv(args.api_key_env, "")
    if not api_key:
        raise ValueError(f"Set {args.api_key_env} before running Prompt V2")

    records = read_jsonl(args.input)
    if args.limit > 0:
        records = records[:args.limit]
    extractor_prompt = args.extractor_prompt.read_text(encoding="utf-8-sig").strip()
    verifier_prompt = args.verifier_prompt.read_text(encoding="utf-8-sig").strip()
    run_dir = args.output_root / args.run_id
    if run_dir.exists() and not args.resume:
        raise FileExistsError(f"Run already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)

    prediction_path = run_dir / "predictions.jsonl"
    extractor_path = run_dir / "extractor.jsonl"
    removed_path = run_dir / "verifier_removed.jsonl"
    issue_path = run_dir / "stage_issues.jsonl"
    prediction_rows, done = load_successful(prediction_path)
    extractor_rows = read_jsonl(extractor_path) if extractor_path.exists() else []
    removed_rows = read_jsonl(removed_path) if removed_path.exists() else []
    issue_rows = read_jsonl(issue_path) if issue_path.exists() else []

    for number, record in enumerate(records, 1):
        sample_id = str(record["id"])
        if sample_id in done:
            continue
        text = str(record["input_text"])
        started = time.perf_counter()
        error = None
        extractor_entities: list[dict] = []
        verified_entities: list[dict] = []
        try:
            extracted = call_gemini(
                args.model, api_key, extractor_prompt, "Câu gốc:\n" + text,
                args.timeout, args.retries, ENTITY_SCHEMA,
            )
            extractor_entities, extractor_issues = canonicalize(text, extracted.get("entities", []), "extractor")
            issue_rows.extend({"id": sample_id, **item} for item in extractor_issues)
            extractor_rows.append({"id": sample_id, "input_text": text, "entities": final_entities(extractor_entities)})

            verified = call_gemini(
                args.model, api_key, verifier_prompt, verifier_input(text, extractor_entities),
                args.timeout, args.retries, ENTITY_SCHEMA,
            )
            verified_entities, verifier_issues = canonicalize(text, verified.get("entities", []), "verifier")
            issue_rows.extend({"id": sample_id, **item} for item in verifier_issues)
            candidate_spans = {(item["text"], item["start"], item["end"]) for item in extractor_entities}
            verified_entities = [item for item in verified_entities if (item["text"], item["start"], item["end"]) in candidate_spans]
            verified_entities, overlap_rejected = enforce_flat(verified_entities)
            issue_rows.extend({"id": sample_id, "stage": "verifier", "type": "OVERLAP_REJECTED", "entity": item} for item in overlap_rejected)

            kept_spans = {(item["text"], item["start"], item["end"]) for item in verified_entities}
            label_by_span = {(item["text"], item["start"], item["end"]): item["label"] for item in verified_entities}
            for candidate in extractor_entities:
                span_key = (candidate["text"], candidate["start"], candidate["end"])
                if span_key not in kept_spans:
                    removed_rows.append({"id": sample_id, "input_text": text, "entity": candidate, "decision": "VERIFIER_REJECTED"})
                elif label_by_span[span_key] != candidate["label"]:
                    removed_rows.append({"id": sample_id, "input_text": text, "entity": candidate, "new_label": label_by_span[span_key], "decision": "LABEL_CHANGED"})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        row = {
            "id": sample_id,
            "system": "prompt_v2_extractor_verifier",
            "model": args.model,
            "input_text": text,
            "entities": final_entities(verified_entities),
            "extractor_entity_count": len(extractor_entities),
            "verifier_entity_count": len(verified_entities),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": error,
        }
        prediction_rows.append(row)
        write_jsonl(prediction_path, prediction_rows)
        write_jsonl(extractor_path, extractor_rows)
        write_jsonl(removed_path, removed_rows)
        write_jsonl(issue_path, issue_rows)
        print(f"[prompt_v2] {number}/{len(records)} extractor={len(extractor_entities)} verifier={len(verified_entities)} error={error or '-'}", flush=True)
        if args.delay:
            time.sleep(args.delay)

    previous_manifest = {}
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    notes = list(previous_manifest.get("notes", []))
    if args.note and args.note not in notes:
        notes.append(args.note)
    manifest = {
        "run_id": args.run_id,
        "created_at": previous_manifest.get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "system": "prompt_v2_extractor_verifier",
        "model": args.model,
        "input": str(args.input.resolve()),
        "input_records": len(records),
        "input_checksum": digest(args.input.read_text(encoding="utf-8")),
        "extractor_prompt_checksum": digest(extractor_prompt),
        "verifier_prompt_checksum": digest(verifier_prompt),
        "parent_checkpoint": args.parent_checkpoint,
        "notes": notes,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (run_dir / "extractor_prompt.txt").write_text(extractor_prompt, encoding="utf-8")
    (run_dir / "verifier_prompt.txt").write_text(verifier_prompt, encoding="utf-8")
    return run_dir


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=root / "ner_finetuning" / "processed" / "medical_gold_v1" / "dev.complete.internal.jsonl")
    parser.add_argument("--extractor-prompt", type=Path, default=root / "ner_finetuning" / "prompts" / "extractor_v2_vi.txt")
    parser.add_argument("--verifier-prompt", type=Path, default=root / "ner_finetuning" / "prompts" / "verifier_v2_vi.txt")
    parser.add_argument("--output-root", type=Path, default=root / "ner_finetuning" / "experiment_runs")
    parser.add_argument("--run-id", default="dev-prompt-v2-100-v1")
    parser.add_argument("--model", default=os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"))
    parser.add_argument("--api-key-env", default="GEMINI_API_KEY")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--parent-checkpoint", default="dev-pilot-100-v1/prompt_only")
    parser.add_argument("--note", default="Prompt V2 two-stage precision experiment on Dev")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = run(args)
    print(f"Run checkpoint: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
