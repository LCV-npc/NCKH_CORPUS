from __future__ import annotations

from dataclasses import dataclass
import os

from config.env import load_backend_env


load_backend_env()

PDF_TEXT_NORMALIZATION_VERSION = "continuous_paragraph_v1"


@dataclass(frozen=True)
class PDFLLMSettings:
    provider: str
    api_key: str
    model: str
    base_url: str
    temperature: float
    timeout_seconds: int
    max_retries: int
    max_input_chars: int
    prompt_version: str

    @classmethod
    def from_env(cls) -> "PDFLLMSettings":
        return cls(
            provider=os.getenv("PDF_LLM_PROVIDER", "gemini").strip().casefold(),
            api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            model=os.getenv(
                "PDF_LLM_MODEL",
                os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"),
            ).strip(),
            base_url=os.getenv(
                "PDF_LLM_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta",
            ).rstrip("/"),
            temperature=float(os.getenv("PDF_LLM_TEMPERATURE", "0")),
            timeout_seconds=max(5, int(os.getenv("PDF_LLM_TIMEOUT_SECONDS", "90"))),
            max_retries=max(0, int(os.getenv("PDF_LLM_MAX_RETRIES", "2"))),
            max_input_chars=max(12000, int(os.getenv("PDF_LLM_MAX_INPUT_CHARS", "90000"))),
            prompt_version=os.getenv("PDF_EXTRACTION_PROMPT_VERSION", "1.1").strip(),
        )

    def validate(self) -> None:
        if self.provider != "gemini":
            raise ValueError(f"PDF_LLM_PROVIDER chưa được hỗ trợ: {self.provider}")
        if not self.api_key:
            raise ValueError("Chưa cấu hình GEMINI_API_KEY trong backend/.env")
        if not self.model:
            raise ValueError("Chưa cấu hình PDF_LLM_MODEL hoặc GEMINI_MODEL")
