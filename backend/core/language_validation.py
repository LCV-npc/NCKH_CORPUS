"""Deterministic, traceable language validation for the Vietnamese corpus.

This module never silently maps an unknown language to Vietnamese. It produces
evidence that can be persisted in MySQL/audit reports for later review.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

from config.language_filter import VietnameseCorpusSettings


DetectorFactory.seed = 0

# Bump this whenever the evidence-selection rule changes in a way that can
# alter a previous rejection. The crawler uses it to re-check decisions made
# by an obsolete rule instead of permanently preserving them.
LANGUAGE_VALIDATION_VERSION = "2.0"

_VI_DIACRITICS = set("ăâđêôơưàảãáạằẳẵắặầẩẫấậèẻẽéẹềểễếệìỉĩíịòỏõóọồổỗốộờởỡớợùủũúụừửữứựỳỷỹýỵ")
_VI_WORDS = {
    "và", "của", "các", "được", "trong", "người", "bệnh", "nghiên", "cứu",
    "điều", "trị", "kết", "quả", "phương", "pháp", "tại", "với", "cho",
    "theo", "đánh", "giá", "mục", "tiêu", "đối", "tượng", "thời", "gian",
}
_EN_WORDS = {
    "the", "and", "of", "in", "for", "with", "this", "that", "study", "patients",
    "methods", "results", "conclusion", "background", "objective", "treatment",
    "medical", "clinical", "were", "was", "from", "between", "data",
}
_WORD_RE = re.compile(r"\b[\wÀ-ỹ]+\b", re.UNICODE)


@dataclass(frozen=True)
class ChunkEvidence:
    characters: int
    detected_language: str
    detected_confidence: float
    label: str
    vietnamese_signal: float
    english_signal: float


@dataclass(frozen=True)
class LanguageAssessment:
    language: str
    confidence: float
    vietnamese_ratio: float
    english_ratio: float
    assessed_characters: int
    chunk_count: int
    reason: str
    chunks: tuple[ChunkEvidence, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AdmissionDecision:
    status: str
    reason: str
    metadata: LanguageAssessment
    pdf: LanguageAssessment | None

    @property
    def accepted(self) -> bool:
        return self.status == "ACCEPTED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "metadata": self.metadata.as_dict(),
            "pdf": self.pdf.as_dict() if self.pdf else None,
        }


def normalize_language_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value or "")).strip()


def _chunks(text: str, chunk_chars: int) -> Iterable[str]:
    text = normalize_language_text(text)
    if not text:
        return []
    return [text[index:index + chunk_chars] for index in range(0, len(text), chunk_chars)]


def _probabilities(text: str) -> tuple[str, float, float, float]:
    """Return top language, confidence, Vietnamese and English probabilities."""
    try:
        probabilities = {item.lang: float(item.prob) for item in detect_langs(text)}
    except LangDetectException:
        return "unknown", 0.0, 0.0, 0.0
    if not probabilities:
        return "unknown", 0.0, 0.0, 0.0
    top, confidence = max(probabilities.items(), key=lambda item: item[1])
    return top, confidence, probabilities.get("vi", 0.0), probabilities.get("en", 0.0)


def _chunk_evidence(text: str) -> ChunkEvidence:
    letters = [char.casefold() for char in text if char.isalpha()]
    words = {word.casefold() for word in _WORD_RE.findall(text)}
    vi_marks = sum(char in _VI_DIACRITICS for char in letters)
    vi_marker_count = len(words & _VI_WORDS)
    en_marker_count = len(words & _EN_WORDS)
    top, top_confidence, vi_probability, en_probability = _probabilities(text)

    # Diacritics and Vietnamese function words are independent evidence. They
    # are capped so a short author name cannot dominate a whole PDF section.
    diacritic_signal = min(0.28, (vi_marks / max(1, len(letters))) * 4.0)
    vi_word_signal = min(0.24, vi_marker_count * 0.035)
    en_word_signal = min(0.24, en_marker_count * 0.035)
    vietnamese_signal = min(1.0, vi_probability * 0.72 + diacritic_signal + vi_word_signal)
    english_signal = min(1.0, en_probability * 0.76 + en_word_signal)

    if vietnamese_signal >= 0.52 and vietnamese_signal >= english_signal + 0.08:
        label = "vi"
    elif english_signal >= 0.58 and english_signal >= vietnamese_signal + 0.10:
        label = "en"
    else:
        label = "unknown"
    return ChunkEvidence(
        characters=len(text), detected_language=top, detected_confidence=round(top_confidence, 4),
        label=label, vietnamese_signal=round(vietnamese_signal, 4), english_signal=round(english_signal, 4),
    )


def assess_language(text: str | None, settings: VietnameseCorpusSettings | None = None) -> LanguageAssessment:
    """Assess text in chunks so bilingual abstracts/references cannot dominate."""
    settings = settings or VietnameseCorpusSettings()
    cleaned = normalize_language_text(text)
    if not cleaned:
        return LanguageAssessment("unknown", 0.0, 0.0, 0.0, 0, 0, "NO_TEXT")
    evidences = tuple(_chunk_evidence(chunk) for chunk in _chunks(cleaned, settings.chunk_chars))
    total = sum(item.characters for item in evidences)
    vi_chars = sum(item.characters for item in evidences if item.label == "vi")
    en_chars = sum(item.characters for item in evidences if item.label == "en")
    vi_ratio = vi_chars / total if total else 0.0
    en_ratio = en_chars / total if total else 0.0
    if vi_ratio >= settings.min_vietnamese_ratio:
        language, confidence, reason = "vi", vi_ratio, "VIETNAMESE_MAJORITY"
    elif en_ratio >= settings.english_reject_ratio:
        language, confidence, reason = "en", en_ratio, "ENGLISH_MAJORITY"
    elif vi_ratio or en_ratio:
        language, confidence, reason = "mixed", max(vi_ratio, en_ratio), "MIXED_OR_INSUFFICIENT"
    else:
        language, confidence, reason = "unknown", 0.0, "INSUFFICIENT_LANGUAGE_SIGNAL"
    return LanguageAssessment(
        language=language, confidence=round(confidence, 4), vietnamese_ratio=round(vi_ratio, 4),
        english_ratio=round(en_ratio, 4), assessed_characters=total, chunk_count=len(evidences),
        reason=reason, chunks=evidences,
    )


def assess_metadata(title: str, abstract: str, html_language: str = "", settings: VietnameseCorpusSettings | None = None) -> LanguageAssessment:
    """Assess title/abstract; the HTML language attribute is only supplemental."""
    text = "\n".join(part for part in (title, abstract) if normalize_language_text(part))
    assessment = assess_language(text, settings)
    if html_language.casefold().startswith("en") and assessment.language == "unknown":
        return LanguageAssessment("en", 0.7, 0.0, 1.0, assessment.assessed_characters, assessment.chunk_count, "HTML_LANGUAGE_EN", assessment.chunks)
    return assessment


def select_pdf_text_for_language(body: str | None, full_text: str | None) -> str:
    """Select reliable PDF text for language validation without altering it.

    A normal ``body`` excludes abstracts and references. Some older bilingual
    layouts can make the section parser retain only a small translated block.
    When the resulting body covers less than half of the extracted document,
    use the complete text so that block cannot wrongly reject a Vietnamese PDF.
    """
    body = body or ""
    full_text = full_text or ""
    if not full_text:
        return body
    if not body or len(body) < len(full_text) * 0.5:
        return full_text
    return body


def decide_admission(
    metadata: LanguageAssessment,
    pdf_text: str | None,
    settings: VietnameseCorpusSettings | None = None,
) -> AdmissionDecision:
    """Only an accepted PDF body can admit an article into the final corpus."""
    settings = settings or VietnameseCorpusSettings()
    pdf = assess_language(pdf_text, settings)
    if pdf.assessed_characters < settings.pdf_min_chars:
        return AdmissionDecision("REJECTED_NO_TEXT", "PDF_TEXT_TOO_SHORT_OR_EMPTY", metadata, pdf)
    if pdf.language == "vi":
        return AdmissionDecision("ACCEPTED", "PDF_VIETNAMESE_MAJORITY", metadata, pdf)
    if pdf.language == "en":
        return AdmissionDecision("REJECTED_ENGLISH", "PDF_ENGLISH_MAJORITY", metadata, pdf)
    return AdmissionDecision("REJECTED_MIXED", "PDF_NOT_CLEARLY_VIETNAMESE", metadata, pdf)


def stable_file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
