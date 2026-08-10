"""Deterministic rule + dictionary NER with source-safe character offsets."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Optional

from core.ner_dict import (
    accentless_term_dict,
    dictionary_metadata,
    get_color,
    load_ner_dictionary,
    normalize_match_text,
    strip_diacritics,
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

    def _tokenize(self, text: str) -> list[Token]:
        tokens: list[Token] = []
        for match in re.finditer(r"[^\W_]+", text, flags=re.UNICODE):
            word = match.group(0)
            key = normalize_match_text(word)
            tokens.append(Token(word, key, strip_diacritics(key), match.start(), match.end()))
        return tokens

    @staticmethod
    def _case_allowed(surface: str, info: dict[str, Any]) -> bool:
        if not info.get("case_sensitive"):
            return True
        letters = "".join(char for char in surface if char.isalpha())
        return bool(letters) and letters == letters.upper()

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
                    info = accentless_term_dict.get(no_tone)
                    matched_by = "accentless"
                if info is None:
                    continue
                char_start, char_end = chunk[0].start, chunk[-1].end
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
                    "display_code": info.get("display_code", info["code"]),
                    "icd_label_vn": info["label_vn"],
                    "entity_type": info["cat"],
                    "is_dagger": info.get("is_dagger", False),
                    "is_asterisk": info.get("is_asterisk", False),
                    "qualifier": info.get("qualifier", ""),
                    "matched_by": matched_by,
                    "source": info.get("source", ""),
                    "paired_cause_code": "",
                    "paired_cause_text": "",
                })
        return candidates

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
        return {
            "entities": entities,
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
    # Parameters remain for API compatibility; this pipeline intentionally has no fuzzy/AI stage.
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
            "engine": "rule_dictionary_v1", "dictionary": dict(dictionary_metadata),
            "exact_count": 0, "alias_count": 0, "accentless_count": 0,
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
            "display_code": entity.get("display_code", entity["icd_code"]),
            "icd_label_vn": entity["icd_label_vn"],
            "matched_by": entity["matched_by"],
            "source": entity.get("source", ""),
            "start": entity["start"],
            "end": entity["end"],
        })

    counts: dict[str, int] = {}
    for entity in entities:
        key = f'{entity["matched_by"]}_count'
        counts[key] = counts.get(key, 0) + 1
    preprocessing_log = {
        "engine": "rule_dictionary_v1",
        "dictionary": dict(dictionary_metadata),
        "preprocessed_text": text,
        "tone_restore": [],
        "np_count": 0,
        **counts,
    }
    return "".join(pieces), concepts, entities, preprocessing_log
