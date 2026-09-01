"""Central ontology and deterministic semantic classifier for article sections.

The ontology deliberately separates a heading shown in a PDF from its
canonical type.  It is used as evidence in a hybrid layout/context pipeline;
it never rewrites document content.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class SectionDefinition:
    canonical_type: str
    aliases: tuple[str, ...]
    parent_types: tuple[str, ...] = ()


def normalize_heading(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    # Unicode decomposition does not convert the Vietnamese letter "đ".
    # Treat it like its ASCII search equivalent so accented and unaccented
    # headings are compared by the same deterministic key.
    value = value.replace("đ", "d")
    value = re.sub(r"^(?:\d+(?:\.\d+)*\.?|[ivxlcdm]+\.|[a-z]\.)\s+", "", value, flags=re.I)
    return re.sub(r"[^a-z0-9đ]+", " ", value).strip()


SECTION_ONTOLOGY: tuple[SectionDefinition, ...] = (
    SectionDefinition("ABSTRACT", ("tóm tắt", "abstract", "summary")),
    SectionDefinition("GRAPHICAL_ABSTRACT", ("graphical abstract",)),
    SectionDefinition("KEYWORDS", ("từ khóa", "tu khoa", "keywords", "key words")),
    SectionDefinition("INTRODUCTION", ("đặt vấn đề", "mo dau", "mở đầu", "giới thiệu", "introduction")),
    SectionDefinition("BACKGROUND", ("bối cảnh", "boi canh", "background"), ("INTRODUCTION",)),
    SectionDefinition("OBJECTIVES", ("mục tiêu", "muc tieu", "objective", "objectives", "aims"), ("INTRODUCTION",)),
    SectionDefinition("METHODS", (
        "đối tượng và phương pháp", "doi tuong va phuong phap", "phương pháp nghiên cứu",
        "phuong phap nghien cuu", "phương pháp", "materials and methods", "patients and methods", "methods",
    )),
    SectionDefinition("STUDY_POPULATION", ("đối tượng nghiên cứu", "doi tuong nghien cuu", "study population", "participants"), ("METHODS",)),
    SectionDefinition("INCLUSION_CRITERIA", ("tiêu chuẩn lựa chọn", "tieu chuan lua chon", "inclusion criteria"), ("STUDY_POPULATION", "METHODS")),
    SectionDefinition("EXCLUSION_CRITERIA", ("tiêu chuẩn loại trừ", "tieu chuan loai tru", "exclusion criteria"), ("STUDY_POPULATION", "METHODS")),
    SectionDefinition("STUDY_DESIGN", ("thiết kế nghiên cứu", "thiet ke nghien cuu", "study design"), ("METHODS",)),
    SectionDefinition("STUDY_TIME", ("thời gian nghiên cứu", "thoi gian nghien cuu", "study period", "study time"), ("METHODS",)),
    SectionDefinition("STUDY_LOCATION", ("địa điểm nghiên cứu", "dia diem nghien cuu", "study setting", "study location"), ("METHODS",)),
    SectionDefinition("SAMPLE_SIZE", ("cỡ mẫu", "co mau", "sample size"), ("METHODS",)),
    SectionDefinition("SAMPLING", ("phương pháp chọn mẫu", "phuong phap chon mau", "sampling"), ("METHODS",)),
    SectionDefinition("DATA_COLLECTION", ("thu thập số liệu", "thu thap so lieu", "data collection"), ("METHODS",)),
    SectionDefinition("STATISTICAL_ANALYSIS", ("xử lý số liệu", "xu ly so lieu", "phân tích số liệu", "statistical analysis", "data analysis"), ("METHODS",)),
    SectionDefinition("ETHICS", ("đạo đức nghiên cứu", "dao duc nghien cuu", "ethics approval", "ethical approval"), ("METHODS",)),
    SectionDefinition("RESULTS", ("kết quả", "ket qua", "kết quả nghiên cứu", "results")),
    SectionDefinition("PARTICIPANT_CHARACTERISTICS", ("đặc điểm chung", "dac diem chung", "đặc điểm đối tượng", "participant characteristics"), ("RESULTS",)),
    SectionDefinition("DISCUSSION", ("bàn luận", "ban luan", "discussion")),
    SectionDefinition("RESULTS_DISCUSSION", ("kết quả và bàn luận", "ket qua va ban luan", "results and discussion")),
    SectionDefinition("CONCLUSION", ("kết luận", "ket luan", "conclusion", "conclusions")),
    SectionDefinition("RECOMMENDATION", ("khuyến nghị", "kien nghi", "recommendation", "recommendations")),
    SectionDefinition("ACKNOWLEDGEMENT", ("lời cảm ơn", "loi cam on", "acknowledgement", "acknowledgment", "acknowledgements", "acknowledgments")),
    SectionDefinition("FUNDING", ("kinh phí", "kinh phi", "funding")),
    SectionDefinition("CONFLICT_OF_INTEREST", ("xung đột lợi ích", "xung dot loi ich", "conflict of interest", "conflict of interest statement", "competing interests")),
    SectionDefinition("DATA_AVAILABILITY", ("data availability", "availability of data and materials")),
    SectionDefinition("AUTHOR_CONTRIBUTIONS", ("author contributions", "credit authorship contribution statement")),
    SectionDefinition("SUPPLEMENTARY_DATA", ("supplementary data",)),
    SectionDefinition("REFERENCES", ("tài liệu tham khảo", "tai lieu tham khao", "references", "bibliography")),
    # Existing scientific-paper labels preserved as canonical ontology entries.
    SectionDefinition("THE_MONARCH_APPLICATION", ("the monarch application",)),
    SectionDefinition("ANALYSIS_TOOLS_POWERED_BY_MONARCH", ("analysis tools powered by monarch", "analysis tools power by monarch")),
    SectionDefinition("COMMUNITY_ENGAGEMENT", ("community engagement",)),
)


ROOT_TYPES = {
    "ABSTRACT", "KEYWORDS", "INTRODUCTION", "METHODS", "RESULTS",
    "DISCUSSION", "RESULTS_DISCUSSION", "CONCLUSION", "RECOMMENDATION",
    "ACKNOWLEDGEMENT", "REFERENCES",
}

_COMPATIBILITY_LABELS = {"ACKNOWLEDGEMENT": "acknowledgment"}


def canonical_label(canonical_type: str) -> str:
    return _COMPATIBILITY_LABELS.get(canonical_type, canonical_type.casefold())


def known_section_aliases() -> dict[str, str]:
    """Compatibility map for exact heading matching in the layout parser."""
    return {
        alias: canonical_label(definition.canonical_type)
        for definition in SECTION_ONTOLOGY
        for alias in definition.aliases
    }


def parent_types_for(canonical_type: str) -> tuple[str, ...]:
    for definition in SECTION_ONTOLOGY:
        if definition.canonical_type == canonical_type:
            return definition.parent_types
    return ()


def classify_section(heading: str, parent_type: str = "", content_preview: str = "") -> tuple[str, float, str]:
    """Classify a heading using canonical aliases and the inferred parent.

    Returns canonical type, confidence and an explainable decision source.
    Unknown headings are kept as ``OTHER`` rather than being guessed.
    """
    normalized = normalize_heading(heading)
    if not normalized:
        return "UNKNOWN", 0.0, "empty_heading"
    for definition in SECTION_ONTOLOGY:
        aliases = {normalize_heading(alias) for alias in definition.aliases}
        if normalized in aliases:
            confidence = 0.99 if not definition.parent_types or parent_type in definition.parent_types else 0.91
            return definition.canonical_type, confidence, "ontology_exact"
    heading_words = set(normalized.split())
    best: tuple[float, SectionDefinition] | None = None
    for definition in SECTION_ONTOLOGY:
        for alias in definition.aliases:
            alias_words = set(normalize_heading(alias).split())
            overlap = len(heading_words & alias_words) / max(1, len(alias_words))
            if overlap >= 0.75 and (best is None or overlap > best[0]):
                best = (overlap, definition)
    if best:
        overlap, definition = best
        return definition.canonical_type, round(0.70 + overlap * 0.20, 2), "ontology_similarity"
    return "OTHER", 0.35, "layout_candidate_without_ontology_match"
