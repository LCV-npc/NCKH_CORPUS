"""
pdf_extractor.py — Trích xuất văn bản y học từ file PDF

Tính năng:
- Đọc PDF bằng pdfplumber
- Tự động phát hiện: tiêu đề, tác giả, tóm tắt (abstract), nội dung chính
- Làm sạch văn bản: xóa header/footer lặp lại, chuẩn hóa whitespace
- Trả về dict có cấu trúc để frontend hiển thị và đưa vào NER pipeline
"""

from __future__ import annotations

import re
import io
import unicodedata
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Từ khóa nhận diện cấu trúc bài báo y học ────────────────────────────────
_ABSTRACT_MARKERS = (
    "tóm tắt", "abstract", "tóm lược", "summary",
    "tóm tắt nghiên cứu", "kết quả nghiên cứu",
)
_INTRO_MARKERS = (
    "đặt vấn đề", "giới thiệu", "introduction", "mở đầu",
    "1. đặt vấn đề", "i. giới thiệu",
)
_METHOD_MARKERS = (
    "đối tượng", "phương pháp", "materials", "methods",
    "phương pháp nghiên cứu", "vật liệu và phương pháp",
)
_RESULT_MARKERS = (
    "kết quả", "results", "kết quả và bàn luận",
)
_CONCLUSION_MARKERS = (
    "kết luận", "conclusion", "conclusions",
)
_REFERENCE_MARKERS = (
    "tài liệu tham khảo", "references", "bibliography",
    "danh mục tài liệu",
)


def _clean_text(text: str) -> str:
    """Làm sạch văn bản PDF: chuẩn hóa Unicode, xóa artifact."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    # Xóa ký tự điều khiển (trừ newline)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Chuẩn hóa whitespace
    text = re.sub(r"[ \t]+", " ", text)
    # Xóa dòng trống liên tiếp
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_title_and_authors(first_page_text: str) -> tuple[str, str]:
    """
    Heuristic đơn giản để tìm tiêu đề và tác giả từ trang đầu.
    Chiến lược: dòng dài nhất trong 10 dòng đầu = tiêu đề.
    """
    lines = [l.strip() for l in first_page_text.split("\n") if l.strip()]
    if not lines:
        return "", ""

    # Tiêu đề: thường là dòng chữ IN HOA hoặc dòng dài ở đầu
    title = ""
    author_line = ""

    for i, line in enumerate(lines[:15]):
        # Bỏ qua dòng quá ngắn hoặc chỉ có số
        if len(line) < 10:
            continue
        if re.fullmatch(r"[\d\s\.\-\/]+", line):
            continue
        # Dòng đầu tiên hợp lệ thường là tiêu đề
        if not title:
            title = line
            # Thử tìm author ở 2-5 dòng sau tiêu đề
            for j in range(i + 1, min(i + 6, len(lines))):
                candidate = lines[j]
                # Author thường chứa dấu phẩy, viết tắt tên
                if ("," in candidate or re.search(r"\b[A-ZĐÁÉÍÓÚÀÈÌÒÙĂÂÊÔƠƯ]\.", candidate)):
                    author_line = candidate
                    break
            break

    return title, author_line


def _detect_abstract(text: str) -> str:
    """Tìm và trích xuất phần tóm tắt từ văn bản."""
    text_lower = text.lower()

    # Tìm vị trí bắt đầu của abstract
    abs_start = -1
    abs_end = len(text)

    for marker in _ABSTRACT_MARKERS:
        idx = text_lower.find(marker)
        if idx != -1 and (abs_start == -1 or idx < abs_start):
            abs_start = idx

    if abs_start == -1:
        return ""

    # Tìm phần kết thúc abstract (bắt đầu phần tiếp theo)
    for marker in _INTRO_MARKERS + _METHOD_MARKERS + _RESULT_MARKERS:
        idx = text_lower.find(marker, abs_start + 20)
        if idx != -1 and idx < abs_end:
            abs_end = idx

    abstract_raw = text[abs_start:abs_end]
    # Xóa nhãn "tóm tắt" khỏi đầu
    abstract_raw = re.sub(
        r"^(tóm\s*tắt|abstract|summary)\s*[:\-]?\s*",
        "",
        abstract_raw,
        flags=re.IGNORECASE,
    )
    return _clean_text(abstract_raw)


def _detect_body(text: str) -> str:
    """Trích xuất phần thân bài (từ intro đến references)."""
    text_lower = text.lower()

    body_start = 0
    body_end = len(text)

    # Tìm phần bắt đầu thân bài
    for marker in _INTRO_MARKERS + _METHOD_MARKERS:
        idx = text_lower.find(marker)
        if idx != -1:
            body_start = idx
            break

    # Tìm phần references (kết thúc thân bài)
    for marker in _REFERENCE_MARKERS:
        idx = text_lower.find(marker)
        if idx != -1 and idx < body_end:
            body_end = idx

    body = text[body_start:body_end]
    return _clean_text(body)


def extract_from_pdf_bytes(pdf_bytes: bytes) -> dict:
    """
    Trích xuất văn bản từ PDF bytes.

    Args:
        pdf_bytes: Nội dung file PDF dạng bytes.

    Returns:
        dict chứa: title, authors, abstract, body, full_text, page_count, error
    """
    result = {
        "title":      "",
        "authors":    "",
        "abstract":   "",
        "body":       "",
        "full_text":  "",
        "page_count": 0,
        "error":      None,
    }

    try:
        import pdfplumber
    except ImportError:
        result["error"] = "Thiếu thư viện pdfplumber. Chạy: pip install pdfplumber"
        return result

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            result["page_count"] = len(pdf.pages)
            all_text_parts: list[str] = []

            for page_idx, page in enumerate(pdf.pages):
                try:
                    page_text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                    all_text_parts.append(_clean_text(page_text))
                except Exception as e:
                    log.warning("Trang %d lỗi: %s", page_idx + 1, e)

            full_text = "\n\n".join(p for p in all_text_parts if p)
            result["full_text"] = full_text

            # Trích xuất metadata từ trang đầu
            if all_text_parts:
                title, authors = _extract_title_and_authors(all_text_parts[0])
                result["title"]   = title
                result["authors"] = authors

            # Trích xuất abstract và body
            result["abstract"] = _detect_abstract(full_text)
            result["body"]     = _detect_body(full_text) or full_text

    except Exception as e:
        log.error("Lỗi đọc PDF: %s", e)
        result["error"] = f"Không thể đọc PDF: {str(e)}"

    return result


def extract_from_pdf_path(pdf_path: str) -> dict:
    """Wrapper đọc từ đường dẫn file."""
    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        return extract_from_pdf_bytes(pdf_bytes)
    except FileNotFoundError:
        return {
            "title": "", "authors": "", "abstract": "",
            "body": "", "full_text": "", "page_count": 0,
            "error": f"Không tìm thấy file: {pdf_path}",
        }
    except Exception as e:
        return {
            "title": "", "authors": "", "abstract": "",
            "body": "", "full_text": "", "page_count": 0,
            "error": str(e),
        }


# ── Self-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        r = extract_from_pdf_path(sys.argv[1])
        print(f"Tiêu đề : {r['title']}")
        print(f"Tác giả : {r['authors']}")
        print(f"Trang   : {r['page_count']}")
        print(f"Abstract: {r['abstract'][:300]}...")
        print(f"Body    : {r['body'][:300]}...")
        if r["error"]:
            print(f"LỖI: {r['error']}")
    else:
        print("Dùng: python pdf_extractor.py <path_to_pdf>")
