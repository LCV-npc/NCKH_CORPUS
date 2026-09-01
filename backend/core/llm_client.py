from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import requests

from config.llm import PDFLLMSettings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMConfigurationError(LLMError):
    pass


class LLMConnectionError(LLMError):
    pass


class LLMTimeoutError(LLMConnectionError):
    pass


class LLMRateLimitError(LLMConnectionError):
    pass


class LLMInvalidResponseError(LLMError):
    pass


class GeminiStructuredClient:
    """Small Gemini REST client for deterministic structured extraction.

    The API key is sent only in the ``x-goog-api-key`` header and is never
    included in logs, URLs returned to callers, or exception messages.
    """

    def __init__(
        self,
        settings: PDFLLMSettings | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings or PDFLLMSettings.from_env()
        try:
            self.settings.validate()
        except ValueError as exc:
            raise LLMConfigurationError(str(exc)) from exc
        self.session = session or requests.Session()

    def generate_structured_output(
        self,
        *,
        system_prompt: str,
        document_content: str,
        response_schema: dict[str, Any],
    ) -> dict[str, Any]:
        if not document_content.strip():
            raise LLMInvalidResponseError("Không có nội dung PDF để gửi đến LLM")

        model = quote(self.settings.model, safe="-._")
        endpoint = f"{self.settings.base_url}/models/{model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": document_content}]}],
            "generationConfig": {
                "temperature": self.settings.temperature,
                "responseMimeType": "application/json",
                "responseSchema": response_schema,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.settings.api_key,
        }

        total_attempts = self.settings.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, total_attempts + 1):
            try:
                logger.info(
                    "[LLM] Gemini extraction attempt %s/%s; model=%s",
                    attempt,
                    total_attempts,
                    self.settings.model,
                )
                response = self.session.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.settings.timeout_seconds,
                )
                if response.status_code == 429:
                    raise LLMRateLimitError("Gemini đang giới hạn tần suất yêu cầu")
                if response.status_code >= 500:
                    raise LLMConnectionError(
                        f"Gemini tạm thời không khả dụng (HTTP {response.status_code})"
                    )
                if not response.ok:
                    # Do not relay provider payloads: they may contain request
                    # details that are unsuitable for the frontend.
                    raise LLMError(f"Gemini từ chối yêu cầu (HTTP {response.status_code})")

                return self._decode_response(response.json())
            except requests.Timeout as exc:
                last_error = LLMTimeoutError("Gemini phản hồi quá thời gian cho phép")
            except requests.ConnectionError as exc:
                last_error = LLMConnectionError("Không thể kết nối đến Gemini")
            except (LLMRateLimitError, LLMConnectionError) as exc:
                last_error = exc
            except requests.RequestException as exc:
                raise LLMConnectionError("Lỗi HTTP khi gọi Gemini") from exc
            except ValueError as exc:
                raise LLMInvalidResponseError("Gemini trả response không phải JSON") from exc

            if attempt < total_attempts:
                time.sleep(min(4.0, 0.75 * (2 ** (attempt - 1))))

        assert last_error is not None
        raise last_error

    @staticmethod
    def _decode_response(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            candidates = payload["candidates"]
            parts = candidates[0]["content"]["parts"]
            raw = "".join(str(part.get("text") or "") for part in parts).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMInvalidResponseError("Gemini không trả structured candidate") from exc

        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw).strip()
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMInvalidResponseError("JSON từ Gemini không hợp lệ") from exc
        if not isinstance(decoded, dict):
            raise LLMInvalidResponseError("Structured output của Gemini phải là object")
        return decoded
