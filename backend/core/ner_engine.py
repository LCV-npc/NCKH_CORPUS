"""Deterministic rule + dictionary NER with source-safe character offsets."""

from __future__ import annotations

import html
import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher
from collections import defaultdict
from typing import Any, Mapping, Optional

from core.ner_dict import (
    DictSnapshot,
    get_dictionary_metadata,
    get_color,
    get_snapshot,
    normalize_match_text,
    strip_diacritics,
)


@dataclass(frozen=True)
class Token:
    word: str
    key: str
    accentless: str
    start: int
    end: int


MATCH_METHOD_PRIORITY = {
    "exact": 100,
    "abbreviation": 95,
    "alias": 90,
    "grammar_variant": 85,
    "headword_variant": 80,
    "clinical_form": 75,
    "accentless": 60,
}


class NEREngine:
    """Longest-match engine. It never rewrites input before computing offsets."""

    def __init__(self) -> None:
        self._fuzzy_signature_cache: tuple[
            tuple[int, tuple[str, ...]],
            dict[tuple[int, str, str], list[tuple[str, Mapping[str, Any]]]],
        ] | None = None

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
        # Lookup keys are case-folded for the index, so a case-sensitive alias
        # needs an explicit comparison against its original surface form.
        # This is stricter than merely requiring uppercase text and also
        # preserves aliases such as ``Ab`` when they are intentionally
        # case-sensitive.
        expected = re.sub(r"\s+", " ", str(info.get("match_term") or "")).strip()
        actual = re.sub(r"\s+", " ", surface).strip()
        if not expected:
            return False
        if actual == expected:
            return True
        if not info.get("allow_accentless"):
            return False

        def remove_marks(value: str) -> str:
            value = unicodedata.normalize("NFC", value).replace("đ", "d").replace("Đ", "D")
            return "".join(
                char
                for char in unicodedata.normalize("NFD", value)
                if not unicodedata.category(char).startswith("M")
            )

        return remove_marks(actual) == remove_marks(expected)

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

    def _candidates(
        self,
        text: str,
        tokens: list[Token],
        snapshot: DictSnapshot,
        allowed_sources: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not tokens:
            return []
        term_dict = snapshot.term_dict
        accentless_term_dict = snapshot.accentless_term_dict
        max_words = snapshot.max_words
        candidates: list[dict[str, Any]] = []
        for start_index in range(len(tokens)):
            first_key = tokens[start_index].key
            first_limit = snapshot.max_words_by_first_token.get(first_key, max_words)
            for size in range(min(first_limit, len(tokens) - start_index), 0, -1):
                chunk = tokens[start_index:start_index + size]
                key = " ".join(token.key for token in chunk)
                no_tone = " ".join(token.accentless for token in chunk)
                if allowed_sources is None:
                    info = term_dict.get(key)
                else:
                    eligible = [
                        value
                        for value in snapshot.term_candidates.get(key, ())
                        if str(value.get("concept_id_source") or "").strip().upper() in allowed_sources
                        and value.get("auto_matchable", True)
                    ]
                    info = eligible[0] if len(eligible) == 1 else None
                matched_by = "exact"
                if info is None:
                    # Accentless matching is only for genuinely unaccented
                    # source text (e.g. "tang huyet ap").  It must never
                    # turn a different accented term ("u mô") into an ICD
                    # term merely because both normalize to "u mo".
                    source_key = normalize_match_text(" ".join(token.word for token in chunk))
                    if strip_diacritics(source_key) == source_key:
                        if allowed_sources is None:
                            info = accentless_term_dict.get(no_tone)
                        else:
                            eligible = [
                                value
                                for value in snapshot.accentless_term_candidates.get(no_tone, ())
                                if str(value.get("concept_id_source") or "").strip().upper() in allowed_sources
                                and value.get("auto_matchable", True)
                            ]
                            info = eligible[0] if len(eligible) == 1 else None
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
                    "rule_score": info.get("rule_score") if info.get("rule_score") is not None else {
                        "exact": 1.0,
                        "alias": 0.98,
                        "grammar_variant": 0.98,
                        "clinical_form": 0.97,
                        "accentless": 0.94,
                    }.get(matched_by, 0.96),
                    "score_type": info.get("score_type") or "heuristic",
                    "score_calibrated": bool(info.get("score_calibrated", False)),
                    "confidence": info.get("rule_score") if info.get("rule_score") is not None else {
                        "exact": 1.0,
                        "alias": 0.98,
                        "grammar_variant": 0.98,
                        "clinical_form": 0.97,
                        "accentless": 0.94,
                    }.get(matched_by, 0.96),
                    "icd_chapter_code": info.get("icd_chapter_code", ""),
                    "icd_chapter_name": info.get("icd_chapter_name", ""),
                    "paired_cause_code": "",
                    "paired_cause_text": "",
                })
        return candidates

    def _fuzzy_index(
        self,
        snapshot: DictSnapshot,
        allowed_sources: set[str] | None = None,
    ) -> dict[tuple[int, str, str], list[tuple[str, Mapping[str, Any]]]]:
        """Build a small deterministic index for review-only fuzzy suggestions."""
        source_signature = tuple(sorted(allowed_sources)) if allowed_sources is not None else ()
        signature = (snapshot.generation, source_signature)
        if self._fuzzy_signature_cache and self._fuzzy_signature_cache[0] == signature:
            return self._fuzzy_signature_cache[1]
        index: dict[tuple[int, str, str], list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
        for key, values in snapshot.term_candidates.items():
            if len(key) < 8:
                continue
            words = key.split()
            for info in values:
                if not info.get("auto_matchable", True):
                    continue
                if allowed_sources is not None and str(info.get("concept_id_source") or "").strip().upper() not in allowed_sources:
                    continue
                # Only unique concepts can be suggested automatically. An
                # ambiguous surface form must remain a reviewer decision.
                index[(len(words), key[0], key[-1])].append((key, info))
        self._fuzzy_signature_cache = (signature, index)
        return index

    def _suggestions(
        self,
        text: str,
        tokens: list[Token],
        snapshot: DictSnapshot,
        allowed_sources: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return ambiguous/fuzzy candidates without making final labels."""
        if not tokens:
            return []
        suggestions: list[dict[str, Any]] = []
        seen: set[tuple[int, int, str]] = set()
        term_candidates = snapshot.term_candidates
        term_dict = snapshot.term_dict
        max_words = snapshot.max_words

        # Exact but ambiguous dictionary phrases are useful reviewer prompts,
        # but never become an automatic entity.
        for start_index in range(len(tokens)):
            for size in range(min(max_words, len(tokens) - start_index), 0, -1):
                chunk = tokens[start_index:start_index + size]
                key = " ".join(token.key for token in chunk)
                values = tuple(
                    value
                    for value in term_candidates.get(key, ())
                    if allowed_sources is None
                    or str(value.get("concept_id_source") or "").strip().upper() in allowed_sources
                )
                if not values:
                    continue
                if allowed_sources is None and key in term_dict:
                    continue
                if allowed_sources is not None and len(
                    [value for value in values if value.get("auto_matchable", True)]
                ) == 1:
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
                            "concept_id_source": info.get("concept_id_source", ""),
                            "icd_chapter_code": info.get("icd_chapter_code", ""),
                            "icd_chapter_name": info.get("icd_chapter_name", ""),
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
        index = self._fuzzy_index(snapshot, allowed_sources)
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
                            "concept_id_source": info.get("concept_id_source", ""),
                            "icd_chapter_code": info.get("icd_chapter_code", ""),
                            "icd_chapter_name": info.get("icd_chapter_name", ""),
                        }],
                    })
                    break
        return sorted(suggestions, key=lambda item: (item["start"], item["end"], item["matched_by"]))[:100]

    @staticmethod
    def _candidate_weight(candidate: dict[str, Any]) -> int:
        """Return a deterministic weight for non-overlapping span selection.

        Match quality dominates span length.  Token count and character length
        then preserve longest-match behaviour among candidates of the same
        quality.  The value is deliberately a ranking score, not a probability.
        """
        method = str(candidate.get("matched_by") or "").lower()
        method_score = MATCH_METHOD_PRIORITY.get(method, 50)
        token_count = int(candidate.get("token_count") or 1)
        span_length = int(candidate["end"]) - int(candidate["start"])
        return method_score * 1_000_000 + token_count * 1_000 + span_length

    @staticmethod
    def _plan_signature(plan: tuple[int, tuple[int, ...]], ordered: list[dict[str, Any]]) -> tuple[tuple[int, int, str], ...]:
        """Provide stable tie-breaking for equally weighted interval plans."""
        _, indexes = plan
        return tuple(
            sorted(
                (
                    int(ordered[index]["start"]),
                    int(ordered[index]["end"]),
                    str(ordered[index].get("concept_id") or ordered[index].get("icd_code") or ""),
                )
                for index in indexes
            )
        )

    @classmethod
    def _better_plan(
        cls,
        candidate: tuple[int, tuple[int, ...]],
        current: tuple[int, tuple[int, ...]],
        ordered: list[dict[str, Any]],
    ) -> tuple[int, tuple[int, ...]]:
        if candidate[0] != current[0]:
            return candidate if candidate[0] > current[0] else current
        return candidate if cls._plan_signature(candidate, ordered) < cls._plan_signature(current, ordered) else current

    @classmethod
    def _resolve_overlaps(cls, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Choose the highest-quality non-overlapping set of spans.

        Candidate generation remains longest-first, but conflict resolution is
        weighted interval scheduling instead of selecting the first long span.
        This allows two high-quality exact entities to beat one lower-quality
        variant that overlaps both of them.
        """
        if not candidates:
            return []

        def dominates(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
            """Drop a nested weaker/equal match when a complete stronger span exists."""
            outer_start, outer_end = int(outer["start"]), int(outer["end"])
            inner_start, inner_end = int(inner["start"]), int(inner["end"])
            if (outer_start, outer_end) == (inner_start, inner_end):
                return False
            if outer_start > inner_start or outer_end < inner_end:
                return False
            outer_method = str(outer.get("matched_by") or "").lower()
            inner_method = str(inner.get("matched_by") or "").lower()
            outer_priority = MATCH_METHOD_PRIORITY.get(outer_method, 50)
            inner_priority = MATCH_METHOD_PRIORITY.get(inner_method, 50)
            return (
                outer_priority > inner_priority
                or (
                    outer_priority == inner_priority
                    and int(outer.get("token_count") or 1) >= int(inner.get("token_count") or 1)
                )
            )

        # A complete canonical exact term must not be split into nested exact
        # and alias fragments.  We only remove nested candidates when the
        # containing span is at least as authoritative; a weaker long variant
        # therefore cannot suppress two stronger exact spans.
        candidates = [
            candidate
            for candidate in candidates
            if not any(
                dominates(other, candidate)
                for other in candidates
                if other is not candidate
            )
        ]
        if not candidates:
            return []

        ordered = sorted(
            candidates,
            key=lambda item: (
                int(item["end"]),
                int(item["start"]),
                -MATCH_METHOD_PRIORITY.get(str(item.get("matched_by") or "").lower(), 50),
                -int(item.get("token_count") or 1),
                str(item.get("concept_id") or item.get("icd_code") or ""),
            ),
        )
        end_positions = [int(item["end"]) for item in ordered]
        previous: list[int] = []
        for index, item in enumerate(ordered):
            previous.append(bisect_right(end_positions, int(item["start"]), 0, index) - 1)

        empty_plan: tuple[int, tuple[int, ...]] = (0, ())
        best: list[tuple[int, tuple[int, ...]]] = [empty_plan]
        for index, item in enumerate(ordered, start=1):
            skip = best[index - 1]
            prior_plan = best[previous[index - 1] + 1]
            take = (
                prior_plan[0] + cls._candidate_weight(item),
                prior_plan[1] + (index - 1,),
            )
            best.append(cls._better_plan(take, skip, ordered))

        selected = [ordered[index] for index in best[-1][1]]
        return sorted(selected, key=lambda item: (int(item["start"]), int(item["end"])))

    def analyze(
        self,
        text: str,
        enable_tone_restore: bool = False,
        enable_noun_phrase: bool = False,
        include_suggestions: bool = True,
        allowed_sources: set[str] | None = None,
    ) -> dict[str, Any]:
        del enable_tone_restore, enable_noun_phrase
        snapshot = get_snapshot()
        if not text:
            return {
                "entities": [],
                "suggestions": [],
                "tone_log": [],
                "np_matches": [],
                "preprocessed_text": "",
                "snapshot_generation": snapshot.generation,
                "dictionary": get_dictionary_metadata(snapshot),
            }
        tokens = self._tokenize(text)
        normalized_sources = (
            {str(source).strip().upper() for source in allowed_sources if str(source).strip()}
            if allowed_sources is not None
            else None
        )
        entities = self._resolve_overlaps(self._candidates(text, tokens, snapshot, normalized_sources))
        suggestions = []
        if include_suggestions:
            suggestions = [
                suggestion
                for suggestion in self._suggestions(text, tokens, snapshot, normalized_sources)
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
            "snapshot_generation": snapshot.generation,
            "dictionary": get_dictionary_metadata(snapshot),
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
    include_suggestions: bool = True,
    allowed_sources: set[str] | None = None,
) -> dict[str, Any]:
    # ``threshold`` remains for API compatibility. Fuzzy output is review-only
    # and is never added to ``entities`` or the persisted highlight HTML.
    del threshold
    return _get_engine().analyze(
        text,
        enable_tone_restore,
        enable_noun_phrase,
        include_suggestions=include_suggestions,
        allowed_sources=allowed_sources,
    )


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
    include_suggestions: bool = True,
    allowed_sources: set[str] | None = None,
):
    if not text:
        snapshot = get_snapshot()
        return "", [], [], {
            "engine": "rule_dictionary_v2", "dictionary": get_dictionary_metadata(snapshot),
            "snapshot_generation": snapshot.generation,
            "exact_count": 0, "alias_count": 0, "accentless_count": 0,
            "suggestions": [],
        }
    result = ner_with_fuzzy(
        text,
        threshold,
        enable_tone_restore,
        enable_noun_phrase,
        include_suggestions=include_suggestions,
        allowed_sources=allowed_sources,
    )
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
            "rule_score": entity.get("rule_score", entity.get("confidence")),
            "score_type": entity.get("score_type", "heuristic"),
            "score_calibrated": entity.get("score_calibrated", False),
            "confidence": entity.get("confidence", 1.0),
            "icd_chapter_code": entity.get("icd_chapter_code", ""),
            "icd_chapter_name": entity.get("icd_chapter_name", ""),
            "start": entity["start"],
            "end": entity["end"],
        })

    counts: dict[str, int] = {}
    for entity in entities:
        key = f'{entity["matched_by"]}_count'
        counts[key] = counts.get(key, 0) + 1
    preprocessing_log = {
        "engine": "rule_dictionary_v2",
        "dictionary": result.get("dictionary") or {},
        "preprocessed_text": text,
        "tone_restore": [],
        "np_count": 0,
        "suggestions_enabled": include_suggestions,
        "suggestions": result.get("suggestions", []),
        **counts,
    }
    return "".join(pieces), concepts, entities, preprocessing_log
