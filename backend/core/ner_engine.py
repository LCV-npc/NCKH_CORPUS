"""Deterministic rule + dictionary NER with source-safe character offsets."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from collections import defaultdict
from typing import Any, Optional

from core.ner_dict import (
    accentless_term_dict,
    dictionary_metadata,
    get_color,
    load_ner_dictionary,
    normalize_match_text,
    strip_diacritics,
    term_candidates,
    term_dict,
)


@dataclass(frozen=True)
class Token:
    word: str
    key: str
    accentless: str
    start: int
    end: int


class NEREngine:
    """Longest-match engine. It never rewrites input before computing offsets."""

    def __init__(self) -> None:
        self._fuzzy_signature_cache: tuple[int, dict[tuple[int, str, str], list[tuple[str, dict[str, Any]]]]] | None = None

    def _tokenize(self, text: str) -> list[Token]:
        """Tokenize without splitting Unicode combining marks from a word.

        Some PDFs encode ``học`` as ``ho`` followed by a combining dot/tone.
        ``\\w`` does not include those marks, which previously fabricated a
        false token ``ho`` and caused an incorrect symptom label.
        """
        tokens: list[Token] = []
        start: Optional[int] = None
        for index, char in enumerate(text):
            if char.isalnum():
                if start is None:
                    start = index
                continue
            if start is not None and unicodedata.category(char).startswith("M"):
                continue
            if start is not None:
                word = text[start:index]
                key = normalize_match_text(word)
                tokens.append(Token(word, key, strip_diacritics(key), start, index))
                start = None
        if start is not None:
            word = text[start:]
            key = normalize_match_text(word)
            tokens.append(Token(word, key, strip_diacritics(key), start, len(text)))
        return tokens

    @staticmethod
    def _case_allowed(surface: str, info: dict[str, Any]) -> bool:
        if not info.get("case_sensitive"):
            return True
        letters = "".join(char for char in surface if char.isalpha())
        return bool(letters) and letters == letters.upper()

    @staticmethod
    def _include_dictionary_suffix(text: str, char_end: int, info: dict[str, Any]) -> int:
        """Include punctuation that belongs to the original dictionary term.

        Lookup intentionally ignores punctuation, but a term such as
        ``Đau cột sống thắt lưng (Yêu thống)`` should not be displayed as an
        unmatched-looking ``... (Yêu thống``.  Only suffix punctuation that is
        explicitly present in the dictionary is included; sentence punctuation
        is never swallowed.
        """
        match_term = str(info.get("match_term") or "")
        suffix = re.search(r"([^\w\s]+)\s*$", match_term, flags=re.UNICODE)
        if not suffix:
            return char_end
        expected = suffix.group(1)
        if text.startswith(expected, char_end):
            return char_end + len(expected)
        return char_end

    def _candidates(self, text: str, tokens: list[Token]) -> list[dict[str, Any]]:
        if not tokens:
            return []
        max_words = max((len(key.split()) for key in term_dict), default=1)
        candidates: list[dict[str, Any]] = []
        for start_index in range(len(tokens)):
            for size in range(min(max_words, len(tokens) - start_index), 0, -1):
                chunk = tokens[start_index:start_index + size]
                key = " ".join(token.key for token in chunk)
                no_tone = " ".join(token.accentless for token in chunk)
                info = term_dict.get(key)
                matched_by = "exact"
                if info is None:
                    # Accentless matching is only for genuinely unaccented
                    # source text (e.g. "tang huyet ap").  It must never
                    # turn a different accented term ("u mô") into an ICD
                    # term merely because both normalize to "u mo".
                    source_key = normalize_match_text(" ".join(token.word for token in chunk))
                    if strip_diacritics(source_key) == source_key:
                        info = accentless_term_dict.get(no_tone)
                        matched_by = "accentless"
                if info is None:
                    continue
                char_start, char_end = chunk[0].start, chunk[-1].end
                char_end = self._include_dictionary_suffix(text, char_end, info)
                surface = text[char_start:char_end]
                if not self._case_allowed(surface, info):
                    continue
                if matched_by == "exact":
                    matched_by = info.get("alias_type", "exact")
                    if matched_by == "canonical":
                        matched_by = "exact"
                candidates.append({
                    "text": surface,
                    "start": char_start,
                    "end": char_end,
                    "token_count": size,
                    "icd_code": info["code"],
                    "concept_id": info.get("concept_id") or info["code"],
                    "concept_id_source": info.get("concept_id_source", ""),
                    "display_code": info.get("display_code", info["code"]),
                    "icd_label_vn": info["label_vn"],
                    "canonical_term": info.get("canonical_term", info["label_vn"]),
                    "entity_type": info["cat"],
                    "is_dagger": info.get("is_dagger", False),
                    "is_asterisk": info.get("is_asterisk", False),
                    "qualifier": info.get("qualifier", ""),
                    "matched_by": matched_by,
                    "source": info.get("source", ""),
                    "confidence": {
                        "exact": 1.0,
                        "alias": 0.98,
                        "clinical_form": 0.97,
                        "accentless": 0.94,
                    }.get(matched_by, 0.96),
                    "paired_cause_code": "",
                    "paired_cause_text": "",
                })
        return candidates

    def _fuzzy_index(self) -> dict[tuple[int, str, str], list[tuple[str, dict[str, Any]]]]:
        """Build a small deterministic index for review-only fuzzy suggestions."""
        signature = len(term_candidates)
        if self._fuzzy_signature_cache and self._fuzzy_signature_cache[0] == signature:
            return self._fuzzy_signature_cache[1]
        index: dict[tuple[int, str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for key, values in term_candidates.items():
            if len(key) < 8:
                continue
            words = key.split()
            for info in values:
                # Only unique concepts can be suggested automatically. An
                # ambiguous surface form must remain a reviewer decision.
                index[(len(words), key[0], key[-1])].append((key, info))
        self._fuzzy_signature_cache = (signature, index)
        return index

    def _suggestions(self, text: str, tokens: list[Token]) -> list[dict[str, Any]]:
        """Return ambiguous/fuzzy candidates without making final labels."""
        if not tokens:
            return []
        suggestions: list[dict[str, Any]] = []
        seen: set[tuple[int, int, str]] = set()
        max_words = max((len(key.split()) for key in term_candidates), default=1)

        # Exact but ambiguous dictionary phrases are useful reviewer prompts,
        # but never become an automatic entity.
        for start_index in range(len(tokens)):
            for size in range(min(max_words, len(tokens) - start_index), 0, -1):
                chunk = tokens[start_index:start_index + size]
                key = " ".join(token.key for token in chunk)
                values = term_candidates.get(key)
                if not values or key in term_dict:
                    continue
                char_start, char_end = chunk[0].start, chunk[-1].end
                identity = (char_start, char_end, "ambiguous")
                if identity in seen:
                    continue
                seen.add(identity)
                suggestions.append({
                    "text": text[char_start:char_end],
                    "start": char_start,
                    "end": char_end,
                    "matched_by": "ambiguous",
                    "confidence": 0.25,
                    "review_state": "UNCERTAIN",
                    "candidate_concepts": [
                        {
                            "concept_id": info.get("concept_id") or info.get("code"),
                            "concept_code": info.get("code", ""),
                            "canonical_term": info.get("canonical_term", info.get("label_vn", "")),
                            "entity_type": info.get("cat", "Bệnh Lý"),
                        }
                        for info in values
                    ],
                })
                break

        # Review-only fuzzy matching. It requires the same number of tokens,
        # similar boundary characters, a minimum surface length, and a high
        # ratio. No result from this branch enters HTML highlights.
        # Avoid turning a large abstract into an O(tokens × dictionary) review
        # job. Exact matching still runs for large texts; fuzzy discovery can
        # be requested later from a bounded corpus-review batch.
        if len(tokens) > 4000:
            return sorted(suggestions, key=lambda item: (item["start"], item["end"], item["matched_by"]))[:100]
        index = self._fuzzy_index()
        for start_index in range(len(tokens)):
            for size in range(2, min(max_words, len(tokens) - start_index) + 1):
                chunk = tokens[start_index:start_index + size]
                key = " ".join(token.key for token in chunk)
                if len(key) < 8 or key in term_dict or key in term_candidates:
                    continue
                bucket = index.get((size, key[0], key[-1]), [])
                for target_key, info in bucket:
                    if target_key == key or abs(len(target_key) - len(key)) > max(2, int(len(target_key) * 0.2)):
                        continue
                    ratio = SequenceMatcher(None, key, target_key).ratio()
                    if ratio < 0.92:
                        continue
                    char_start, char_end = chunk[0].start, chunk[-1].end
                    identity = (char_start, char_end, target_key)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    suggestions.append({
                        "text": text[char_start:char_end],
                        "start": char_start,
                        "end": char_end,
                        "matched_by": "fuzzy_candidate",
                        "confidence": round(ratio, 5),
                        "review_state": "UNCERTAIN",
                        "candidate_concepts": [{
                            "concept_id": info.get("concept_id") or info.get("code"),
                            "concept_code": info.get("code", ""),
                            "canonical_term": info.get("canonical_term", info.get("label_vn", "")),
                            "entity_type": info.get("cat", "Bệnh Lý"),
                        }],
                    })
                    break
        return sorted(suggestions, key=lambda item: (item["start"], item["end"], item["matched_by"]))[:100]

    @staticmethod
    def _resolve_overlaps(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rank = {"exact": 0, "abbreviation": 1, "alias": 2, "clinical_form": 2, "accentless": 3}
        ordered = sorted(
            candidates,
            key=lambda item: (
                -(item["end"] - item["start"]),
                rank.get(item["matched_by"], 2),
                item["start"],
                item["icd_code"],
            ),
        )
        chosen: list[dict[str, Any]] = []
        for candidate in ordered:
            if any(candidate["start"] < old["end"] and candidate["end"] > old["start"] for old in chosen):
                continue
            chosen.append(candidate)
        return sorted(chosen, key=lambda item: (item["start"], item["end"]))

    def analyze(self, text: str, enable_tone_restore: bool = False, enable_noun_phrase: bool = False) -> dict[str, Any]:
        del enable_tone_restore, enable_noun_phrase
        if not text:
            return {"entities": [], "tone_log": [], "np_matches": [], "preprocessed_text": ""}
        load_ner_dictionary()
        tokens = self._tokenize(text)
        entities = self._resolve_overlaps(self._candidates(text, tokens))
        suggestions = [
            suggestion
            for suggestion in self._suggestions(text, tokens)
            if not any(
                suggestion["start"] < entity["end"] and entity["start"] < suggestion["end"]
                for entity in entities
            )
        ]
        return {
            "entities": entities,
            "suggestions": suggestions,
            "tone_log": [],
            "np_matches": [],
            "preprocessed_text": text,
        }


_engine: Optional[NEREngine] = None


def _get_engine() -> NEREngine:
    global _engine
    if _engine is None:
        _engine = NEREngine()
    return _engine


def reset_ner_engine() -> None:
    global _engine
    _engine = None


def ner_with_fuzzy(
    text: str,
    threshold: int = 100,
    enable_tone_restore: bool = False,
    enable_noun_phrase: bool = False,
) -> dict[str, Any]:
    # ``threshold`` remains for API compatibility. Fuzzy output is review-only
    # and is never added to ``entities`` or the persisted highlight HTML.
    del threshold
    return _get_engine().analyze(text, enable_tone_restore, enable_noun_phrase)


def _build_mark(entity: dict[str, Any]) -> str:
    category = entity["entity_type"]
    code = entity.get("display_code") or entity["icd_code"]
    label = entity.get("icd_label_vn", "")
    tooltip = f"{category} | Mã: {code}"
    if label and normalize_match_text(label) != normalize_match_text(entity["text"]):
        tooltip += f" | {label}"
    return (
        '<mark class="concept-highlight" '
        f'style="background:{get_color(category)};padding:2px 5px;border-radius:4px;font-weight:600;" '
        f'title="{html.escape(tooltip, quote=True)}">{html.escape(entity["text"])}</mark>'
    )


def run_ner(
    text: str,
    threshold: int = 100,
    enable_tone_restore: bool = False,
    enable_noun_phrase: bool = False,
):
    if not text:
        return "", [], [], {
            "engine": "rule_dictionary_v2", "dictionary": dict(dictionary_metadata),
            "exact_count": 0, "alias_count": 0, "accentless_count": 0,
            "suggestions": [],
        }
    result = ner_with_fuzzy(text, threshold, enable_tone_restore, enable_noun_phrase)
    entities = result["entities"]

    pieces: list[str] = []
    cursor = 0
    for entity in entities:
        # This invariant catches regressions before corrupt HTML can be persisted.
        if text[entity["start"]:entity["end"]] != entity["text"]:
            raise ValueError("NER offset không trùng văn bản nguồn")
        pieces.append(html.escape(text[cursor:entity["start"]]))
        pieces.append(_build_mark(entity))
        cursor = entity["end"]
    pieces.append(html.escape(text[cursor:]))

    concepts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entity in entities:
        identity = (normalize_match_text(entity["text"]), entity["icd_code"], entity["entity_type"])
        if identity in seen:
            continue
        seen.add(identity)
        concepts.append({
            "name": entity["text"],
            "type": entity["entity_type"],
            "code": entity["icd_code"],
            "concept_id": entity.get("concept_id") or entity["icd_code"],
            "concept_id_source": entity.get("concept_id_source", ""),
            "display_code": entity.get("display_code", entity["icd_code"]),
            "icd_label_vn": entity["icd_label_vn"],
            "canonical_term": entity.get("canonical_term", entity["icd_label_vn"]),
            "matched_by": entity["matched_by"],
            "source": entity.get("source", ""),
            "confidence": entity.get("confidence", 1.0),
            "start": entity["start"],
            "end": entity["end"],
        })

    counts: dict[str, int] = {}
    for entity in entities:
        key = f'{entity["matched_by"]}_count'
        counts[key] = counts.get(key, 0) + 1
    preprocessing_log = {
        "engine": "rule_dictionary_v2",
        "dictionary": dict(dictionary_metadata),
        "preprocessed_text": text,
        "tone_restore": [],
        "np_count": 0,
        "suggestions": result.get("suggestions", []),
        **counts,
    }
    return "".join(pieces), concepts, entities, preprocessing_log
