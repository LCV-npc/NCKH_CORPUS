#!/usr/bin/env python3
"""Run the 2x2 Gemini NER experiment: base/tuned model x dictionary off/on."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path


LABELS = ["DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC_PROCEDURE", "TREATMENT"]
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "entities": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING"},
                    "label": {"type": "STRING", "enum": LABELS},
                },
                "required": ["text", "label"],
            },
        }
    },
    "required": ["entities"],
}


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


class DictionaryMatcher:
    END = "__END__"

    def __init__(self, records: list[dict]) -> None:
        self.root: dict = {}
        for record in records:
            term = str(record.get("TÊN BỆNH") or record.get("ten_benh") or "").strip()
            if len(term) < 2:
                continue
            label = "SYMPTOM" if str(record.get("ENTITY_TYPE", "")).upper() == "SYMPTOM" else "DISEASE"
            node = self.root
            for character in normalize(term):
                node = node.setdefault(character, {})
            node[self.END] = label

    @staticmethod
    def _boundary(text: str, index: int) -> bool:
        return index < 0 or index >= len(text) or not (text[index].isalnum() or text[index] == "_")

    def find(self, text: str, limit: int = 40) -> list[dict]:
        normalized = normalize(text)
        candidates: list[dict] = []
        for start in range(len(normalized)):
            if not self._boundary(normalized, start - 1):
                continue
            node = self.root
            end = start
            while end < len(normalized) and normalized[end] in node:
                node = node[normalized[end]]
                end += 1
                if self.END in node and self._boundary(normalized, end):
                    candidates.append(
                        {
                            "text": text[start:end],
                            "label": node[self.END],
                            "start": start,
                            "end": end,
                        }
                    )
        # Longest match wins on overlaps; output remains in document order.
        selected: list[dict] = []
        for candidate in sorted(candidates, key=lambda item: (-(item["end"] - item["start"]), item["start"])):
            if any(candidate["start"] < item["end"] and item["start"] < candidate["end"] for item in selected):
                continue
            selected.append(candidate)
        selected.sort(key=lambda item: item["start"])
        return selected[:limit]


def load_matcher(path: Path) -> DictionaryMatcher:
    return DictionaryMatcher(json.loads(path.read_text(encoding="utf-8-sig")))


def load_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def decode_response(payload: dict) -> dict:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini response has no candidate")
    parts = candidates[0].get("content", {}).get("parts", [])
    raw = "".join(str(part.get("text", "")) for part in parts).strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw).strip()
    result = json.loads(raw)
    if not isinstance(result, dict) or not isinstance(result.get("entities"), list):
        raise ValueError("Gemini response does not contain an entities array")
    return result


class GeminiClient:
    def __init__(
        self,
        provider: str,
        project: str | None,
        location: str,
        api_key: str | None,
        access_token: str | None,
        timeout: int,
        retries: int,
    ) -> None:
        self.provider = provider
        self.project = project
        self.location = location
        self.api_key = api_key
        self.access_token = access_token
        self.timeout = timeout
        self.retries = retries
        if provider == "gemini-api" and not api_key:
            raise ValueError("Set GEMINI_API_KEY or pass --api-key-env")
        if provider == "vertex" and (not project or not access_token):
            raise ValueError("Vertex requires --project and GOOGLE_OAUTH_ACCESS_TOKEN")

    def endpoint(self, model: str) -> str:
        if self.provider == "gemini-api":
            return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        if model.startswith("projects/"):
            resource = model
        elif model.startswith("endpoints/"):
            resource = f"projects/{self.project}/locations/{self.location}/{model}"
        else:
            resource = f"projects/{self.project}/locations/{self.location}/publishers/google/models/{model}"
        host = "aiplatform.googleapis.com" if self.location == "global" else f"{self.location}-aiplatform.googleapis.com"
        return f"https://{host}/v1/{resource}:generateContent"

    def generate(self, model: str, system_prompt: str, user_text: str) -> dict:
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
            },
        }
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.provider == "gemini-api":
            headers["x-goog-api-key"] = str(self.api_key)
        else:
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = urllib.request.Request(
            self.endpoint(model),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return decode_response(json.load(response))
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(min(8.0, 1.0 * (2**attempt)))
        assert last_error is not None
        raise RuntimeError(str(last_error)) from last_error


def dictionary_context(candidates: list[dict]) -> str:
    if not candidates:
        return ""
    compact = [{"text": item["text"], "label": item["label"]} for item in candidates]
    return (
        "\nỨng viên từ từ điển dưới đây chỉ là gợi ý. Hãy kiểm tra ngữ cảnh, không bắt buộc "
        "giữ mọi ứng viên và không được thay đổi chuỗi text:\n"
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    )


def existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            try:
                ids.add(str(json.loads(line).get("id")))
            except json.JSONDecodeError:
                continue
    return ids


def run_system(
    name: str,
    model: str,
    use_dictionary: bool,
    records: list[dict],
    output: Path,
    client: GeminiClient,
    prompt: str,
    matcher: DictionaryMatcher,
    delay: float,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = existing_ids(output)
    with output.open("a", encoding="utf-8", newline="\n") as handle:
        for number, record in enumerate(records, start=1):
            sample_id = str(record["id"])
            if sample_id in completed:
                continue
            text = record["input_text"]
            candidates = matcher.find(text) if use_dictionary else []
            user_text = f"Văn bản: {text}" + dictionary_context(candidates)
            started = time.perf_counter()
            error = None
            try:
                result = client.generate(model, prompt, user_text)
            except Exception as exc:  # Preserve failures in the experiment log.
                result = {"entities": []}
                error = type(exc).__name__
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            row = {
                "id": sample_id,
                "system": name,
                "model": model,
                "input_text": text,
                "output_text": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                "dictionary_candidates": candidates,
                "latency_ms": elapsed_ms,
                "error": error,
            }
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            print(f"[{name}] {number}/{len(records)} {sample_id} error={error or '-'}")
            if delay:
                time.sleep(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--gold", type=Path, default=root / "processed" / "safe" / "test.internal.jsonl")
    parser.add_argument("--prompt", type=Path, default=root / "prompts" / "system_prompt_vi.txt")
    parser.add_argument("--dictionary", type=Path, default=root.parent / "data" / "icd10_dictionary.json")
    parser.add_argument("--output-dir", type=Path, default=root / "predictions")
    parser.add_argument("--provider", choices=("gemini-api", "vertex"), default="gemini-api")
    parser.add_argument("--base-model", default=os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"))
    parser.add_argument("--tuned-model")
    parser.add_argument(
        "--systems",
        default="prompt_only,prompt_dictionary,prompt_tuned,prompt_tuned_dictionary",
        help="Comma-separated subset of the four experiment systems",
    )
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--location", default=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"))
    parser.add_argument("--api-key-env", default="GEMINI_API_KEY")
    parser.add_argument("--access-token-env", default="GOOGLE_OAUTH_ACCESS_TOKEN")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    records = load_jsonl(args.gold)
    if args.limit > 0:
        records = records[: args.limit]
    prompt = args.prompt.read_text(encoding="utf-8-sig").strip()
    matcher = load_matcher(args.dictionary)
    client = GeminiClient(
        provider=args.provider,
        project=args.project,
        location=args.location,
        api_key=os.getenv(args.api_key_env),
        access_token=os.getenv(args.access_token_env),
        timeout=args.timeout,
        retries=args.retries,
    )
    all_systems = [
        ("prompt_only", args.base_model, False),
        ("prompt_dictionary", args.base_model, True),
        ("prompt_tuned", args.tuned_model, False),
        ("prompt_tuned_dictionary", args.tuned_model, True),
    ]
    requested = {item.strip() for item in args.systems.split(",") if item.strip()}
    known = {item[0] for item in all_systems}
    if not requested or requested - known:
        raise ValueError(f"--systems must be a non-empty subset of {sorted(known)}")
    if any(name.startswith("prompt_tuned") for name in requested) and not args.tuned_model:
        raise ValueError("--tuned-model is required when a tuned system is selected")
    systems = [item for item in all_systems if item[0] in requested]
    for name, model, use_dictionary in systems:
        run_system(
            name,
            str(model),
            use_dictionary,
            records,
            args.output_dir / f"{name}.jsonl",
            client,
            prompt,
            matcher,
            args.delay,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
