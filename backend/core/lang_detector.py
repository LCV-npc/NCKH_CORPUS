"""Backward-compatible language name helper.

Corpus admission must use :mod:`core.language_validation`; this small legacy
helper remains for callers that only need a display folder name.
"""

from core.language_validation import assess_language


def detect_language(text: str) -> str:
    assessment = assess_language(text)
    if assessment.language == "vi":
        return "Vietnamese"
    if assessment.language == "en":
        return "English"
    return "Unknown"
