"""Configuration for the Vietnamese-only corpus admission gate.

All thresholds are configurable so the research team can tune the gate without
changing crawler code. The defaults are deliberately conservative: a document
must show a clear Vietnamese majority before it enters the final corpus.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(
        value.strip().lower().removeprefix("www.")
        for value in os.getenv(name, default).split(",")
        if value.strip()
    )


@dataclass(frozen=True)
class VietnameseCorpusSettings:
    """Runtime settings shared by the OJS crawler and PDF batch tools."""

    allowed_domains: tuple[str, ...] = _csv(
        "CORPUS_ALLOWED_JOURNAL_DOMAINS",
        "tapchinghiencuuyhoc.vn,tapchiyhcd.vn,tapchiyhocvietnam.vn",
    )
    metadata_min_chars: int = int(os.getenv("CORPUS_LANGUAGE_METADATA_MIN_CHARS", "80"))
    pdf_min_chars: int = int(os.getenv("CORPUS_LANGUAGE_PDF_MIN_CHARS", "400"))
    chunk_chars: int = int(os.getenv("CORPUS_LANGUAGE_CHUNK_CHARS", "1200"))
    min_vietnamese_ratio: float = float(os.getenv("CORPUS_LANGUAGE_MIN_VI_RATIO", "0.72"))
    english_reject_ratio: float = float(os.getenv("CORPUS_LANGUAGE_EN_REJECT_RATIO", "0.65"))
    candidates_dir: Path = Path(
        os.getenv("CORPUS_PDF_CANDIDATES_DIR", str(BACKEND_ROOT / "Văn_Bản_Y_Tế_PDF" / "candidates"))
    )
    quarantine_dir: Path = Path(
        os.getenv("CORPUS_PDF_QUARANTINE_DIR", str(BACKEND_ROOT / "Văn_Bản_Y_Tế_PDF" / "quarantine"))
    )
    audit_report_dir: Path = Path(
        os.getenv("CORPUS_LANGUAGE_AUDIT_REPORT_DIR", str(BACKEND_ROOT / "reports"))
    )
