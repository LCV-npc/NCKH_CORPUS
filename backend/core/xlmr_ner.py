"""Local XLM-R token-classification inference for medical NER."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_PATH = ROOT / "ner_finetuning" / "models" / "xlmr-vimed-vietbio-v1" / "best_model"
LABEL_ALIASES = {"DIAGNOSTIC": "DIAGNOSTIC_PROCEDURE"}
ALLOWED_LABELS = {"DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC_PROCEDURE", "TREATMENT"}


class XLMRInferenceError(RuntimeError):
    """Raised when the local checkpoint cannot be loaded or used."""


class XLMRNER:
    def __init__(self) -> None:
        self._load_lock = Lock()
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device: Any = None
        self._model_path: Path | None = None
        self._model_name = "xlmr-vimed-vietbio-v1"
        self._max_length = int(os.getenv("XLMR_MAX_LENGTH", "256"))
        self._min_confidence = float(os.getenv("XLMR_MIN_CONFIDENCE", "0.0"))

    @property
    def model_path(self) -> Path:
        configured = os.getenv("XLMR_MODEL_PATH", "").strip()
        path = Path(configured) if configured else DEFAULT_MODEL_PATH
        if not path.is_absolute():
            path = ROOT / path
        return path.resolve()

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            model_path = self.model_path
            if not model_path.is_dir():
                raise XLMRInferenceError(
                    f"Không tìm thấy checkpoint XLM-R tại '{model_path}'. "
                    "Hãy đặt checkpoint đã fine-tune vào XLMR_MODEL_PATH."
                )
            if not (model_path / "config.json").is_file():
                raise XLMRInferenceError(f"Checkpoint XLM-R thiếu config.json: {model_path}")
            try:
                import torch
                from transformers import AutoModelForTokenClassification, AutoTokenizer
            except ImportError as exc:
                raise XLMRInferenceError(
                    "Chưa cài dependency XLM-R. Hãy cài torch và transformers "
                    "từ backend/requirements-xlmr.txt."
                ) from exc

            device_name = os.getenv("XLMR_DEVICE", "auto").strip().lower()
            if device_name == "auto":
                device_name = "cuda" if torch.cuda.is_available() else "cpu"
            try:
                device = torch.device(device_name)
            except RuntimeError as exc:
                raise XLMRInferenceError(f"Thiết bị XLM-R không hợp lệ: {device_name}") from exc

            try:
                tokenizer = AutoTokenizer.from_pretrained(str(model_path), use_fast=True)
                model = AutoModelForTokenClassification.from_pretrained(str(model_path))
                model.to(device)
                model.eval()
            except Exception as exc:
                raise XLMRInferenceError(f"Không thể load checkpoint XLM-R: {exc}") from exc
            if not getattr(tokenizer, "is_fast", False):
                raise XLMRInferenceError("Checkpoint XLM-R cần fast tokenizer để giữ offset highlight.")

            config_name = getattr(model.config, "_name_or_path", "") or model_path.name
            self._model_name = os.getenv("XLMR_MODEL_NAME", config_name or model_path.name)
            self._tokenizer = tokenizer
            self._model = model
            self._torch = torch
            self._device = device
            self._model_path = model_path

    @staticmethod
    def _label_name(raw: str) -> tuple[str, str]:
        value = str(raw or "O").upper()
        if value == "O" or "-" not in value:
            return "O", ""
        prefix, label = value.split("-", 1)
        label = LABEL_ALIASES.get(label, label)
        return prefix if prefix in {"B", "I"} else "B", label

    def predict(self, text: str) -> dict[str, Any]:
        self._ensure_loaded()
        if not text or not text.strip():
            return {"model": self._model_name, "entities": []}

        encoded = self._tokenizer(
            text,
            return_offsets_mapping=True,
            return_tensors="pt",
            truncation=True,
            max_length=self._max_length,
        )
        offsets = encoded.pop("offset_mapping")[0].tolist()
        model_inputs = {key: value.to(self._device) for key, value in encoded.items()}
        with self._torch.inference_mode():
            logits = self._model(**model_inputs).logits[0]
        probabilities = self._torch.softmax(logits, dim=-1)
        predicted_ids = logits.argmax(dim=-1).tolist()
        scores = probabilities.max(dim=-1).values.tolist()
        word_ids = encoded.word_ids(batch_index=0)

        # Keep one prediction per source word. Training assigns labels only to
        # first subwords, so later pieces must not create duplicate entities.
        source_words: dict[int, dict[str, Any]] = {}
        for token_index, word_id in enumerate(word_ids):
            start, end = offsets[token_index]
            if word_id is None or end <= start:
                continue
            current = source_words.setdefault(word_id, {"start": start, "end": end, "token": token_index})
            current["end"] = max(current["end"], end)

        word_predictions: list[dict[str, Any]] = []
        for word in sorted(source_words.values(), key=lambda item: item["start"]):
            token_index = word["token"]
            id2label = getattr(self._model.config, "id2label", {})
            raw_label = id2label.get(predicted_ids[token_index], id2label.get(str(predicted_ids[token_index]), "O"))
            prefix, label = self._label_name(raw_label)
            score = float(scores[token_index])
            word_predictions.append({**word, "prefix": prefix, "label": label, "score": score})

        entities: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for word in word_predictions + [{"prefix": "O", "label": "", "score": 0.0}]:
            is_entity = word.get("label") in ALLOWED_LABELS
            starts_new = is_entity and (word["prefix"] == "B" or current is None or current["label"] != word["label"])
            if not is_entity or starts_new:
                if current is not None:
                    self._append_entity(entities, current, text)
                    current = None
            if is_entity:
                if current is None:
                    current = {"start": word["start"], "end": word["end"], "label": word["label"], "scores": [word["score"]]}
                else:
                    current["end"] = word["end"]
                    current["scores"].append(word["score"])

        return {
            "model": self._model_name,
            "model_version": self._model_path.name if self._model_path else "best_model",
            "entities": entities,
        }

    def _append_entity(self, output: list[dict[str, Any]], item: dict[str, Any], text: str) -> None:
        score = sum(item["scores"]) / max(1, len(item["scores"]))
        if score < self._min_confidence:
            return
        start, end = int(item["start"]), int(item["end"])
        surface = text[start:end]
        if not surface.strip():
            return
        output.append({
            "text": surface,
            "name": surface,
            "label": item["label"],
            "type": item["label"],
            "start": start,
            "end": end,
            "score": round(score, 6),
            "source": "xlmr",
        })


xlmr_ner = XLMRNER()
