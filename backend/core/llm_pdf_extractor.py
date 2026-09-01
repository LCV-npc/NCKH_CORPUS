from __future__ import annotations

from collections import OrderedDict
import logging
from pathlib import Path
import re
import unicodedata
from typing import Any

from pydantic import ValidationError

from config.llm import PDFLLMSettings, PDF_TEXT_NORMALIZATION_VERSION
from core.llm_client import GeminiStructuredClient, LLMInvalidResponseError
from core.section_ontology import classify_section
from models.llm_article import (
    ArticleExtractionResult,
    ArticleSection,
    GEMINI_EXTRACTION_SCHEMA,
    LLMExtractionCandidate,
    LLMSectionCandidate,
)

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "pdf_section_extraction.md"


class LLMValidationError(LLMInvalidResponseError):
    pass


def load_pdf_extraction_prompt(path: Path = PROMPT_PATH) -> str:
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LLMValidationError(f"Không thể đọc extraction prompt: {path}") from exc
    if len(prompt) < 200 or "không" not in prompt.casefold() or "section" not in prompt.casefold():
        raise LLMValidationError("Extraction prompt bị thiếu hoặc không hợp lệ")
    return prompt


def _match_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.replace("đ", "d")
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _continuous_text(value: str | None) -> str:
    """Remove visual PDF line wrapping while preserving words and punctuation."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _is_evidenced(value: str | None, source_text: str) -> bool:
    candidate = _match_key(value or "")
    source = _match_key(source_text)
    return bool(candidate) and len(candidate) >= 3 and candidate in source


def _split_authors(value: str) -> list[str]:
    return [
        _continuous_text(item)
        for item in re.split(r"\s*[,;]\s*", value or "")
        if _continuous_text(item)
    ]


def _excluded_section(full_heading: str) -> bool:
    key = _match_key(full_heading)
    blocked = (
        "loicamon", "acknowledgment", "acknowledgement", "acknowledgments",
        "acknowledgements", "tailieuthamkhao", "reference", "references", "bibliography",
    )
    if any(key == item or key.startswith(item) for item in blocked):
        return True
    canonical_type, _, _ = classify_section(full_heading)
    return canonical_type in {"ACKNOWLEDGMENT", "REFERENCES"}


def _metadata_section(full_heading: str) -> bool:
    canonical_type, _, _ = classify_section(full_heading)
    return canonical_type in {"ABSTRACT", "KEYWORDS", "GRAPHICAL_ABSTRACT"}


class LLMPDFExtractor:
    """Hybrid extractor: LLM finds semantics, backend reconstructs exact text."""

    def __init__(
        self,
        settings: PDFLLMSettings | None = None,
        client: GeminiStructuredClient | None = None,
        prompt_path: Path = PROMPT_PATH,
    ) -> None:
        self.settings = settings or PDFLLMSettings.from_env()
        self.client = client or GeminiStructuredClient(self.settings)
        self.system_prompt = load_pdf_extraction_prompt(prompt_path)

    def extract(
        self,
        local_result: dict[str, Any],
        *,
        source_pdf: str,
        source_hash: str = "",
    ) -> ArticleExtractionResult:
        blocks = list(local_result.get("blocks") or [])
        if not blocks:
            raise LLMValidationError("PDF không có text block để LLM phân tích")

        chunks = self._build_chunks(blocks, local_result.get("headings") or [], source_pdf)
        logger.info(
            "[PDF] Prepared %s blocks in %s LLM chunk(s) for %s",
            len(blocks), len(chunks), Path(source_pdf).name,
        )
        candidates: list[LLMExtractionCandidate] = []
        for index, chunk in enumerate(chunks, 1):
            user_content = (
                f"SOURCE_PDF: {Path(source_pdf).name}\n"
                f"CHUNK: {index}/{len(chunks)}\n"
                "Chỉ dùng các block ID trong chunk dưới đây. Nếu metadata không có trong chunk, trả null/mảng rỗng.\n\n"
                f"{chunk}"
            )
            payload = self.client.generate_structured_output(
                system_prompt=self.system_prompt,
                document_content=user_content,
                response_schema=GEMINI_EXTRACTION_SCHEMA,
            )
            try:
                candidates.append(LLMExtractionCandidate.model_validate(payload))
            except ValidationError as exc:
                raise LLMValidationError("Structured JSON của Gemini sai schema") from exc

        return self._validate_and_reconstruct(
            candidates,
            blocks,
            local_result,
            source_pdf=source_pdf,
            source_hash=source_hash,
            chunk_count=len(chunks),
        )

    def _build_chunks(
        self,
        blocks: list[dict[str, Any]],
        local_headings: list[dict[str, Any]],
        source_pdf: str,
    ) -> list[str]:
        heading_keys = {
            (int(item.get("page") or 0), _match_key(item.get("heading") or ""))
            for item in local_headings
        }
        rendered: list[str] = []
        current_page: int | None = None
        for block in blocks:
            page = int(block.get("page") or 0)
            if page != current_page:
                rendered.append(f"--- PAGE {page} ---\n")
                current_page = page
            bbox = block.get("bbox") or {}
            heading_hint = (page, _match_key(block.get("text") or "")) in heading_keys
            rendered.append(
                "[{id}] page={page} font_size={size} bold={bold} "
                "bbox=({x0},{y0},{x1},{y1}) local_heading_candidate={heading}\n"
                "TEXT: {text}\n".format(
                    id=block.get("id"), page=page,
                    size=block.get("font_size", ""), bold=str(bool(block.get("bold"))).lower(),
                    x0=bbox.get("x0", ""), y0=bbox.get("y0", ""),
                    x1=bbox.get("x1", ""), y1=bbox.get("y1", ""),
                    heading=str(heading_hint).lower(), text=str(block.get("text") or ""),
                )
            )

        # Pack complete block records up to the configured context budget. A
        # record is never split, so its ID and layout evidence remain intact.
        chunks: list[str] = []
        current: list[str] = []
        length = 0
        for record in rendered:
            if current and length + len(record) > self.settings.max_input_chars:
                chunks.append("".join(current))
                current, length = [], 0
            current.append(record)
            length += len(record)
        if current:
            chunks.append("".join(current))
        return chunks

    def _validate_and_reconstruct(
        self,
        candidates: list[LLMExtractionCandidate],
        blocks: list[dict[str, Any]],
        local_result: dict[str, Any],
        *,
        source_pdf: str,
        source_hash: str,
        chunk_count: int,
    ) -> ArticleExtractionResult:
        by_id = {str(block.get("id")): block for block in blocks if block.get("id")}
        order = {block_id: index for index, block_id in enumerate(by_id)}
        full_source = "\n".join(str(block.get("text") or "") for block in blocks)

        def evidence_text(block_ids: list[str]) -> str:
            return "\n".join(
                str(by_id[block_id].get("text") or "")
                for block_id in block_ids
                if block_id in by_id
            )

        def first_supported(field: str, source_field: str) -> str | None:
            for candidate in candidates:
                value = getattr(candidate, field)
                source = evidence_text(getattr(candidate, source_field))
                if value and source and _is_evidenced(value, source):
                    return _continuous_text(value)
            return None

        title = first_supported("title", "title_source_blocks")
        if not title:
            local_title = _continuous_text(local_result.get("title"))
            title = local_title if _is_evidenced(local_title, full_source) else None

        authors: list[str] = []
        for candidate in candidates:
            source = evidence_text(candidate.author_source_blocks)
            for author in candidate.authors:
                if source and _is_evidenced(author, source) and _match_key(author) not in {_match_key(x) for x in authors}:
                    authors.append(_continuous_text(author))
        if not authors:
            authors = [
                author for author in _split_authors(str(local_result.get("authors") or ""))
                if _is_evidenced(author, full_source)
            ]

        affiliations = self._supported_candidate_values(
            candidates, "affiliations", "affiliation_source_blocks", by_id,
        )
        keywords = self._supported_candidate_values(
            candidates, "keywords", "keyword_source_blocks", by_id,
        )
        abstract = first_supported("abstract", "abstract_source_blocks")
        abstract_vi = first_supported("abstract_vi", "abstract_source_blocks")
        abstract_en = first_supported("abstract_en", "abstract_source_blocks")
        if not abstract:
            local_abstract = _continuous_text(local_result.get("abstract"))
            abstract = local_abstract if _is_evidenced(local_abstract, full_source) else None

        # The local sanitizer removes author/email/contact artefacts while
        # preserving scientific sentences. It is applied only after source
        # evidence has been verified.
        from pdf_extractor import _sanitize_abstract_content

        author_text = ", ".join(authors)
        abstract = _continuous_text(_sanitize_abstract_content(abstract or "", author_text)) or None
        abstract_vi = _continuous_text(_sanitize_abstract_content(abstract_vi or "", author_text)) or None
        abstract_en = _continuous_text(_sanitize_abstract_content(abstract_en or "", author_text)) or None

        raw_sections = [section for item in candidates for section in item.sections]
        raw_sections.extend(self._local_semantic_boundaries(local_result, blocks))
        raw_sections.extend(self._local_exclusion_boundaries(local_result, blocks))
        resolved = self._resolve_section_boundaries(raw_sections, blocks, by_id, order)
        sections = self._reconstruct_sections(resolved, blocks, order)
        if not sections:
            raise LLMValidationError("Gemini không xác định được section có bằng chứng trong PDF")

        logger.info("[VALIDATION] Accepted %s section(s) from Gemini output", len(sections))
        return ArticleExtractionResult(
            source_pdf=source_pdf,
            source_hash=source_hash,
            title=title,
            authors=authors,
            affiliations=affiliations,
            abstract=abstract,
            abstract_vi=abstract_vi,
            abstract_en=abstract_en,
            keywords=keywords,
            sections=sections,
            extraction={
                "method": "llm_hybrid",
                "provider": self.settings.provider,
                "model": self.settings.model,
                "prompt_version": self.settings.prompt_version,
                "temperature": self.settings.temperature,
                "chunk_count": chunk_count,
                "content_source": "pdf_blocks",
                "text_normalization": PDF_TEXT_NORMALIZATION_VERSION,
            },
        )

    @staticmethod
    def _supported_candidate_values(
        candidates: list[LLMExtractionCandidate],
        value_field: str,
        source_field: str,
        by_id: dict[str, dict[str, Any]],
    ) -> list[str]:
        unique: OrderedDict[str, str] = OrderedDict()
        for candidate in candidates:
            source = "\n".join(
                str(by_id[block_id].get("text") or "")
                for block_id in getattr(candidate, source_field)
                if block_id in by_id
            )
            if not source:
                continue
            for value in getattr(candidate, value_field):
                clean = _continuous_text(value)
                key = _match_key(clean)
                if key and key not in unique and _is_evidenced(clean, source):
                    unique[key] = clean
        return list(unique.values())

    @staticmethod
    def _local_semantic_boundaries(
        local_result: dict[str, Any], blocks: list[dict[str, Any]],
    ) -> list[LLMSectionCandidate]:
        """Recover high-confidence top-level headings omitted by the LLM.

        The local parser is only allowed to contribute a boundary when its
        semantic ontology and typography agree, or when the PDF explicitly
        uses a Roman numeral. Numeric subsections are left to the LLM so a
        local word such as ``Phương pháp`` cannot be promoted to a new root.
        """
        major_types = {
            "INTRODUCTION", "LITERATURE_REVIEW", "METHODS", "RESULTS",
            "RESULTS_DISCUSSION", "DISCUSSION", "CONCLUSION",
            "LIMITATIONS", "CASE_PRESENTATION",
        }
        candidates: list[LLMSectionCandidate] = []
        for heading in local_result.get("headings") or []:
            text = str(heading.get("heading") or "").strip()
            canonical = str(heading.get("canonical_type") or "")
            confidence = float(heading.get("classification_confidence") or 0)
            heading_score = float(heading.get("heading_score") or 0)
            if not text or canonical not in major_types or confidence < 0.8:
                continue
            page = int(heading.get("page") or 0)
            block = next(
                (
                    item for item in blocks
                    if int(item.get("page") or 0) == page
                    and (
                        _is_evidenced(text, str(item.get("text") or ""))
                        or _is_evidenced(str(item.get("text") or ""), text)
                    )
                ),
                None,
            )
            if not block or not block.get("id"):
                continue
            actual = str(block.get("text") or "").strip()
            roman = re.match(r"^\s*([IVXLCDM]+)\.\s+", actual, flags=re.I)
            numeric = re.match(r"^\s*\d+(?:\.\d+)*\.?\s+", actual)
            if numeric or (not roman and heading_score < 0.75):
                continue
            candidates.append(LLMSectionCandidate(
                label=roman.group(1).upper() if roman else None,
                title=text,
                full_heading=actual,
                level=1,
                parent=None,
                heading_block_id=str(block["id"]),
            ))
        return candidates

    @staticmethod
    def _local_exclusion_boundaries(
        local_result: dict[str, Any], blocks: list[dict[str, Any]],
    ) -> list[LLMSectionCandidate]:
        """Keep excluded local headings as hard stops without exporting them."""
        candidates: list[LLMSectionCandidate] = []
        for heading in local_result.get("headings") or []:
            text = str(heading.get("heading") or "").strip()
            if not text or not _excluded_section(text):
                continue
            page = int(heading.get("page") or 0)
            block = next(
                (
                    item for item in blocks
                    if int(item.get("page") or 0) == page
                    and (
                        _is_evidenced(text, str(item.get("text") or ""))
                        or _is_evidenced(str(item.get("text") or ""), text)
                    )
                ),
                None,
            )
            if block and block.get("id"):
                candidates.append(LLMSectionCandidate(
                    label=None,
                    title=text,
                    full_heading=text,
                    level=1,
                    parent=None,
                    heading_block_id=str(block["id"]),
                ))
        return candidates

    @staticmethod
    def _resolve_section_boundaries(
        sections: list[LLMSectionCandidate],
        blocks: list[dict[str, Any]],
        by_id: dict[str, dict[str, Any]],
        order: dict[str, int],
    ) -> list[tuple[LLMSectionCandidate, str]]:
        resolved: dict[str, tuple[LLMSectionCandidate, str]] = {}
        for section in sections:
            block_id = section.heading_block_id
            if block_id not in by_id:
                target = _match_key(section.full_heading)
                block_id = next(
                    (
                        str(block.get("id")) for block in blocks
                        if target and target == _match_key(block.get("text") or "")
                    ),
                    "",
                )
            if not block_id:
                continue
            heading_text = str(by_id[block_id].get("text") or "")
            if not (
                _is_evidenced(section.full_heading, heading_text)
                or _is_evidenced(heading_text, section.full_heading)
            ):
                continue
            resolved.setdefault(block_id, (section, block_id))
        return sorted(resolved.values(), key=lambda item: order[item[1]])

    @staticmethod
    def _reconstruct_sections(
        boundaries: list[tuple[LLMSectionCandidate, str]],
        blocks: list[dict[str, Any]],
        order: dict[str, int],
    ) -> list[ArticleSection]:
        accepted: list[ArticleSection] = []
        parent_keys: dict[str, str] = {}
        for index, (candidate, block_id) in enumerate(boundaries):
            start = order[block_id] + 1
            end = order[boundaries[index + 1][1]] if index + 1 < len(boundaries) else len(blocks)
            heading_block = blocks[order[block_id]]
            actual_heading = _continuous_text(heading_block.get("text"))
            if _excluded_section(actual_heading) or _metadata_section(actual_heading):
                continue
            content_blocks = [block for block in blocks[start:end] if _continuous_text(block.get("text"))]
            content = _continuous_text(" ".join(
                _continuous_text(block.get("text")) for block in content_blocks
            ))
            has_child = (
                index + 1 < len(boundaries)
                and boundaries[index + 1][0].level > candidate.level
            )
            if not content and not has_child:
                continue

            parent = candidate.parent.strip() if candidate.parent else None
            resolved_parent = parent_keys.get(_match_key(parent or "")) if parent else None
            level = candidate.level
            if level > 1 and not resolved_parent:
                previous_parent = next(
                    (item for item in reversed(accepted) if item.level < level),
                    None,
                )
                resolved_parent = previous_parent.label or previous_parent.full_heading if previous_parent else None
            if level > 1 and not resolved_parent:
                level = 1

            section = ArticleSection(
                order=len(accepted) + 1,
                label=candidate.label,
                title=candidate.title if _is_evidenced(candidate.title, actual_heading) else actual_heading,
                full_heading=actual_heading,
                level=level,
                parent=resolved_parent,
                content=content,
                source_pages=sorted(
                    {int(block.get("page") or 0) for block in content_blocks if block.get("page")}
                    or ({int(heading_block.get("page") or 0)} if heading_block.get("page") else set())
                ),
                source_blocks=[str(block.get("id")) for block in content_blocks if block.get("id")],
            )
            accepted.append(section)
            identity = section.label or section.full_heading
            parent_keys[_match_key(identity)] = identity
            parent_keys[_match_key(section.full_heading)] = identity
        return accepted
