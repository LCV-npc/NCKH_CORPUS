from __future__ import annotations

import re
import unicodedata


def sanitize_file_component(value: str, max_chars: int = 150) -> str:
    """Return a Windows-safe file or directory name component."""
    clean = unicodedata.normalize("NFC", str(value or ""))
    clean = re.sub(r"[\t\n\r\f\v]+", " ", clean)
    clean = re.sub(r'[\\/*?"<>|:\x00-\x1f]+', "", clean)
    return re.sub(r"\s+", " ", clean).strip(" ._")[:max_chars].strip(" ._")


def compact_article_name(value: str, max_words: int = 5) -> str:
    """Use at most the first words of an article title as its visible name.

    A leading list number is not part of the title. An ellipsis is appended
    only when the original title contains more words than are displayed.
    """
    had_ellipsis = bool(re.search(r"(?:\.{3}|…)\s*$", str(value or "")))
    clean = sanitize_file_component(value)
    clean = re.sub(r"^\d+\s*[.)-]\s*", "", clean).strip()
    # Older crawler names ended in a source hash; do not treat it as a word
    # when an existing PDF name becomes an extracted-directory name.
    clean = re.sub(r"_[0-9a-f]{12}$", "", clean, flags=re.IGNORECASE).strip()
    had_ellipsis = had_ellipsis or bool(re.search(r"(?:\.{3}|…)$", clean))
    clean = re.sub(r"(?:\.{3}|…)$", "", clean).rstrip()
    words = clean.split()
    if not words:
        return "article"
    visible = " ".join(words[:max_words]).rstrip(" .…")
    return visible + ("..." if len(words) > max_words or had_ellipsis else "")
