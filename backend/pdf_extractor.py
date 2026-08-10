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
    cleaned = _strip_heading_number(heading_text).casefold()
    compact = _compact_for_match(cleaned)
    for key, label in sorted(_KNOWN_SECTIONS.items(), key=lambda item: len(item[0]), reverse=True):
        key_compact = _compact_for_match(key)
        if cleaned == key or compact == key_compact or cleaned.startswith(key + ":"):
            return label
    return _slug_label(cleaned)


def _known_heading_prefix(text: str) -> tuple[str, str, int] | None:
    """Trả heading/label/độ dài khi một heading chuẩn đứng đầu dòng."""
    stripped = _strip_heading_number(text)
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


def _body_font_size(lines: list[dict]) -> float:
    counts: Counter[float] = Counter()
    for line in lines:
        for size, weight in line["span_sizes"]:
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
        headings.append({
            "heading": candidate,
            "label": known_label or _get_label_for_heading(candidate),
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
    for heading in headings[start:]:
        # Abstract/Summary chỉ hợp lệ trước phần thân bài. Quy tắc thứ tự này
        # chặn header "Summary" trong bảng tạo ra abstract_2.
        if heading["label"] == "abstract" and any(
            item["label"] in {"abstract", "introduction", "methods", "results"}
            for item in filtered
        ):
            continue
        filtered.append(heading)
        if heading["label"] == "references":
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


def _extract_title_and_authors(lines: list[dict], headings: list[dict]) -> tuple[str, str]:
    first_anchor = headings[0]["position"] if headings else float("inf")
    preamble = [line for line in lines if line["page"] == 1 and line["position"] < first_anchor]
    if not preamble:
        return "", ""
    title_line = max(preamble[:20], key=lambda line: (line["size"], len(line["text"])), default=None)
    if not title_line:
        return "", ""
    title = title_line["text"]
    later = [line["text"] for line in preamble if line["position"] > title_line["position"]]
    return title, (later[0] if later else "")


def extract_from_pdf_bytes(pdf_bytes: bytes) -> dict:
    result = {
        "title": "", "authors": "", "abstract": "", "body": "", "full_text": "",
        "page_count": 0, "sections": [], "headings": [], "validation": {}, "error": None,
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
        headings = _detect_headings(lines)
        sections = _split_text_by_headings(full_text, headings)
        if not sections and full_text:
            sections = [{"heading": "Content", "label": "content", "page": 1, "content": full_text}]

        result["full_text"] = full_text
        result["headings"] = headings
        result["sections"] = sections
        result["validation"] = _validate_sections(full_text, sections)
        result["title"], result["authors"] = _extract_title_and_authors(lines, headings)
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


def extract_from_pdf_path(pdf_path: str) -> dict:
    try:
        with open(pdf_path, "rb") as file:
            return extract_from_pdf_bytes(file.read())
    except FileNotFoundError:
        return {
            "title": "", "authors": "", "abstract": "", "body": "", "full_text": "",
            "page_count": 0, "sections": [], "headings": [], "validation": {},
            "error": f"Không tìm thấy file: {pdf_path}",
        }
    except Exception as exc:
        return {
            "title": "", "authors": "", "abstract": "", "body": "", "full_text": "",
            "page_count": 0, "sections": [], "headings": [], "validation": {}, "error": str(exc),
        }
