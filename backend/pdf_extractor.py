"""Trích xuất bài báo PDF theo section bằng PyMuPDF.

Pipeline chỉ đọc text block/line/span. Image block, chữ xoay, lề ngoài,
header/footer và boilerplate xuất bản không được đưa vào kết quả.
"""

from __future__ import annotations

from collections import Counter
import io
import logging
import re
import unicodedata

from core.section_ontology import ROOT_TYPES, canonical_label, classify_section, known_section_aliases, parent_types_for

log = logging.getLogger(__name__)


_KNOWN_SECTIONS: dict[str, str] = {
    "tóm tắt": "abstract",
    "abstract": "abstract",
    "summary": "abstract",
    "graphical abstract": "graphical_abstract",
    "đặt vấn đề": "introduction",
    "giới thiệu": "introduction",
    "introduction": "introduction",
    "mở đầu": "introduction",
    "đối tượng và phương pháp nghiên cứu": "methods",
    "đối tượng và phương pháp": "methods",
    "phương pháp nghiên cứu": "methods",
    "phương pháp": "methods",
    "materials and methods": "methods",
    "methods": "methods",
    "kết quả và bàn luận": "results_discussion",
    "kết quả nghiên cứu": "results",
    "kết quả": "results",
    "results": "results",
    "discussion and future directions": "discussion",
    "bàn luận": "discussion",
    "discussion": "discussion",
    "kết luận": "conclusion",
    "conclusions": "conclusion",
    "conclusion": "conclusion",
    "the monarch application": "the_monarch_application",
    "core components": "core_components",
    "the monarch knowledge graph": "the_monarch_knowledge_graph",
    "knowledge graph ingest system": "knowledge_graph_ingest_system",
    "analysis tools power by monarch": "analysis_tools_powered_by_monarch",
    "analysis tools powered by monarch": "analysis_tools_powered_by_monarch",
    "clinical diagnostic resources": "clinical_diagnostic_resources",
    "monarch chatgpt plugin": "monarch_chatgpt_plugin",
    "grape integration advanced graph machine learning": "grape_integration",
    "grape integration: advanced graph machine learning": "grape_integration",
    "community engagement": "community_engagement",
    "data availability": "data_availability",
    "availability of data and materials": "data_availability",
    "acknowledgements": "acknowledgment",
    "acknowledgments": "acknowledgment",
    "acknowledgment": "acknowledgment",
    "lời cảm ơn": "acknowledgment",
    "funding": "funding",
    "conflict of interest statement": "conflict_of_interest",
    "conflicts of interest": "conflict_of_interest",
    "author contributions": "author_contributions",
    "supplementary data": "supplementary_data",
    "tài liệu tham khảo": "references",
    "danh mục tài liệu": "references",
    "bibliography": "references",
    "references": "references",
    "từ khóa": "keywords",
    "keywords": "keywords",
}

# Các đề mục thường gặp ở bài báo khoa học. Danh sách này là lớp nhận diện ngữ
# nghĩa; lớp bố cục bên dưới vẫn nhận được đề mục lạ nếu cả dòng thực sự có kiểu
# chữ/không gian của một heading.
_KNOWN_SECTIONS.update({
    "background": "background",
    "objectives": "objectives",
    "objective": "objectives",
    "aims": "objectives",
    "patients and methods": "methods",
    "study design": "study_design",
    "experimental procedures": "methods",
    "statistical analysis": "statistical_analysis",
    "results and discussion": "results_discussion",
    "limitations": "limitations",
    "future directions": "future_directions",
    "data and code availability": "data_availability",
    "code availability": "code_availability",
    "ethics approval": "ethics",
    "ethical approval": "ethics",
    "consent for publication": "consent",
    "competing interests": "conflict_of_interest",
    "declaration of interests": "conflict_of_interest",
    "credit authorship contribution statement": "author_contributions",
})

# The legacy map above remains only as historical context for this release.
# Parsing now reads a single ontology so aliases are not spread across the
# extraction, hierarchy and export stages.
_KNOWN_SECTIONS = known_section_aliases()

_BOILERPLATE_PATTERNS = (
    re.compile(r"^\s*(?:received|accepted|revised|published\s+online)\b", re.I),
    re.compile(r"^\s*(?:©|copyright\b)", re.I),
    re.compile(r"\bcreative\s+commons\b", re.I),
    re.compile(r"^\s*this\s+(?:(?:article|work)\s+)?is\s+an?\s+open\s+access\b", re.I),
    re.compile(r"^\s*which\s+permits\s+unrestricted\s+reuse\b", re.I),
    re.compile(r"\bdownloaded\s+from\b", re.I),
    re.compile(r"\bvol\.\s*\d+\s*,\s*(?:database\s+issue|no\.)\b", re.I),
)

_NUMBER_PREFIX = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?|[ivx]+\.|[a-z]\.)\s+", re.I
)
_NUMBERED_HEADING = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?|[ivx]+\.|[A-Z]\.)\s+\S.{1,118}$", re.I
)
_CAPTION_START = re.compile(
    r"^\s*(?:fig(?:ure)?|table)\s*(?:s?\d+|[ivx]+|[.:])\b", re.I
)
_CAPTION_LABEL_ONLY = re.compile(r"^\s*(?:fig(?:ure)?|table)s?\s*[:.]?\s*$", re.I)
_TABLE_CAPTION = re.compile(
    r"^\s*(?:tables?\s*(?:\d+|[ivx]+)[.:]?|table\s*[:.])(?:$|\s)|^\s*table\s*$", re.I
)
_LIST_PREFIX = re.compile(r"^\s*(?:[•▪◦‣]|[-*]\s|\(?\d+[.)]\s|\(?[a-z][.)]\s)", re.I)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "-", text)
    text = re.sub(r"(?<=\w)\.\s+(?=(?:com|org|io|net|edu|gov)\b)", ".", text, flags=re.I)
    text = re.sub(r"(?<=\d)\s+(?=\d{3}\b)", "", text)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_boilerplate_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and any(pattern.search(stripped) for pattern in _BOILERPLATE_PATTERNS)


def _boilerplate_key(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip()).casefold()
    return re.sub(r"\d+", "#", text)


def _strip_heading_number(text: str) -> str:
    return _NUMBER_PREFIX.sub("", text.strip()).strip().rstrip(".:–—- ")


def _compact_for_match(text: str) -> str:
    """Chuẩn hóa mạnh chỉ để so khớp heading, không làm biến dạng nội dung."""
    normalized = unicodedata.normalize("NFKD", _strip_heading_number(text).casefold())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9đ]+", "", normalized)


def _slug_label(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")[:80] or "section"


def _get_label_for_heading(heading_text: str) -> str:
    canonical_type, _, _ = classify_section(heading_text)
    if canonical_type not in {"OTHER", "UNKNOWN"}:
        return canonical_label(canonical_type)
    cleaned = _strip_heading_number(heading_text).casefold()
    compact = _compact_for_match(cleaned)
    for key, label in _KNOWN_SECTIONS.items():
        if compact == _compact_for_match(key):
            return label
    return _slug_label(cleaned)


def _known_heading_prefix(text: str) -> tuple[str, str, int] | None:
    """Trả heading/label/độ dài khi một heading chuẩn đứng đầu dòng."""
    stripped = _strip_heading_number(text)
    # A superscript footnote may be extracted as a normal suffix, e.g.
    # "TÓM TẮT8". It is a marker, not part of the heading text.
    stripped = re.sub(r"(?<=\D)[\d*]+\s*$", "", stripped).strip()
    compact = _compact_for_match(stripped)
    for key, label in sorted(_KNOWN_SECTIONS.items(), key=lambda item: len(item[0]), reverse=True):
        # PyMuPDF đôi khi chèn khoảng trắng giữa các ký tự khi font thay đổi.
        # So khớp compact giúp "Monar c h" vẫn thành "Monarch", nhưng chỉ áp
        # dụng khi toàn dòng là một đề mục đã biết.
        if compact == _compact_for_match(key):
            return key, label, len(text)

    for key, label in sorted(_KNOWN_SECTIONS.items(), key=lambda item: len(item[0]), reverse=True):
        flexible = r"\s+".join(re.escape(part) for part in key.split())
        match = re.match(rf"^\s*({flexible})(?=$|\s*[\.:–—-]\s+)", text, re.I)
        if match:
            return match.group(1).strip(), label, match.end(1)
    return None


def _is_table_or_figure_caption(text: str) -> bool:
    text = _clean_text(text).replace("\n", " ")
    return bool(_CAPTION_START.match(text) or _CAPTION_LABEL_ONLY.fullmatch(text))


def _is_table_caption(text: str) -> bool:
    text = _clean_text(text).replace("\n", " ")
    return bool(_TABLE_CAPTION.match(text))


def _bbox_overlap_ratio(bbox: tuple[float, ...], area: tuple[float, ...]) -> float:
    x0, y0, x1, y1 = bbox
    ax0, ay0, ax1, ay1 = area
    width = max(0.0, min(x1, ax1) - max(x0, ax0))
    height = max(0.0, min(y1, ay1) - max(y0, ay0))
    overlap = width * height
    own_area = max(1.0, (x1 - x0) * (y1 - y0))
    return overlap / own_area


def _table_areas_from_horizontal_rules(page, data: dict) -> list[tuple[float, float, float, float]]:
    """Nhận bảng không viền dọc từ caption + các đường kẻ ngang dài."""
    captions: list[tuple[float, float, float, float]] = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        text = " ".join(
            _line_text_from_chars(line.get("spans", []))
            for line in block.get("lines", [])
        )
        if _is_table_caption(text):
            captions.append(tuple(map(float, block.get("bbox", (0, 0, 0, 0)))))

    rules: list[tuple[float, float, float]] = []
    try:
        for drawing in page.get_drawings():
            rect = tuple(map(float, drawing.get("rect", (0, 0, 0, 0))))
            x0, y0, x1, y1 = rect
            if x1 - x0 >= float(page.rect.width) * 0.45 and abs(y1 - y0) <= 2.0:
                rules.append((x0, (y0 + y1) / 2, x1))
    except Exception as exc:
        log.debug("Không đọc được đường kẻ bảng: %s", exc)

    areas: list[tuple[float, float, float, float]] = []
    for caption in captions:
        candidates = [
            rule for rule in rules
            if caption[3] <= rule[1] <= caption[3] + float(page.rect.height) * 0.55
            and min(caption[2], rule[2]) - max(caption[0], rule[0]) > 0
        ]
        if len(candidates) >= 2:
            candidates.sort(key=lambda item: item[1])
            areas.append((
                min(rule[0] for rule in candidates), candidates[0][1] - 2,
                max(rule[2] for rule in candidates), candidates[-1][1] + 2,
            ))
    return areas


def _is_bold_span(span: dict) -> bool:
    font = str(span.get("font", "")).casefold()
    return bool(int(span.get("flags", 0)) & 16) or any(
        marker in font for marker in ("bold", "semibold", "demi", "black")
    )


def _span_text(span: dict) -> str:
    if "text" in span:
        return str(span.get("text", ""))
    return "".join(str(char.get("c", "")) for char in span.get("chars", []))


def _line_text_from_chars(spans: list[dict]) -> str:
    """Bỏ space giả do PDF đổi font giữa một từ dựa trên vị trí glyph."""
    glyphs: list[tuple[dict, float]] = []
    for span in spans:
        size = float(span.get("size", 0))
        glyphs.extend((char, size) for char in span.get("chars", []))
    if not glyphs:
        return "".join(_span_text(span) for span in spans)

    output: list[str] = []
    for index, (char, size) in enumerate(glyphs):
        value = str(char.get("c", ""))
        if value.isspace() and index + 1 < len(glyphs):
            bbox = char.get("bbox", (0, 0, 0, 0))
            next_bbox = glyphs[index + 1][0].get("bbox", (0, 0, 0, 0))
            next_overlap = float(next_bbox[0]) - float(bbox[2])
            # Space thật của font này chồng khoảng 1 px; space giả chồng hơn
            # 3 px. Ngưỡng theo font size giúp quy tắc hoạt động với PDF khác.
            if next_overlap < -max(2.0, size * 0.28):
                continue
        output.append(value)
    return "".join(output)


def _horizontal_line(line: dict) -> bool:
    direction = line.get("dir", (1.0, 0.0))
    return len(direction) == 2 and abs(float(direction[0])) >= 0.98 and abs(float(direction[1])) <= 0.08


def _make_line_record(line: dict, page_index: int, page_width: float, page_height: float) -> dict | None:
    if not _horizontal_line(line):
        return None
    spans = [span for span in line.get("spans", []) if _span_text(span)]
    if not spans:
        return None
    text = _clean_text(_line_text_from_chars(spans)).replace("\n", " ")
    if not text:
        return None
    bbox = tuple(map(float, line.get("bbox", spans[0].get("bbox", (0, 0, 0, 0)))))
    x0, _, x1, _ = bbox
    side = page_width * 0.025
    if x1 <= side or x0 >= page_width - side:
        return None

    char_count = sum(max(1, len(_span_text(span).strip())) for span in spans)
    bold_chars = sum(
        max(1, len(_span_text(span).strip()))
        for span in spans if _is_bold_span(span)
    )
    leading_bold: list[str] = []
    for span in spans:
        if _is_bold_span(span):
            leading_bold.append(_span_text(span))
        elif leading_bold:
            break

    return {
        "text": text,
        "bbox": bbox,
        "page": page_index + 1,
        "page_height": page_height,
        "size": max(float(span.get("size", 0)) for span in spans),
        "bold_ratio": bold_chars / max(1, char_count),
        "leading_bold": _clean_text("".join(leading_bold)).replace("\n", " "),
        "span_sizes": [
            (round(float(span.get("size", 0)), 1), max(1, len(_span_text(span).strip())))
            for span in spans
        ],
    }


def _extract_page_blocks(page, page_index: int, fitz_module) -> list[dict]:
    flags = fitz_module.TEXTFLAGS_TEXT | fitz_module.TEXT_DEHYPHENATE
    data = page.get_text("rawdict", flags=flags, sort=False)
    table_areas: list[tuple[float, float, float, float]] = []
    try:
        finder = page.find_tables()
        table_areas = [tuple(map(float, table.bbox)) for table in finder.tables]
    except Exception as exc:  # PDF không có đường kẻ rõ hoặc bản PyMuPDF cũ
        log.debug("Không nhận diện được vùng bảng ở trang %s: %s", page_index + 1, exc)
    table_areas.extend(_table_areas_from_horizontal_rules(page, data))

    blocks: list[dict] = []
    for raw_block_index, raw_block in enumerate(data.get("blocks", [])):
        if raw_block.get("type") != 0:  # image/vector block
            continue
        lines = [
            record for record in (
                _make_line_record(line, page_index, float(page.rect.width), float(page.rect.height))
                for line in raw_block.get("lines", [])
            ) if record is not None
        ]
        lines = [
            line for line in lines
            if not any(_bbox_overlap_ratio(line["bbox"], area) >= 0.35 for area in table_areas)
        ]
        if not lines:
            continue
        ordered_lines: list[dict] = []
        for candidate in sorted(lines, key=lambda item: item["bbox"][1]):
            insert_at = len(ordered_lines)
            for index, existing in enumerate(ordered_lines):
                same_baseline = abs(candidate["bbox"][1] - existing["bbox"][1]) <= 2
                if same_baseline and candidate["bbox"][0] < existing["bbox"][0]:
                    insert_at = index
                    break
            ordered_lines.insert(insert_at, candidate)
        block_text = " ".join(line["text"] for line in ordered_lines)
        # Bảng và caption bảng bị loại hoàn toàn. Caption hình vẫn là văn bản
        # của section hiện tại, nhưng không được nhận nhầm thành heading.
        # Image block đã bị bỏ ở nhánh type != 0 phía trên.
        if _is_table_caption(block_text):
            continue
        for line_index, line in enumerate(ordered_lines):
            line["block_line_count"] = len(ordered_lines)
            line["block_line_index"] = line_index
            line["block_id"] = f"{page_index}:{raw_block_index}"
        blocks.append({
            "bbox": (
                min(line["bbox"][0] for line in lines),
                min(line["bbox"][1] for line in lines),
                max(line["bbox"][2] for line in lines),
                max(line["bbox"][3] for line in lines),
            ),
            "lines": ordered_lines,
            "text": block_text,
        })
    return blocks


def _order_region(blocks: list[dict], page_width: float) -> list[dict]:
    if not blocks:
        return []
    center = page_width / 2
    left = [block for block in blocks if (block["bbox"][0] + block["bbox"][2]) / 2 < center]
    right = [block for block in blocks if block not in left]
    two_columns = bool(left and right) and (
        min(block["bbox"][0] for block in right) >= center - page_width * 0.08
        and max(block["bbox"][2] for block in left) <= center + page_width * 0.08
    )
    key = lambda block: (block["bbox"][1], block["bbox"][0])
    if two_columns:
        return sorted(left, key=key) + sorted(right, key=key)
    return sorted(blocks, key=key)


def _order_page_blocks(blocks: list[dict], page_width: float) -> list[dict]:
    """XY-cut đơn giản: full-width block chia trang thành các vùng, mỗi vùng đọc trái→phải."""
    center = page_width / 2
    full = [
        block for block in blocks
        if block["bbox"][0] < center - page_width * 0.08
        and block["bbox"][2] > center + page_width * 0.08
        and block["bbox"][2] - block["bbox"][0] >= page_width * 0.50
    ]
    narrow = [block for block in blocks if block not in full]
    full.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))

    ordered: list[dict] = []
    lower_bound = float("-inf")
    for wide in full:
        before = [
            block for block in narrow
            if lower_bound <= (block["bbox"][1] + block["bbox"][3]) / 2 < wide["bbox"][1]
        ]
        ordered.extend(_order_region(before, page_width))
        ordered.append(wide)
        lower_bound = max(lower_bound, wide["bbox"][3])
    remaining = [
        block for block in narrow
        if (block["bbox"][1] + block["bbox"][3]) / 2 >= lower_bound
    ]
    ordered.extend(_order_region(remaining, page_width))
    return ordered


def _remove_headers_and_footers(pages: list[list[dict]]) -> list[list[dict]]:
    boundary_counts: Counter[str] = Counter()
    for blocks in pages:
        per_page: set[str] = set()
        for block in blocks:
            for line in block["lines"]:
                _, y0, _, y1 = line["bbox"]
                height = line["page_height"]
                if y0 <= height * 0.08 or y1 >= height * 0.92:
                    per_page.add(_boilerplate_key(line["text"]))
        boundary_counts.update(key for key in per_page if len(key) >= 2)
    repeated = {key for key, count in boundary_counts.items() if count >= 2}

    cleaned_pages: list[list[dict]] = []
    for blocks in pages:
        cleaned_blocks: list[dict] = []
        for block in blocks:
            kept = []
            for line in block["lines"]:
                _, y0, _, y1 = line["bbox"]
                height = line["page_height"]
                in_margin = y0 <= height * 0.08 or y1 >= height * 0.92
                standalone_page = in_margin and bool(re.fullmatch(r"[DWS]?\s*\d+", line["text"].strip(), re.I))
                if _is_boilerplate_line(line["text"]) or standalone_page:
                    continue
                if in_margin and _boilerplate_key(line["text"]) in repeated:
                    continue
                kept.append(line)
            if kept:
                clone = dict(block)
                clone["lines"] = kept
                clone["bbox"] = (
                    min(line["bbox"][0] for line in kept), min(line["bbox"][1] for line in kept),
                    max(line["bbox"][2] for line in kept), max(line["bbox"][3] for line in kept),
                )
                cleaned_blocks.append(clone)
        cleaned_pages.append(cleaned_blocks)
    return cleaned_pages


def _serialize_pages(pages: list[list[dict]], page_widths: list[float]) -> tuple[str, list[dict]]:
    pieces: list[str] = []
    records: list[dict] = []
    position = 0
    for page_index, blocks in enumerate(pages):
        for block in _order_page_blocks(blocks, page_widths[page_index]):
            if pieces:
                separator = "\n\n" if not pieces[-1].endswith("\n") else "\n"
                pieces.append(separator)
                position += len(separator)
            for line_index, line in enumerate(block["lines"]):
                if line_index:
                    pieces.append("\n")
                    position += 1
                line = dict(line)
                line["position"] = position
                pieces.append(line["text"])
                position += len(line["text"])
                line["end_position"] = position
                records.append(line)
    return "".join(pieces).strip(), records


def _article_front_start_index(lines: list[dict]) -> int | None:
    """Locate an article front matter cluster within a concatenated PDF.

    Some journal downloads start with the tail/references of a preceding
    article and place the requested article title later on the first page.
    A valid front cluster has a larger title directly above an Abstract,
    Summary or Tóm tắt heading on the same page.
    """
    for anchor_index, anchor in enumerate(lines):
        known = _known_heading_prefix(anchor["text"])
        if not known or known[1] != "abstract":
            continue
        anchor_y0 = float(anchor["bbox"][1])
        anchor_size = float(anchor["size"])
        nearby = [
            index for index, line in enumerate(lines[:anchor_index])
            if line["page"] == anchor["page"]
            and 0 <= anchor_y0 - float(line["bbox"][3]) <= 260
            and _meaningful_heading(line["text"])
        ]
        if not nearby:
            continue
        title_size = max(float(lines[index]["size"]) for index in nearby)
        if title_size < anchor_size + 0.7:
            continue

        title_end = max(
            (index for index in nearby if abs(float(lines[index]["size"]) - title_size) <= 0.5),
            key=lambda index: float(lines[index]["bbox"][1]),
        )
        title_start = title_end
        while title_start > 0:
            previous = lines[title_start - 1]
            current = lines[title_start]
            same_size = abs(float(previous["size"]) - title_size) <= 0.5
            same_page = previous["page"] == anchor["page"]
            # Bounding boxes of two visual title lines can overlap by a tiny
            # fraction of a point in PyMuPDF.
            close = -2.5 <= float(current["bbox"][1]) - float(previous["bbox"][3]) <= title_size * 1.35
            if not (same_size and same_page and close):
                break
            title_start -= 1
        return title_start
    return None


def _trim_to_article_front(full_text: str, lines: list[dict]) -> tuple[str, list[dict]]:
    """Discard a preceding article tail when a reliable new front is found."""
    start_index = _article_front_start_index(lines)
    if start_index in (None, 0):
        return full_text, lines
    start_position = lines[start_index]["position"]
    trimmed_lines: list[dict] = []
    for line in lines[start_index:]:
        clone = dict(line)
        clone["position"] -= start_position
        clone["end_position"] -= start_position
        trimmed_lines.append(clone)
    log.info("Detected embedded article front on page %s; discarded %s leading text characters.", lines[start_index]["page"], start_position)
    return full_text[start_position:], trimmed_lines


def _body_font_size(lines: list[dict]) -> float:
    counts: Counter[float] = Counter()
    for line in lines:
        span_sizes = line.get("span_sizes") or [
            (float(line.get("size", 10.0) or 10.0), max(1, len(str(line.get("text", "")))))
        ]
        for size, weight in span_sizes:
            counts[size] += weight
    return counts.most_common(1)[0][0] if counts else 10.0


def _meaningful_heading(text: str) -> bool:
    text = text.strip()
    lowered = text.casefold()
    if not 3 <= len(text) <= 120 or len(text.split()) > 14:
        return False
    if _is_boilerplate_line(text):
        return False
    if any(token in lowered for token in ("@", "http://", "https://", "www.")):
        return False
    if re.search(r"\bvol\.?\s*\d+|\bdoi\s*:", lowered):
        return False
    return not bool(re.fullmatch(r"[\d\s.,;:/()\[\]-]+", text))


def _generic_heading_candidate(line: dict, body_size: float) -> str:
    """Nhận diện heading lạ từ *toàn dòng*, không dùng span đậm đầu dòng."""
    text = line["text"].strip()
    if line.get("block_line_count", 1) != 1:
        return ""
    if _LIST_PREFIX.match(text) or _is_table_or_figure_caption(text):
        return ""
    if text.endswith((":", ";", ",")):
        return ""
    words = re.findall(r"[^\W_]+", _strip_heading_number(text), re.UNICODE)
    # Heading một từ phổ biến đã nằm trong _KNOWN_SECTIONS. Quy tắc này loại
    # các nhãn danh sách như "Zenodo", "Deposit", "DOI", "Licensing".
    if not 2 <= len(words) <= 14:
        return ""
    strong_layout = (
        line["size"] >= body_size + 0.8
        or line["bold_ratio"] >= 0.82
        or (text.isupper() and line["size"] >= body_size - 0.1)
    )
    numbered_layout = bool(_NUMBERED_HEADING.match(text)) and (
        line["bold_ratio"] >= 0.6 or line["size"] >= body_size + 0.4
    )
    if not (strong_layout or numbered_layout):
        return ""
    if re.search(r"[.!?]\s+\S", text):
        return ""
    return text.rstrip(".:–—- ")


def _known_wrapped_heading(lines: list[dict], index: int, body_size: float) -> tuple[str, str, int] | None:
    """Ghép 2 dòng cùng block khi một heading dài bị xuống dòng."""
    if index + 1 >= len(lines):
        return None
    first, second = lines[index], lines[index + 1]
    if first.get("block_id") != second.get("block_id"):
        return None
    if abs(float(first["size"]) - float(second["size"])) > 0.2:
        return None
    if first["size"] < body_size + 0.5 and first["bold_ratio"] < 0.75:
        return None
    combined = f"{first['text'].strip()} {second['text'].strip()}"
    known = _known_heading_prefix(combined)
    if not known:
        return None
    heading, label, _ = known
    if _compact_for_match(combined) != _compact_for_match(heading):
        return None
    return heading, label, second["end_position"]


def _numbering_depth(text: str) -> int | None:
    match = re.match(r"^\s*(\d+(?:\.\d+)*|[ivxlcdm]+)\.?\s+", text, re.I)
    if not match:
        return None
    token = match.group(1)
    return token.count(".") + 1 if token[0].isdigit() else 1


def _heading_evidence(line: dict, candidate: str, body_size: float, known_label: str) -> tuple[float, dict]:
    """Explainable evidence used to decide that a layout line is a heading."""
    features = {
        "numbering": 1.0 if _numbering_depth(candidate) else 0.0,
        "typography": round(min(1.0, max(0.0, (line["size"] - body_size + 1.2) / 2.4)), 2),
        "bold": round(min(1.0, line["bold_ratio"]), 2),
        "length": 1.0 if len(candidate) <= 80 and len(candidate.split()) <= 12 else 0.35,
        "semantic": 1.0 if known_label else 0.0,
        "layout": 1.0 if line.get("block_line_count", 1) == 1 else 0.45,
    }
    score = (
        features["numbering"] * 0.22 + features["typography"] * 0.20 +
        features["bold"] * 0.20 + features["length"] * 0.10 +
        features["semantic"] * 0.20 + features["layout"] * 0.08
    )
    return round(score, 3), features


class HeadingCandidateDetector:
    """Layout-aware, explainable heading candidate detector.

    The existing parser supplies its candidate lines; this class exposes the
    score and features in the master JSON so a reviewer can inspect why a line
    was treated as a heading.
    """

    @staticmethod
    def score(line: dict, candidate: str, body_size: float, known_label: str) -> tuple[float, dict]:
        return _heading_evidence(line, candidate, body_size, known_label)


def _detect_headings(lines: list[dict]) -> list[dict]:
    body_size = _body_font_size(lines)
    headings: list[dict] = []
    seen_positions: set[int] = set()

    for line_index, line in enumerate(lines):
        text = line["text"].strip()
        candidate = ""
        end_offset = len(text)

        wrapped_known = _known_wrapped_heading(lines, line_index, body_size)
        known = _known_heading_prefix(text)
        if wrapped_known:
            candidate, known_label, absolute_end = wrapped_known
            end_offset = absolute_end - line["position"]
        elif known:
            candidate, known_label, end_offset = known
        else:
            # Cách nhận diện thứ hai dành cho PDF đặt heading đậm và câu đầu
            # cùng một dòng. Chỉ chấp nhận span đậm nếu nó khớp một heading đã
            # biết; vì vậy Zenodo/Deposit/DOI không thể cắt section.
            leading = line.get("leading_bold", "").strip().rstrip(".:–—- ")
            leading_known = _known_heading_prefix(leading) if leading else None
            if leading_known and text.casefold().startswith(leading.casefold()):
                candidate, known_label, _ = leading_known
                end_offset = len(leading)
            else:
                known_label = ""
                candidate = _generic_heading_candidate(line, body_size)
                end_offset = len(text)

        if not candidate or not _meaningful_heading(candidate):
            continue
        candidate_offset = line["text"].casefold().find(candidate.casefold())
        start = line["position"] + max(0, candidate_offset)
        if start in seen_positions:
            continue
        seen_positions.add(start)
        heading_score, features = HeadingCandidateDetector.score(line, candidate, body_size, known_label)
        canonical_type, classification_confidence, classification_source = classify_section(candidate)
        headings.append({
            "heading": candidate,
            "label": known_label or _get_label_for_heading(candidate),
            "canonical_type": canonical_type,
            "classification_confidence": classification_confidence,
            "classification_source": classification_source,
            "heading_score": heading_score,
            "features": features,
            "numbering_depth": _numbering_depth(candidate),
            "page": line["page"],
            "size": round(line["size"], 1),
            "position": start,
            "end_position": line["position"] + end_offset,
        })
    headings.sort(key=lambda item: item["position"])
    return _filter_article_headings(headings)


def _filter_article_headings(headings: list[dict]) -> list[dict]:
    if not headings:
        return []
    anchors = {"abstract", "introduction", "methods", "results"}
    start = next((i for i, heading in enumerate(headings) if heading["label"] in anchors), 0)
    filtered: list[dict] = []
    abstract_page: int | None = None
    # Objective/Methods/Results can be inline labels inside a structured
    # abstract. They must remain abstract text, not become article sections.
    abstract_inline_labels = {"objectives", "background", "methods", "results", "conclusion"}
    for heading in headings[start:]:
        label = heading["label"]
        if abstract_page is not None:
            if label == "keywords":
                abstract_page = None
            elif heading["page"] == abstract_page and label in abstract_inline_labels:
                continue
            elif label == "introduction" or heading["page"] > abstract_page:
                abstract_page = None
        # Abstract/Summary chỉ hợp lệ trước phần thân bài. Quy tắc thứ tự này
        # chặn header "Summary" trong bảng tạo ra abstract_2.
        if label == "abstract" and any(
            item["label"] in {"abstract", "introduction"}
            for item in filtered
        ):
            continue
        filtered.append(heading)
        if label == "abstract":
            abstract_page = heading["page"]
        if label == "references":
            break
    return filtered


def _split_text_by_headings(full_text: str, headings: list[dict]) -> list[dict]:
    sections: list[dict] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1]["position"] if index + 1 < len(headings) else len(full_text)
        content = full_text[heading["end_position"]:end]
        content = re.sub(r"^[\s:.\-–—]+", "", content)
        content = _normalize_section_content(content)
        sections.append({
            "heading": heading["heading"],
            "label": heading["label"],
            "page": heading["page"],
            "content": content,
        })
    return sections


def _build_hierarchical_sections(full_text: str, headings: list[dict]) -> list[dict]:
    """Build hierarchy plus direct and aggregate content from heading bounds."""
    sections: list[dict] = []
    stack: list[dict] = []
    for index, heading in enumerate(headings):
        expected_parents = parent_types_for(heading.get("canonical_type", ""))
        parent = next(
            (candidate for candidate in reversed(stack) if candidate["canonical_type"] in expected_parents),
            None,
        ) if expected_parents else None
        numbering_depth = heading.get("numbering_depth")
        if heading.get("canonical_type") in ROOT_TYPES:
            level, parent = 1, None
        elif parent is not None:
            level = parent["level"] + 1
        elif numbering_depth and numbering_depth > 1:
            level = numbering_depth
            parent = next((candidate for candidate in reversed(stack) if candidate["level"] < level), None)
        elif numbering_depth == 1:
            parent = next((candidate for candidate in reversed(stack) if candidate["level"] == 1), None)
            level = 2 if parent else 1
        else:
            # An unnumbered heading with no ontology parent is a new top-level
            # section; do not incorrectly turn every bold heading into a child.
            parent, level = None, 1

        while stack and stack[-1]["level"] >= level:
            stack.pop()
        if parent is not None and parent not in stack:
            parent = next((candidate for candidate in reversed(stack) if candidate["level"] < level), None)

        end = headings[index + 1]["position"] if index + 1 < len(headings) else len(full_text)
        raw_content = full_text[heading["end_position"]:end]
        direct_content = _normalize_section_content(re.sub(r"^[\s:.\-–—]+", "", raw_content))
        canonical_type, confidence, source = classify_section(
            heading["heading"], parent["canonical_type"] if parent else "", direct_content[:500]
        )
        raw_heading = heading["heading"]
        section = {
            "section_id": f"S{index + 1:03d}", "order": index + 1, "level": level,
            "parent_id": parent["section_id"] if parent else None, "children": [],
            "heading": _strip_heading_number(raw_heading), "original_heading": raw_heading,
            "label": canonical_label(canonical_type) if canonical_type not in {"OTHER", "UNKNOWN"} else heading["label"],
            "canonical_type": canonical_type,
            "heading_score": heading.get("heading_score", 0.0),
            "heading_features": heading.get("features", {}),
            "classification_confidence": confidence, "classification_source": source,
            "page": heading["page"], "page_start": heading["page"],
            "page_end": headings[index + 1]["page"] if index + 1 < len(headings) else heading["page"],
            "direct_content": direct_content, "content": direct_content,
            "aggregate_content": direct_content,
        }
        if parent:
            parent["children"].append(section["section_id"])
        sections.append(section)
        stack.append(section)

    by_id = {section["section_id"]: section for section in sections}
    for section in reversed(sections):
        parts = [section["direct_content"]] if section["direct_content"] else []
        parts.extend(by_id[child_id]["aggregate_content"] for child_id in section["children"] if by_id[child_id]["aggregate_content"])
        section["aggregate_content"] = "\n\n".join(parts).strip()
    return sections


def _normalize_section_content(content: str) -> str:
    """Chuẩn hóa TXT: bỏ style, nối dòng PDF và nối từ bị ngắt bằng dấu gạch."""
    kept = "\n".join(
        line for line in content.splitlines() if not _is_boilerplate_line(line)
    )
    kept = re.sub(r"(?<=\w)-\s*\n\s*(?=[a-zà-ỹ])", "", kept, flags=re.I)
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", kept):
        normalized = re.sub(r"\s*\n\s*", " ", paragraph).strip()
        if normalized:
            if (
                paragraphs
                and not re.search(r"[.!?:;\])”’]$", paragraphs[-1])
                and not _LIST_PREFIX.match(normalized)
            ):
                paragraphs[-1] = f"{paragraphs[-1]} {normalized}"
            else:
                paragraphs.append(normalized)
    return _clean_text("\n\n".join(paragraphs))


def _sanitize_abstract_content(content: str, authors: str = "") -> str:
    """Remove author/contact artefacts accidentally interleaved into an abstract."""
    if not content:
        return ""
    text = content
    # A journal footer can be interleaved into a two-column abstract. Remove
    # each known metadata field independently; never drop all text after a
    # contact marker because the second abstract column can follow it.
    text = re.sub(
        r"(?is)\*[^\n]{0,180}?(?=\s*(?:corresponding\s+author|chịu\s+trách\s+nhiệm))",
        "", text,
    )
    text = re.sub(
        r"(?is)\b(?:corresponding\s+author|chịu\s+trách\s+nhiệm(?:\s+chính)?)\s*:?\s*"
        r".*?(?=\s*(?:e[- ]?mail|phone|điện\s+thoại|fax|ngày\s+(?:nhận|phản|duyệt))\b|$)",
        "", text,
    )
    text = re.sub(
        r"(?i)\b(?:e[- ]?mail(?:\s+address)?|phone(?:\s+number)?|điện\s+thoại|fax)\s*:?\s*"
        r"(?:[A-Z0-9._%+()\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}|[+()\-\d ]{7,})",
        "", text,
    )
    text = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "", text)
    text = re.sub(
        r"(?i)\bngày\s+(?:nhận\s+bài|phản\s+biện\s+khoa\s+học|duyệt\s+bài)\s*:\s*\d{1,2}/\d{1,2}/\d{2,4}",
        "", text,
    )
    text = re.sub(
        r"(?i)(?<=\s)\d{1,3}(?=\s+(?:mục\s+tiêu|các\s+mẫu|kết\s+quả|objectives?|methods?|results?|conclusion)\b)",
        "", text,
    )

    # Remove only complete author names extracted from the front matter. This
    # avoids deleting ordinary medical terms that merely resemble a name.
    for name in re.split(r"[,;]", authors or ""):
        name = re.sub(r"[*\d]+", "", name).strip()
        if len(name.split()) >= 2 and len(name) >= 6:
            text = re.sub(rf"(?i)(?<!\w){re.escape(name)}(?!\w)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    return text.strip()


def _comparison_text(text: str) -> str:
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return re.sub(r"\s+", " ", _clean_text(text)).strip()


def _validate_sections(full_text: str, sections: list[dict]) -> dict:
    source = _comparison_text(full_text)
    issues: list[str] = []
    details: list[dict] = []
    for index, section in enumerate(sections):
        label = section["label"]
        content = _comparison_text(section.get("content", ""))
        section_issues: list[str] = []
        if content and content not in source:
            section_issues.append("content_not_found_in_source")
        if any(_is_boilerplate_line(line) for line in content.splitlines()):
            section_issues.append("contains_boilerplate")
        if index + 1 < len(sections):
            next_heading = sections[index + 1]["heading"]
            if re.search(rf"(?im)^\s*{re.escape(next_heading)}\s*$", content):
                section_issues.append("contains_next_heading")
        issues.extend(f"{label}:{issue}" for issue in section_issues)
        details.append({
            "heading": section["heading"], "label": label,
            "content_length": len(content), "source_match": "content_not_found_in_source" not in section_issues,
            "clean": not section_issues,
        })
    return {"ok": not issues, "section_count": len(sections), "issues": issues, "sections": details}


_FRONT_MATTER_EXCLUSIONS = re.compile(
    r"\b(?:t[oó]m\s*t[aá]t|abstract|summary|keywords?|t[ừu]\s*kh[oó]a|"
    r"email|e-mail|corresponding\s+author|ch[ịi]u\s+tr[aá]ch\s+nhi[ệe]m|"
    r"ng[aà]y\s+(?:nh[ậa]n|duy[ệe]t|ph[aả]n)|doi\s*:|vol(?:ume)?\.?\s*\d+|"
    r"\bjournal\b|t[ạa]p\s*ch[ií])\b",
    re.I,
)


def _line_size(line: dict) -> float:
    """Read a layout size defensively so synthetic tests remain simple."""
    try:
        return float(line.get("size", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _author_likelihood(text: str) -> float:
    """Return a conservative score that a front-matter line is an author list.

    This intentionally requires strong person-name evidence.  A short article
    title must not be discarded merely because it uses Title Case, whereas a
    comma-separated list of 2--5-word names (especially with affiliation
    markers) is a very reliable author signal in Vietnamese journal PDFs.
    """
    original = _clean_text(text)
    if not original or len(original) > 180 or _FRONT_MATTER_EXCLUSIONS.search(original):
        return 0.0
    if "@" in original or re.search(r"https?://|www\.", original, re.I):
        return 0.0

    parts = [part.strip() for part in re.split(r"\s*[,;]\s*|\s+v[àa]\s+", original) if part.strip()]
    if not parts:
        return 0.0

    valid_names = 0
    has_affiliation_marker = bool(re.search(r"(?:\d+|[*†‡])(?:\s*[,;]|$)", original))
    for part in parts:
        without_markers = re.sub(r"(?:\d+|[*†‡]+)", "", part).strip()
        words = re.findall(r"[A-Za-zÀ-ỹĐđ]+(?:[-'][A-Za-zÀ-ỹĐđ]+)?", without_markers)
        # Personal names normally have 2--5 lexical words.  Requiring the
        # complete part to be almost exclusively alphabetic avoids accepting
        # sentences such as "Kết quả điều trị...".
        alpha_coverage = sum(len(word) for word in words) / max(1, len(without_markers.replace(" ", "")))
        capitalised = sum(word[0].isupper() for word in words if word) / max(1, len(words))
        if 2 <= len(words) <= 5 and alpha_coverage >= 0.78 and capitalised >= 0.60:
            valid_names += 1

    valid_ratio = valid_names / len(parts)
    if valid_ratio < 1.0:
        return 0.0

    # One bare name is deliberately treated as uncertain.  It becomes strong
    # only when the PDF provides an affiliation/footnote marker.
    if len(parts) == 1 and not has_affiliation_marker:
        return 0.42

    score = 0.50 + min(0.30, 0.10 * valid_names)
    if len(parts) >= 2:
        score += 0.10
    if has_affiliation_marker:
        score += 0.14
    return min(1.0, score)


def _title_from_source_hint(source_hint: str) -> str:
    """Return a safe title fallback from an original PDF filename.

    Some portal PDFs expose only an author line before ``TÓM TẮT`` because the
    visual title is an image or lies in an unreadable embedded page object.  In
    that case an uploader's original filename is better provenance than
    inventing a title or writing the authors to ``title.txt``.
    """
    name = str(source_hint or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    # Crawler/upload filenames append content hashes for uniqueness.  They are
    # not part of the scholarly title.
    name = re.sub(r"(?:[_\-][0-9a-f]{12,64}){1,2}$", "", name, flags=re.I)
    name = _clean_text(name.replace("_", " "))
    if len(name) < 8 or re.fullmatch(r"(?:upload|document|scan|pdf|untitled)(?:\s*\d+)?", name, re.I):
        return ""
    if re.fullmatch(r"[0-9a-f]{12,64}", name, re.I):
        return ""
    return name


def _title_group(candidate_lines: list[dict], index: int) -> tuple[int, int, str]:
    """Join neighbouring visual title lines with matching typography."""
    title_size = _line_size(candidate_lines[index])
    size_tolerance = max(0.4, title_size * 0.05)
    start = index
    while start > 0:
        previous, current = candidate_lines[start - 1], candidate_lines[start]
        gap = float(current["bbox"][1]) - float(previous["bbox"][3])
        if (
            abs(_line_size(previous) - title_size) > size_tolerance
            or not -2.5 <= gap <= title_size * 1.35
            or _author_likelihood(previous["text"]) >= 0.70
        ):
            break
        start -= 1
    end = index
    while end + 1 < len(candidate_lines):
        current, following = candidate_lines[end], candidate_lines[end + 1]
        gap = float(following["bbox"][1]) - float(current["bbox"][3])
        if (
            abs(_line_size(following) - title_size) > size_tolerance
            or not -2.5 <= gap <= title_size * 1.35
            or _author_likelihood(following["text"]) >= 0.70
        ):
            break
        end += 1
    return start, end, _clean_text(" ".join(line["text"] for line in candidate_lines[start:end + 1]))


def _title_score(title: str, lines: list[dict], body_size: float, source_title: str) -> float:
    """Combine independent layout, language and provenance signals."""
    if not title or _FRONT_MATTER_EXCLUSIONS.search(title) or _author_likelihood(title) >= 0.70:
        return float("-inf")
    words = re.findall(r"[A-Za-zÀ-ỹĐđ]+", title)
    if len(words) < 2 or len(words) > 42 or len(title) > 300:
        return float("-inf")
    max_size = max((_line_size(line) for line in lines), default=0.0)
    bold_ratio = sum(float(line.get("bold_ratio", 0.0)) for line in lines) / max(1, len(lines))
    typography = min(3.0, max(0.0, max_size - body_size) * 0.55)
    shape = 0.55 if len(words) >= 4 else 0.20
    uppercase_ratio = sum(char.isupper() for char in title if char.isalpha()) / max(1, sum(char.isalpha() for char in title))
    case_signal = 0.35 if uppercase_ratio >= 0.45 else 0.10
    filename_signal = 0.0
    if source_title:
        title_words = {word.casefold() for word in words if len(word) >= 3}
        source_words = {word.casefold() for word in re.findall(r"[A-Za-zÀ-ỹĐđ]+", source_title) if len(word) >= 3}
        if title_words:
            filename_signal = min(0.80, len(title_words & source_words) / len(title_words))
    return typography + shape + case_signal + min(0.70, bold_ratio * 0.70) + filename_signal


def _extract_title_and_authors(
    lines: list[dict], headings: list[dict], source_hint: str = "",
) -> tuple[str, str]:
    """Extract title/authors using layout, person-name and filename evidence.

    The old implementation selected the largest line before the first heading.
    That is unsafe for pages whose actual title is graphical or absent from the
    PDF text layer: the author list can be the largest readable line.  This
    routine only accepts a title after it passes all applicable signals.
    """
    abstract_headings = []
    for heading in headings:
        known = _known_heading_prefix(str(heading.get("heading", "")))
        if str(heading.get("canonical_type", "")).casefold() == "abstract" or (known and known[1] == "abstract"):
            abstract_headings.append(heading)
    anchor = abstract_headings[0] if abstract_headings else (headings[0] if headings else None)
    source_title = _title_from_source_hint(source_hint)
    if not anchor:
        return source_title, ""

    anchor_page = anchor.get("page") or (lines[0].get("page") if lines else None)
    anchor_position = float(anchor.get("position", float("inf")))
    preamble = [
        line for line in lines
        if line.get("page") == anchor_page and float(line.get("position", 0)) < anchor_position
    ][-40:]
    if not preamble:
        return source_title, ""

    # The document-wide body font is normally the best baseline.  Tiny test
    # fixtures and malformed PDFs can contain only front matter, however, so
    # also keep the smallest visible front-matter size as a conservative
    # typography baseline.
    visible_sizes = [_line_size(line) for line in preamble if _line_size(line) > 0]
    body_size = min(_body_font_size(lines), min(visible_sizes, default=10.0))
    candidates: list[tuple[float, int, int, str]] = []
    seen_groups: set[tuple[int, int]] = set()
    for index, line in enumerate(preamble):
        text = str(line.get("text", ""))
        if not _meaningful_heading(text) or _author_likelihood(text) >= 0.70:
            continue
        start, end, title = _title_group(preamble, index)
        key = (start, end)
        if key in seen_groups:
            continue
        seen_groups.add(key)
        score = _title_score(title, preamble[start:end + 1], body_size, source_title)
        candidates.append((score, start, end, title))

    # A score below 1.35 usually means ordinary body/header text rather than a
    # typographically distinguished title.  Returning a filename fallback is
    # safer than silently assigning an author name as the title.
    best = max(candidates, default=(float("-inf"), 0, -1, ""), key=lambda item: item[0])
    title = best[3] if best[0] >= 1.35 else source_title

    author_candidates = [
        (score, index, _clean_text(str(line.get("text", ""))))
        for index, line in enumerate(preamble)
        for score in [_author_likelihood(str(line.get("text", "")))]
        if score >= 0.70
    ]
    # Prefer author lines below a detected title; otherwise preserve the best
    # strong author candidate even when title came from the filename fallback.
    if author_candidates:
        if best[0] >= 1.35:
            below_title = [item for item in author_candidates if item[1] > best[2]]
            if below_title:
                author_candidates = below_title
        authors = max(author_candidates, key=lambda item: (item[0], item[1]))[2]
    else:
        authors = ""
    return title, authors


def _document_blocks(lines: list[dict]) -> list[dict]:
    """Expose cleaned layout lines as traceable document blocks in master JSON."""
    blocks: list[dict] = []
    for order, line in enumerate(lines, 1):
        x0, y0, x1, y1 = line["bbox"]
        blocks.append({
            "id": f"B{order:04d}", "order": order, "page": line["page"],
            "text": line["text"],
            "bbox": {"x0": round(x0, 2), "y0": round(y0, 2), "x1": round(x1, 2), "y1": round(y1, 2)},
            "font_size": round(line["size"], 2), "bold": bool(line["bold_ratio"] >= 0.6),
            "line_count": line.get("block_line_count", 1),
            "previous_block": f"B{order - 1:04d}" if order > 1 else None,
            "next_block": f"B{order + 1:04d}" if order < len(lines) else None,
        })
    return blocks


def extract_from_pdf_bytes(pdf_bytes: bytes, source_hint: str = "") -> dict:
    result = {
        "title": "", "authors": "", "abstract": "", "body": "", "full_text": "",
        "page_count": 0, "sections": [], "headings": [], "blocks": [], "validation": {}, "error": None,
    }
    try:
        import pymupdf
    except ImportError:
        result["error"] = "Thiếu PyMuPDF. Chạy: pip install PyMuPDF"
        return result

    document = None
    try:
        document = pymupdf.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
        result["page_count"] = document.page_count
        raw_pages = [
            _extract_page_blocks(document[index], index, pymupdf)
            for index in range(document.page_count)
        ]
        clean_pages = _remove_headers_and_footers(raw_pages)
        page_widths = [float(document[index].rect.width) for index in range(document.page_count)]
        full_text, lines = _serialize_pages(clean_pages, page_widths)
        full_text, lines = _trim_to_article_front(full_text, lines)
        headings = _detect_headings(lines)
        sections = _build_hierarchical_sections(full_text, headings)
        if not sections and full_text:
            sections = [{
                "section_id": "S001", "order": 1, "level": 1, "parent_id": None,
                "children": [], "heading": "Content", "original_heading": "Content",
                "label": "content", "canonical_type": "OTHER", "heading_score": 0.0,
                "heading_features": {}, "classification_confidence": 0.0,
                "classification_source": "no_heading_detected", "page": 1, "page_start": 1,
                "page_end": document.page_count, "direct_content": full_text,
                "content": full_text, "aggregate_content": full_text,
            }]

        result["full_text"] = full_text
        result["headings"] = headings
        result["blocks"] = _document_blocks(lines)
        result["sections"] = sections
        result["title"], result["authors"] = _extract_title_and_authors(
            lines, headings, source_hint=source_hint,
        )
        for section in sections:
            if section["label"] != "abstract":
                continue
            cleaned_abstract = _sanitize_abstract_content(section["direct_content"], result["authors"])
            section["direct_content"] = cleaned_abstract
            section["content"] = cleaned_abstract
            section["aggregate_content"] = cleaned_abstract
        result["validation"] = _validate_sections(full_text, sections)
        result["abstract"] = next((s["content"] for s in sections if s["label"] == "abstract"), "")
        result["body"] = "\n\n".join(
            section["content"] for section in sections
            if section["label"] not in {"abstract", "references", "graphical_abstract"}
        )
    except Exception as exc:
        log.exception("Lỗi trích xuất PDF bằng PyMuPDF")
        result["error"] = f"Không thể đọc PDF bằng PyMuPDF: {exc}"
    finally:
        if document is not None:
            document.close()
    return result


def extract_from_pdf_path(pdf_path: str, source_hint: str = "") -> dict:
    try:
        with open(pdf_path, "rb") as file:
            return extract_from_pdf_bytes(file.read(), source_hint=source_hint or pdf_path)
    except FileNotFoundError:
        return {
            "title": "", "authors": "", "abstract": "", "body": "", "full_text": "",
            "page_count": 0, "sections": [], "headings": [], "blocks": [], "validation": {},
            "error": f"Không tìm thấy file: {pdf_path}",
        }
    except Exception as exc:
        return {
            "title": "", "authors": "", "abstract": "", "body": "", "full_text": "",
            "page_count": 0, "sections": [], "headings": [], "blocks": [], "validation": {}, "error": str(exc),
        }
