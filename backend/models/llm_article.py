from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMSectionCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str | None = None
    title: str
    full_heading: str
    level: int = Field(default=1, ge=1, le=8)
    parent: str | None = None
    heading_block_id: str

    @field_validator("title", "full_heading", "heading_block_id")
    @classmethod
    def required_text(cls, value: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise ValueError("Giá trị bắt buộc không được rỗng")
        return clean


class LLMExtractionCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    title_source_blocks: list[str] = Field(default_factory=list)
    authors: list[str] = Field(default_factory=list)
    author_source_blocks: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)
    affiliation_source_blocks: list[str] = Field(default_factory=list)
    abstract: str | None = None
    abstract_vi: str | None = None
    abstract_en: str | None = None
    abstract_source_blocks: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    keyword_source_blocks: list[str] = Field(default_factory=list)
    sections: list[LLMSectionCandidate] = Field(default_factory=list)


class ArticleSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1)
    label: str | None = None
    title: str
    full_heading: str
    level: int = Field(default=1, ge=1, le=8)
    parent: str | None = None
    content: str
    source_pages: list[int] = Field(default_factory=list)
    source_blocks: list[str] = Field(default_factory=list)


class ArticleExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_pdf: str
    source_hash: str = ""
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)
    abstract: str | None = None
    abstract_vi: str | None = None
    abstract_en: str | None = None
    keywords: list[str] = Field(default_factory=list)
    sections: list[ArticleSection] = Field(default_factory=list)
    extraction: dict[str, Any] = Field(default_factory=dict)


GEMINI_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING", "nullable": True},
        "title_source_blocks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "authors": {"type": "ARRAY", "items": {"type": "STRING"}},
        "author_source_blocks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "affiliations": {"type": "ARRAY", "items": {"type": "STRING"}},
        "affiliation_source_blocks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "abstract": {"type": "STRING", "nullable": True},
        "abstract_vi": {"type": "STRING", "nullable": True},
        "abstract_en": {"type": "STRING", "nullable": True},
        "abstract_source_blocks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "keyword_source_blocks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "sections": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "label": {"type": "STRING", "nullable": True},
                    "title": {"type": "STRING"},
                    "full_heading": {"type": "STRING"},
                    "level": {"type": "INTEGER"},
                    "parent": {"type": "STRING", "nullable": True},
                    "heading_block_id": {"type": "STRING"},
                },
                "required": ["title", "full_heading", "level", "heading_block_id"],
            },
        },
    },
    "required": [
        "title_source_blocks", "authors", "author_source_blocks", "affiliations",
        "affiliation_source_blocks", "abstract_source_blocks", "keywords",
        "keyword_source_blocks", "sections",
    ],
}
