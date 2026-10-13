from __future__ import annotations

import csv
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import google.generativeai as genai
import mysql.connector

from core.ai_label import GEMINI_MODEL, extract_with_ai_label
from core.ner_dict import DICT_DIR


LABELS = ("DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC_PROCEDURE", "TREATMENT")
SYSTEMS = ("current_ai", "vietbioner", "vimedner")
LABEL_PRIORITY = {label: index for index, label in enumerate(LABELS)}
CURRENT_LABEL_MAP = {
    "Bệnh lý": "DISEASE",
    "Triệu chứng": "SYMPTOM",
    "Điều trị": "TREATMENT",
    "Xét nghiệm": "DIAGNOSTIC_PROCEDURE",
    "Hình ảnh": "DIAGNOSTIC_PROCEDURE",
    "Sinh lý": None,
}
ROOT = Path(__file__).resolve().parents[2]
TEXT_ROOT = ROOT / "text"
TRAINING_DATA = ROOT / "ner_finetuning" / "processed" / "combined_provisional" / "train.internal.jsonl"
TOKEN_PATTERN = re.compile(r"\w+(?:[-+]\w+)*|[^\w\s]", re.UNICODE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def tokenize(text: str) -> list[dict[str, Any]]:
    return [
        {"text": match.group(), "start": match.start(), "end": match.end()}
        for match in TOKEN_PATTERN.finditer(text)
    ]


def normalize_entity(text: str, entity: dict[str, Any]) -> dict[str, Any] | None:
    label = str(entity.get("label") or "").strip().upper()
    surface = str(entity.get("text") or "").strip()
    if label not in LABELS or not surface:
        return None
    start = entity.get("start")
    end = entity.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        start = text.find(surface)
        end = start + len(surface) if start >= 0 else -1
    if start < 0 or end <= start or end > len(text) or text[start:end] != surface:
        return None
    return {"text": surface, "label": label, "start": start, "end": end}


def align_surface_entities(text: str, entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    aligned: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    cursors: dict[tuple[str, str], int] = defaultdict(int)
    source_tokens = tokenize(text)
    token_starts = {token["start"] for token in source_tokens}
    token_ends = {token["end"] for token in source_tokens}
    for raw in entities:
        label = str(raw.get("label") or "").strip().upper()
        surface = str(raw.get("text") or "").strip()
        if label not in LABELS:
            issues.append({"type": "INVALID_LABEL", "entity": raw})
            continue
        key = (surface.casefold(), label)
        start = raw.get("start") if isinstance(raw.get("start"), int) else text.casefold().find(surface.casefold(), cursors[key])
        end = raw.get("end") if isinstance(raw.get("end"), int) else start + len(surface)
        if start < 0 or end <= start or end > len(text) or text[start:end].casefold() != surface.casefold():
            candidate = None
        else:
            candidate = normalize_entity(text, {**raw, "text": text[start:end], "start": start, "end": end})
        if candidate is None:
            issues.append({"type": "NON_VERBATIM", "entity": raw})
            continue
        if candidate["start"] not in token_starts or candidate["end"] not in token_ends:
            issues.append({"type": "TOKEN_BOUNDARY", "entity": candidate})
            continue
        cursors[key] = candidate["end"]
        aligned.append(candidate)
    return aligned, issues


def resolve_overlaps(entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(
        entities,
        key=lambda item: (-(item["end"] - item["start"]), LABEL_PRIORITY[item["label"]], item["start"], item["end"]),
    )
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for item in ordered:
        identity = (item["start"], item["end"], item["label"])
        if identity in seen:
            rejected.append({"type": "DUPLICATE", "entity": item})
            continue
        seen.add(identity)
        conflict = next(
            (other for other in kept if item["start"] < other["end"] and other["start"] < item["end"]),
            None,
        )
        if conflict:
            rejected.append({"type": "OVERLAP_DROPPED", "entity": item, "kept": conflict})
        else:
            kept.append(item)
    return sorted(kept, key=lambda item: (item["start"], item["end"])), rejected


def entities_to_bio(text: str, entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tokens = tokenize(text)
    issues: list[dict[str, Any]] = []
    for token in tokens:
        token["tag"] = "O"
    for entity in entities:
        covered = [
            token for token in tokens
            if entity["start"] <= token["start"] and token["end"] <= entity["end"]
        ]
        if not covered or covered[0]["start"] != entity["start"] or covered[-1]["end"] != entity["end"]:
            issues.append({"type": "TOKEN_BOUNDARY", "entity": entity})
            continue
        for index, token in enumerate(covered):
            token["tag"] = ("B-" if index == 0 else "I-") + entity["label"]
    return tokens, issues


def bio_text(tokens: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, token in enumerate(tokens):
        lines.append(f"{token['text']}\t{token['tag']}")
        if token["text"] in {".", "!", "?"} and index + 1 < len(tokens):
            lines.append("")
    return "\n".join(lines).rstrip() + "\n\n"


def load_few_shots(source: str, count: int = 5) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates = [row for row in read_jsonl(TRAINING_DATA) if row.get("source") == source and row.get("entities")]
    selected: list[dict[str, Any]] = []
    covered: set[str] = set()
    for row in candidates:
        row_labels = {item.get("label") for item in row.get("entities", [])} & set(LABELS)
        if row_labels - covered:
            selected.append(row)
            covered.update(row_labels)
        if len(selected) >= count:
            break
    for row in candidates:
        if len(selected) >= count:
            break
        if row not in selected:
            selected.append(row)
    missing = [label for label in LABELS if label not in covered]
    return selected, {"source": source, "shot_count": len(selected), "covered_labels": sorted(covered), "missing_labels": missing}


def prompt_for(source: str, examples: list[dict[str, Any]]) -> str:
    example_text = "\n\n".join(
        "INPUT:\n" + row["input_text"] + "\nOUTPUT:\n" + json.dumps({"entities": [
            {"text": item["text"], "label": item["label"]} for item in row["entities"]
        ]}, ensure_ascii=False, separators=(",", ":"))
        for row in examples
    )
    return f"""Bạn là hệ thống NER y khoa tiếng Việt. Trích xuất đúng nguyên văn năm loại thực thể:
DISEASE (bệnh/tình trạng bệnh lý), SYMPTOM (dấu hiệu/triệu chứng), CAUSE (nguyên nhân hoặc yếu tố gây bệnh),
DIAGNOSTIC_PROCEDURE (xét nghiệm/thăm dò/chẩn đoán hình ảnh), TREATMENT (thuốc/thủ thuật/phương pháp điều trị).
Chỉ xuất JSON dạng {{\"entities\":[{{\"text\":\"...\",\"label\":\"DISEASE\"}}]}}.
Không suy diễn, không đổi cách viết, không xuất chuỗi không có nguyên văn, không thêm Markdown hay giải thích.
Nếu một thực thể lặp lại trong văn bản, xuất từng lần theo thứ tự. Dữ liệu few-shot cố định từ {source}:

{example_text}
"""


def gemini_extract(text: str, prompt: str) -> list[dict[str, Any]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Chưa cấu hình GEMINI_API_KEY trong backend/.env")
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(GEMINI_MODEL, system_instruction=prompt)
    response = model.generate_content(
        "Văn bản:\n" + text,
        generation_config={"temperature": 0, "response_mime_type": "application/json"},
        request_options={"timeout": float(os.getenv("GEMINI_TIMEOUT_SECONDS", "90"))},
    )
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.text.strip(), flags=re.IGNORECASE)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("entities"), list):
        raise ValueError("INVALID_JSON_SCHEMA")
    return payload["entities"]


def current_ai_entities(text: str) -> list[dict[str, Any]]:
    result = extract_with_ai_label(text)
    entities: list[dict[str, Any]] = []
    for source_label, items in result.items():
        label = CURRENT_LABEL_MAP.get(source_label)
        if not label:
            continue
        for item in items if isinstance(items, list) else []:
            for span in item.get("spans", []):
                start, end = int(span["start"]), int(span["end"])
                entities.append({"text": text[start:end], "label": label, "start": start, "end": end})
    return entities


def git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
    except Exception:
        return None


@dataclass
class RunState:
    run_id: str
    directory: Path
    limit: int
    note: str = ""
    parent_run_id: str | None = None
    status: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    systems: dict[str, dict[str, Any]] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "status": self.status,
            "limit": self.limit,
            "note": self.note,
            "parentRunId": self.parent_run_id,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "systems": self.systems,
            "hasMetrics": (self.directory / "metrics.json").exists(),
        }


class NerExperimentManager:
    def __init__(self) -> None:
        self.db_config: dict[str, Any] = {}
        self.runs: dict[str, RunState] = {}
        self.lock = threading.RLock()

    def configure_db(self, db_config: dict[str, Any]) -> None:
        self.db_config = dict(db_config)
        TEXT_ROOT.mkdir(parents=True, exist_ok=True)
        (TEXT_ROOT / "runs").mkdir(parents=True, exist_ok=True)
        self._load_runs()

    def _load_runs(self) -> None:
        for state_path in sorted((TEXT_ROOT / "runs").glob("*/state.json")):
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                state = RunState(
                    run_id=data["runId"], directory=state_path.parent, limit=int(data["limit"]),
                    note=data.get("note", ""), parent_run_id=data.get("parentRunId"),
                    status="stopped" if data.get("status") in {"running", "queued"} else data.get("status", "stopped"),
                    created_at=data.get("createdAt", utc_now()), started_at=data.get("startedAt"),
                    finished_at=data.get("finishedAt"), systems=data.get("systems", {}),
                )
                self.runs[state.run_id] = state
            except (OSError, ValueError, KeyError):
                continue

    def _save(self, state: RunState) -> None:
        atomic_json(state.directory / "state.json", state.public())

    def _snapshot_articles(self, limit: int) -> list[dict[str, Any]]:
        connection = mysql.connector.connect(**self.db_config)
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id, title, abstract FROM articles WHERE abstract IS NOT NULL AND TRIM(abstract) <> '' ORDER BY id ASC LIMIT %s",
                (limit,),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
            connection.close()
        return [
            {"article_id": int(row["id"]), "title": row.get("title") or "", "abstract": row["abstract"],
             "checksum": checksum(row["abstract"])}
            for row in rows
        ]

    def start(self, limit: int = 100, note: str = "", parent_run_id: str | None = None) -> RunState:
        if not 1 <= limit <= 100:
            raise ValueError("limit phải nằm trong khoảng 1..100")
        with self.lock:
            if any(run.status == "running" for run in self.runs.values()):
                raise RuntimeError("Đang có một lần chạy NER khác hoạt động.")
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            directory = TEXT_ROOT / "runs" / run_id
            articles = self._snapshot_articles(limit)
            if len(articles) != limit:
                raise RuntimeError(f"Chỉ tìm thấy {len(articles)}/{limit} bài có abstract.")
            state = RunState(run_id, directory, limit, note, parent_run_id)
            state.systems = {
                name: {"status": "queued", "completed": 0, "total": limit, "apiErrors": 0, "averageLatencyMs": 0.0}
                for name in SYSTEMS
            }
            directory.mkdir(parents=True)
            input_dir = directory / "input"
            for article in articles:
                append_jsonl(input_dir / "articles.jsonl", article)
            shutil.copy2(input_dir / "articles.jsonl", directory / "articles.jsonl")
            self._prepare_config(state)
            if parent_run_id:
                self._seed_from_parent(state, articles, parent_run_id)
            self.runs[run_id] = state
            self._save(state)
            threading.Thread(target=self._run, args=(state,), daemon=True, name=f"ner-{run_id}").start()
            return state

    def _seed_from_parent(self, state: RunState, articles: list[dict[str, Any]], parent_run_id: str) -> None:
        parent = self.runs.get(parent_run_id)
        if not parent:
            raise ValueError(f"Không tìm thấy parent_run_id={parent_run_id}")
        expected = {int(row["article_id"]): row["checksum"] for row in articles}
        for filename in ("mapping.json", "vietbioner_prompt.txt", "vimedner_prompt.txt"):
            child_value = (state.directory / "config" / filename).read_bytes()
            parent_path = parent.directory / "config" / filename
            if not parent_path.exists() or parent_path.read_bytes() != child_value:
                raise ValueError(f"Không thể kế thừa vì cấu hình {filename} đã thay đổi.")
        inherited: dict[str, int] = {}
        for system in SYSTEMS:
            count = 0
            inherited_latencies: list[float] = []
            for row in read_jsonl(parent.directory / system / "predictions.jsonl"):
                article_id = int(row["article_id"])
                if row.get("error") or expected.get(article_id) != row.get("input_checksum"):
                    continue
                cleaned, boundary_issues = align_surface_entities(
                    next(item["abstract"] for item in articles if int(item["article_id"]) == article_id),
                    row.get("entities", []),
                )
                cleaned, overlap_issues = resolve_overlaps(cleaned)
                copied = {**row, "entities": cleaned, "inherited_from": parent_run_id}
                append_jsonl(state.directory / system / "predictions.jsonl", copied)
                inherited_latencies.append(float(row.get("latency_ms") or 0))
                for issue in boundary_issues + overlap_issues:
                    append_jsonl(state.directory / system / "conversion_issues.jsonl", {"article_id": article_id, **issue})
                count += 1
            inherited[system] = count
            state.systems[system]["completed"] = count
            state.systems[system]["averageLatencyMs"] = round(sum(inherited_latencies) / len(inherited_latencies), 2) if inherited_latencies else 0.0
        manifest_path = state.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["inherited_predictions"] = inherited
        atomic_json(manifest_path, manifest)

    def _prepare_config(self, state: RunState) -> None:
        config_dir = state.directory / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(config_dir / "mapping.json", {"current_ai": CURRENT_LABEL_MAP, "labels": LABELS})
        (config_dir / "current_ai_implementation.py").write_text(inspect.getsource(extract_with_ai_label), encoding="utf-8")
        if DICT_DIR.exists():
            shutil.copytree(DICT_DIR, config_dir / "dictionary", dirs_exist_ok=True)
        manifests = {}
        for system in ("vietbioner", "vimedner"):
            shots, manifest = load_few_shots(system)
            append_path = config_dir / f"{system}_fewshot.jsonl"
            for shot in shots:
                append_jsonl(append_path, shot)
            prompt = prompt_for(system, shots)
            (config_dir / f"{system}_prompt.txt").write_text(prompt, encoding="utf-8")
            manifests[system] = manifest
        metadata = {
            "run_id": state.run_id, "model": GEMINI_MODEL, "temperature": 0, "created_at": state.created_at,
            "git_commit": git_commit(), "note": state.note, "parent_run_id": state.parent_run_id,
            "few_shot": manifests, "current_ai_missing_labels": ["CAUSE"],
        }
        metadata["configuration_checksum"] = checksum(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        atomic_json(state.directory / "manifest.json", metadata)

    def _completed_ids(self, output: Path) -> set[int]:
        return {int(row["article_id"]) for row in read_jsonl(output) if row.get("article_id") is not None}

    def _run(self, state: RunState) -> None:
        state.status = "running"
        state.started_at = state.started_at or utc_now()
        self._save(state)
        articles = read_jsonl(state.directory / "articles.jsonl")
        try:
            for system in SYSTEMS:
                if state.stop_event.is_set():
                    break
                self._run_system(state, system, articles)
            if state.stop_event.is_set():
                state.status = "stopped"
            else:
                self._compile_run(state, articles)
                state.status = "completed_with_errors" if any(v["apiErrors"] for v in state.systems.values()) else "completed"
                state.finished_at = utc_now()
        except Exception as exc:
            state.status = "failed"
            state.finished_at = utc_now()
            append_jsonl(state.directory / "fatal_errors.jsonl", {"at": utc_now(), "error": type(exc).__name__, "message": str(exc)})
        finally:
            self._save(state)

    def _run_system(self, state: RunState, system: str, articles: list[dict[str, Any]]) -> None:
        output_dir = state.directory / system
        prediction_path = output_dir / "predictions.jsonl"
        completed = self._completed_ids(prediction_path)
        latencies = [float(row.get("latency_ms", 0)) for row in read_jsonl(prediction_path)]
        info = state.systems[system]
        info.update({"status": "running", "completed": len(completed)})
        prompt = ""
        if system != "current_ai":
            prompt = (state.directory / "config" / f"{system}_prompt.txt").read_text(encoding="utf-8")
        for article in articles:
            article_id = int(article["article_id"])
            if article_id in completed or state.stop_event.is_set():
                continue
            started = time.perf_counter()
            error = None
            attempt_errors: list[dict[str, Any]] = []
            raw_entities: list[dict[str, Any]] = []
            for attempt in range(3):
                try:
                    raw_entities = current_ai_entities(article["abstract"]) if system == "current_ai" else gemini_extract(article["abstract"], prompt)
                    error = None
                    break
                except Exception as exc:
                    error = {"type": type(exc).__name__, "message": str(exc), "attempt": attempt + 1}
                    attempt_errors.append(error)
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            latency = round((time.perf_counter() - started) * 1000, 2)
            latencies.append(latency)
            aligned, issues = align_surface_entities(article["abstract"], raw_entities)
            resolved, overlap_issues = resolve_overlaps(aligned)
            issues.extend(overlap_issues)
            for retry_error in (attempt_errors if error is None else attempt_errors[:-1]):
                issues.append({**retry_error, "error_type": retry_error["type"], "type": "API_RETRY"})
            if error:
                issues.append({**error, "error_type": error["type"], "type": "API_FAILURE"})
                info["apiErrors"] += 1
            row = {
                "article_id": article_id, "title": article["title"], "input_checksum": article["checksum"],
                "system": system, "model": GEMINI_MODEL, "entities": resolved, "raw_entities": raw_entities,
                "latency_ms": latency, "error": error, "created_at": utc_now(),
            }
            append_jsonl(prediction_path, row)
            for issue in issues:
                append_jsonl(output_dir / "conversion_issues.jsonl", {"article_id": article_id, **issue})
            completed.add(article_id)
            info.update({"completed": len(completed), "averageLatencyMs": round(sum(latencies) / len(latencies), 2)})
            self._save(state)
            time.sleep(float(os.getenv("NER_EXPERIMENT_DELAY", "0.2")))
        info["status"] = "stopped" if state.stop_event.is_set() else "completed"
        self._save(state)

    def _compile_run(self, state: RunState, articles: list[dict[str, Any]]) -> None:
        by_id = {int(row["article_id"]): row for row in articles}
        for system in SYSTEMS:
            system_dir = state.directory / system
            predictions = sorted(read_jsonl(system_dir / "predictions.jsonl"), key=lambda row: int(row["article_id"]))
            conll_parts: list[str] = []
            line_number = 1
            index_rows: list[dict[str, Any]] = []
            articles_dir = system_dir / "articles"
            articles_dir.mkdir(parents=True, exist_ok=True)
            for prediction in predictions:
                article = by_id[int(prediction["article_id"])]
                tokens, token_issues = entities_to_bio(article["abstract"], prediction["entities"])
                block = bio_text(tokens)
                start_line = line_number
                line_number += block.count("\n")
                conll_parts.append(block)
                append_jsonl(system_dir / "article_index.jsonl", {
                    "article_id": article["article_id"], "start_line": start_line, "end_line": line_number - 1,
                    "token_count": len(tokens), "checksum": article["checksum"],
                })
                for issue in token_issues:
                    append_jsonl(system_dir / "conversion_issues.jsonl", {"article_id": article["article_id"], **issue})
                stem = f"{int(article['article_id']):04d}"
                (articles_dir / f"{stem}.txt").write_text(article["abstract"], encoding="utf-8")
                ann = "\n".join(
                    f"T{idx}\t{entity['label']} {entity['start']} {entity['end']}\t{entity['text']}"
                    for idx, entity in enumerate(prediction["entities"], start=1)
                )
                (articles_dir / f"{stem}.ann").write_text(ann + ("\n" if ann else ""), encoding="utf-8")
            (system_dir / "test.txt").write_text("".join(conll_parts), encoding="utf-8")
        gold_path = TEXT_ROOT / "gold" / "gold.jsonl"
        if gold_path.exists():
            gold_ids = {int(row["article_id"]) for row in read_jsonl(gold_path)}
            article_ids = {int(row["article_id"]) for row in articles}
            if gold_ids == article_ids:
                from core.ner_evaluation import evaluate_run

                evaluate_run(state.directory, gold_path)

    def stop(self, run_id: str) -> RunState | None:
        state = self.runs.get(run_id)
        if state and state.status == "running":
            state.stop_event.set()
            state.status = "stopping"
            self._save(state)
        return state

    def resume(self, run_id: str) -> RunState | None:
        with self.lock:
            state = self.runs.get(run_id)
            if not state:
                return None
            if state.status not in {"stopped", "failed", "completed_with_errors"}:
                raise RuntimeError("Run này không ở trạng thái có thể resume.")
            if any(run.status == "running" for run in self.runs.values()):
                raise RuntimeError("Đang có một run khác hoạt động.")
            state.stop_event = threading.Event()
            state.status = "queued"
            self._save(state)
            threading.Thread(target=self._run, args=(state,), daemon=True, name=f"ner-{run_id}").start()
            return state

    def list(self) -> list[dict[str, Any]]:
        return [state.public() for state in sorted(self.runs.values(), key=lambda item: item.created_at, reverse=True)]

    def get(self, run_id: str) -> RunState | None:
        return self.runs.get(run_id)

    def promote(self, run_id: str) -> RunState:
        state = self.runs[run_id]
        if state.status not in {"completed", "completed_with_errors"}:
            raise RuntimeError("Chỉ có thể chọn run đã hoàn tất.")
        for system in SYSTEMS:
            source = state.directory / system
            target = TEXT_ROOT / system
            temporary = TEXT_ROOT / f".{system}-{state.run_id}"
            if temporary.exists():
                shutil.rmtree(temporary)
            shutil.copytree(source, temporary)
            if target.exists():
                shutil.rmtree(target)
            temporary.replace(target)
        atomic_json(TEXT_ROOT / "current_run.json", {"run_id": run_id, "promoted_at": utc_now()})
        return state


ner_experiment_manager = NerExperimentManager()
