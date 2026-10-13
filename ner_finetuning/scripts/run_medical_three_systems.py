#!/usr/bin/env python3
"""Run/checkpoint the three-system Vietnamese medical NER experiment.

System 2 accepts predictions from an independently trained PhoBERT/XLM-R model;
systems 1 and 3 call Gemini. This keeps training and LLM inference independent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


LABELS = ["DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC", "TREATMENT"]
SCHEMA = {"type": "OBJECT", "properties": {"entities": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}, "label": {"type": "STRING", "enum": LABELS}}, "required": ["text", "label"]}}}, "required": ["entities"]}
BASE_PROMPT = """Bạn là hệ thống NER y khoa tiếng Việt. Trích xuất đúng chuỗi xuất hiện nguyên văn theo năm nhãn DISEASE, SYMPTOM, CAUSE, DIAGNOSTIC, TREATMENT. Không suy diễn thực thể không có trong văn bản. Trả về duy nhất JSON dạng {\"entities\":[{\"text\":\"...\",\"label\":\"DISEASE\"}]} và không thêm giải thích."""


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def completed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    result: set[str] = set()
    successful_rows: list[dict] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            row = json.loads(line)
            if not row.get("error"):
                result.add(str(row["id"]))
                successful_rows.append(row)
        except (json.JSONDecodeError, KeyError):
            pass
    # Failed rows are removed before retry so each sample has one final record.
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in successful_rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return result


class Dictionary:
    def __init__(self, path: Path) -> None:
        self.terms: list[tuple[str, str]] = []
        for row in json.loads(path.read_text(encoding="utf-8-sig")):
            term = str(row.get("TÊN BỆNH") or row.get("ten_benh") or "").strip()
            if len(term) >= 2:
                label = "SYMPTOM" if str(row.get("ENTITY_TYPE", "")).upper() == "SYMPTOM" else "DISEASE"
                self.terms.append((term, label))
        self.terms.sort(key=lambda item: len(item[0]), reverse=True)

    def find(self, text: str, limit: int = 40) -> list[dict]:
        folded = text.casefold()
        candidates: list[dict] = []
        for term, label in self.terms:
            pattern = re.compile(rf"(?<!\w){re.escape(term.casefold())}(?!\w)")
            for match in pattern.finditer(folded):
                item = {"text": text[match.start():match.end()], "label": label, "start": match.start(), "end": match.end()}
                if not any(item["start"] < old["end"] and old["start"] < item["end"] for old in candidates):
                    candidates.append(item)
                    if len(candidates) >= limit:
                        return sorted(candidates, key=lambda value: value["start"])
        return sorted(candidates, key=lambda value: value["start"])


def fewshot_block(train: list[dict]) -> tuple[str, list[str]]:
    chosen: list[dict] = []
    covered: set[str] = set()
    for row in train:
        labels = {entity["label"] for entity in row["entities"]}
        if labels - covered:
            chosen.append(row)
            covered.update(labels)
        if covered == set(LABELS):
            break
    examples: list[str] = []
    for row in chosen:
        output = {"entities": [{"text": entity["text"], "label": entity["label"]} for entity in row["entities"]]}
        examples.append("Văn bản: " + row["input_text"] + "\nKết quả: " + json.dumps(output, ensure_ascii=False, separators=(",", ":")))
    return "\n\nVí dụ Gold:\n" + "\n\n".join(examples), [row["id"] for row in chosen]


def call_gemini(model: str, api_key: str, prompt: str, user_text: str, timeout: int, retries: int, response_schema: dict | None = None) -> dict:
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {"systemInstruction": {"parts": [{"text": prompt}]}, "contents": [{"role": "user", "parts": [{"text": user_text}]}], "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseSchema": response_schema or SCHEMA}}
    request = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json; charset=utf-8", "x-goog-api-key": api_key}, method="POST")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
            raw = "".join(part.get("text", "") for part in body["candidates"][0]["content"]["parts"])
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
            result = json.loads(raw)
            if not isinstance(result.get("entities"), list):
                raise ValueError("Missing entities array")
            return result
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(8, 2**attempt))
    raise RuntimeError(str(last_error))


def run_llm(name: str, records: list[dict], output: Path, model: str, api_key: str, prompt: str, dictionary: Dictionary | None, delay: float, timeout: int, retries: int) -> None:
    done = completed(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8", newline="\n") as handle:
        for index, row in enumerate(records, 1):
            if row["id"] in done:
                continue
            candidates = dictionary.find(row["input_text"]) if dictionary is not None else []
            user = f"Văn bản: {row['input_text']}"
            if dictionary is not None:
                hint = json.dumps([{"text": item["text"], "label": item["label"]} for item in candidates], ensure_ascii=False, separators=(",", ":"))
                user += f"\nỨng viên từ điển (chỉ là gợi ý): {hint}"
            started = time.perf_counter()
            error = None
            try:
                result = call_gemini(model, api_key, prompt, user, timeout, retries)
            except Exception as exc:
                result = {"entities": []}
                error = f"{type(exc).__name__}: {exc}"
            record = {"id": row["id"], "system": name, "input_text": row["input_text"], "entities": result["entities"], "dictionary_candidates": candidates, "latency_ms": round((time.perf_counter()-started)*1000, 2), "error": error}
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            print(f"[{name}] {index}/{len(records)} error={error or '-'}")
            if delay:
                time.sleep(delay)


def validate_external_predictions(source: Path, test: list[dict], output: Path) -> None:
    rows = read_jsonl(source)
    expected = {str(row["id"]) for row in test}
    actual = {str(row.get("id")) for row in rows}
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise ValueError(f"Fine-tuned predictions ID mismatch: missing={len(missing)}, extra={len(extra)}")
    shutil.copyfile(source, output)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-dir", type=Path, default=root / "ner_finetuning" / "processed" / "medical_gold_v1")
    parser.add_argument("--input", type=Path, help="Input internal JSONL; defaults to gold-dir/test.internal.jsonl")
    parser.add_argument("--dictionary", type=Path, default=root / "data" / "icd10_dictionary.json")
    parser.add_argument("--output-root", type=Path, default=root / "ner_finetuning" / "experiment_runs")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--systems", default="prompt_only,prompt_dictionary,fine_tuned,prompt_dictionary_gold")
    parser.add_argument("--fine-tuned-predictions", type=Path)
    parser.add_argument("--model", default=os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"))
    parser.add_argument("--api-key-env", default="GEMINI_API_KEY")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--note", default="")
    parser.add_argument("--resume", action="store_true", help="Resume an existing run and retry failed records")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[2]
    load_env_file(root / ".env")
    load_env_file(root / "backend" / ".env")
    systems = {item.strip() for item in args.systems.split(",") if item.strip()}
    allowed = {"prompt_only", "prompt_dictionary", "fine_tuned", "prompt_dictionary_gold"}
    if not systems or systems - allowed:
        raise ValueError(f"--systems must be a subset of {sorted(allowed)}")
    input_path = args.input or (args.gold_dir / "test.internal.jsonl")
    test = read_jsonl(input_path)
    train = read_jsonl(args.gold_dir / "ann_train.complete.internal.jsonl")
    if args.limit:
        test = test[:args.limit]
    run_dir = args.output_root / args.run_id
    if run_dir.exists() and not args.resume:
        raise FileExistsError(f"Run already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    previous_manifest: dict = {}
    manifest_path = run_dir / "manifest.json"
    if args.resume and manifest_path.exists():
        previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    prompt = BASE_PROMPT
    fewshot, fewshot_ids = fewshot_block(train)
    dictionary = Dictionary(args.dictionary)
    api_key = os.getenv(args.api_key_env, "")
    if systems & {"prompt_only", "prompt_dictionary", "prompt_dictionary_gold"} and not api_key:
        raise ValueError(f"Set {args.api_key_env} before running Gemini systems")
    if "prompt_only" in systems:
        run_llm("prompt_only", test, run_dir / "prompt_only.jsonl", args.model, api_key, prompt, None, args.delay, args.timeout, args.retries)
    if "prompt_dictionary" in systems:
        run_llm("prompt_dictionary", test, run_dir / "prompt_dictionary.jsonl", args.model, api_key, prompt, dictionary, args.delay, args.timeout, args.retries)
    if "fine_tuned" in systems:
        if not args.fine_tuned_predictions:
            raise ValueError("--fine-tuned-predictions is required for the independently trained model")
        validate_external_predictions(args.fine_tuned_predictions, test, run_dir / "fine_tuned.jsonl")
    if "prompt_dictionary_gold" in systems:
        run_llm("prompt_dictionary_gold", test, run_dir / "prompt_dictionary_gold.jsonl", args.model, api_key, prompt + fewshot, dictionary, args.delay, args.timeout, args.retries)
    notes = list(previous_manifest.get("notes", []))
    previous_note = previous_manifest.get("note")
    if previous_note and previous_note not in notes:
        notes.append(previous_note)
    if args.note and args.note not in notes:
        notes.append(args.note)
    manifest = {"run_id": args.run_id, "created_at": previous_manifest.get("created_at", datetime.now(timezone.utc).isoformat()), "updated_at": datetime.now(timezone.utc).isoformat(), "systems": sorted(set(previous_manifest.get("systems", [])) | systems), "input": str(input_path.resolve()), "input_records": len(test), "input_checksum": digest(input_path.read_text(encoding="utf-8")), "model": args.model, "dictionary": str(args.dictionary.resolve()), "dictionary_checksum": digest(args.dictionary.read_text(encoding="utf-8-sig")), "fewshot_ids": fewshot_ids, "prompt_checksum": digest(prompt), "notes": notes}
    (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (run_dir / "prompt_with_gold_fewshot.txt").write_text(prompt + fewshot, encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Run checkpoint: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
