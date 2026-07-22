"""
nlp_extractor.py — Trích xuất cụm danh từ (Noun Phrase) tiếng Việt

Pipeline:
1. POS-tagging văn bản bằng underthesea
2. Gom các token có nhãn N (danh từ), Np (danh từ riêng), A (tính từ) liên tiếp
   → tạo thành cụm danh từ (noun phrase)
3. Lọc bỏ stop words, từ đơn mơ hồ, chuỗi quá ngắn
4. Trả về danh sách cụm danh từ để so sánh với term_dict

Tích hợp vào NER pipeline:
- Bước 1: exact match (như hiện tại)
- Bước 2 (nếu exact miss): noun phrase extraction → so sánh với term_dict
"""

from __future__ import annotations

import re
import unicodedata
import logging
from functools import lru_cache
from typing import Optional

log = logging.getLogger(__name__)

# ── Import underthesea (lazy để tránh chậm startup) ──────────────────────────
_pos_tag_fn = None
_word_tokenize_fn = None


def _get_pos_tag():
    global _pos_tag_fn
    if _pos_tag_fn is None:
        try:
            from underthesea import pos_tag
            _pos_tag_fn = pos_tag
            log.info("underthesea pos_tag loaded OK")
        except ImportError:
            log.warning("underthesea không khả dụng — noun phrase extraction bị tắt")
            _pos_tag_fn = None
    return _pos_tag_fn


def _get_word_tokenize():
    global _word_tokenize_fn
    if _word_tokenize_fn is None:
        try:
            from underthesea import word_tokenize
            _word_tokenize_fn = word_tokenize
            log.info("underthesea word_tokenize loaded OK")
        except ImportError:
            _word_tokenize_fn = None
    return _word_tokenize_fn


# ── Nhãn POS cần giữ lại khi xây dựng noun phrase ───────────────────────────
# N: danh từ, Np: danh từ riêng, A: tính từ, M: số từ (số lượng)
# Bỏ: V (động từ), R (phó từ), P (đại từ), C (liên từ), E (giới từ), ...
_NP_POS_TAGS: frozenset[str] = frozenset({"N", "Np", "A", "M", "Nu"})

# ── Stop words y khoa — không tính làm cụm danh từ độc lập ──────────────────
_MEDICAL_STOP: frozenset[str] = frozenset({
    "bệnh", "triệu chứng", "điều trị", "xét nghiệm",
    "bệnh nhân", "bác sĩ", "thuốc", "y tế",
    "lâm sàng", "cận lâm sàng", "chẩn đoán",
    "và", "hoặc", "của", "các", "những", "với",
    "tại", "ở", "trong", "ngoài", "trên", "dưới",
})

# Độ dài tối thiểu của một noun phrase (số ký tự)
_MIN_NP_CHARS = 4
# Số token tối đa trong một noun phrase
_MAX_NP_TOKENS = 8


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text.strip().lower())


def _is_valid_np(phrase: str) -> bool:
    """Kiểm tra cụm danh từ có hợp lệ không."""
    p = phrase.strip()
    if len(p) < _MIN_NP_CHARS:
        return False
    if p.lower() in _MEDICAL_STOP:
        return False
    # Loại bỏ chuỗi chỉ có số/ký tự đặc biệt
    if re.fullmatch(r"[\d\s\.\,\%\-\+\/\(\)]+", p):
        return False
    # Phải có ít nhất 1 ký tự chữ
    if not re.search(r"[a-zA-Z\u00C0-\u1EF9]", p):
        return False
    return True


def extract_noun_phrases(text: str) -> list[str]:
    """
    Trích xuất danh sách cụm danh từ từ văn bản tiếng Việt.

    Args:
        text: Văn bản đầu vào (đã được tiền xử lý dấu).

    Returns:
        Danh sách cụm danh từ (string), đã de-duplicate, theo thứ tự xuất hiện.
    """
    if not text or not text.strip():
        return []

    pos_tag = _get_pos_tag()
    if pos_tag is None:
        # Fallback: nếu không có underthesea, dùng sliding window thông thường
        return _fallback_noun_phrases(text)

    try:
        tagged: list[tuple[str, str]] = pos_tag(text)
    except Exception as e:
        log.error("POS-tagging lỗi: %s", e)
        return _fallback_noun_phrases(text)

    noun_phrases: list[str] = []
    seen: set[str] = set()
    current_np: list[str] = []

    for token, tag in tagged:
        # Lấy prefix tag (underthesea trả về 'N', 'Np', 'V', 'A', ...)
        base_tag = tag.split("+")[0].strip() if "+" in tag else tag.strip()

        if base_tag in _NP_POS_TAGS and len(current_np) < _MAX_NP_TOKENS:
            current_np.append(token)
        else:
            if current_np:
                phrase = " ".join(current_np).strip()
                norm = _normalize(phrase)
                if _is_valid_np(phrase) and norm not in seen:
                    noun_phrases.append(phrase)
                    seen.add(norm)
                current_np = []

            # Bắt đầu NP mới nếu token hiện tại là danh từ
            if base_tag in _NP_POS_TAGS:
                current_np = [token]

    # Flush NP cuối cùng
    if current_np:
        phrase = " ".join(current_np).strip()
        norm = _normalize(phrase)
        if _is_valid_np(phrase) and norm not in seen:
            noun_phrases.append(phrase)

    return noun_phrases


def _fallback_noun_phrases(text: str) -> list[str]:
    """
    Fallback khi underthesea không khả dụng.
    Dùng tokenize đơn giản: tách câu → lấy cụm 2-4 từ loại bỏ stop words.
    """
    from core.ner_dict import STOP_WORDS  # type: ignore
    words = text.lower().split()
    phrases: list[str] = []
    seen: set[str] = set()

    for window in range(4, 1, -1):
        for i in range(len(words) - window + 1):
            chunk = words[i:i + window]
            # Bỏ qua nếu đầu/cuối là stop word
            if chunk[0] in STOP_WORDS or chunk[-1] in STOP_WORDS:
                continue
            phrase = " ".join(chunk)
            norm = _normalize(phrase)
            if _is_valid_np(phrase) and norm not in seen:
                phrases.append(phrase)
                seen.add(norm)

    return phrases


def match_noun_phrases_to_dict(
    noun_phrases: list[str],
    term_dict: dict,
) -> list[dict]:
    """
    So khớp danh sách noun phrases với term_dict.

    Args:
        noun_phrases: Danh sách cụm danh từ đã trích xuất.
        term_dict: Từ điển thuật ngữ y học (từ ner_dict.py).

    Returns:
        Danh sách kết quả khớp [{phrase, icd_code, label_vn, entity_type, ...}].
    """
    results: list[dict] = []
    seen_codes: set[str] = set()

    for phrase in noun_phrases:
        key = _normalize(phrase)
        if key in term_dict:
            info = term_dict[key]
            code = info.get("code", "")
            if code and code in seen_codes:
                continue
            if code:
                seen_codes.add(code)
            results.append({
                "text":         phrase,
                "icd_code":     code,
                "icd_label_vn": info.get("label_vn", phrase),
                "entity_type":  info.get("cat", "Bệnh Lý"),
                "is_dagger":    info.get("is_dagger", False),
                "matched_by":   "noun_phrase",
                "score":        1.0,
            })

    return results


def analyze_with_noun_phrases(
    text: str,
    term_dict: dict,
    exact_matches: list[dict],
) -> list[dict]:
    """
    Bổ sung kết quả NER bằng noun phrase matching cho những phần văn bản
    chưa được exact match bắt được.

    Args:
        text: Văn bản gốc.
        term_dict: Từ điển thuật ngữ.
        exact_matches: Danh sách entities đã khớp chính xác (từ ner_engine).

    Returns:
        Danh sách entities bổ sung (noun_phrase matches).
    """
    # Xây dựng tập mã ICD đã tìm thấy để tránh trùng lặp
    found_codes: set[str] = {e.get("icd_code", "") for e in exact_matches if e.get("icd_code")}
    found_texts: set[str] = {e.get("text", "").lower() for e in exact_matches}

    noun_phrases = extract_noun_phrases(text)
    np_matches = match_noun_phrases_to_dict(noun_phrases, term_dict)

    # Lọc bỏ trùng lặp với exact matches
    additional: list[dict] = []
    for match in np_matches:
        code = match.get("icd_code", "")
        phrase_lower = match.get("text", "").lower()
        if code and code in found_codes:
            continue
        if phrase_lower in found_texts:
            continue
        found_codes.add(code)
        found_texts.add(phrase_lower)
        additional.append(match)

    return additional


# ── Self-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    samples = [
        "Bệnh nhân bị viêm phổi cấp tính và sốt cao 39 độ",
        "Chẩn đoán: đái tháo đường type 2, tăng huyết áp, suy tim độ III",
        "Kết quả xét nghiệm máu cho thấy thiếu máu hồng cầu nhỏ",
    ]
    for s in samples:
        nps = extract_noun_phrases(s)
        print(f"TEXT : {s}")
        print(f"NPs  : {nps}")
        print()
