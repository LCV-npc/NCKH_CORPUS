"""
tone_restore.py — Khôi phục dấu tiếng Việt bị thiếu/sai

Chiến lược:
1. Phát hiện các từ/cụm từ không có dấu (hoặc ít dấu hơn bình thường)
   dựa trên heuristic: nếu từ chỉ gồm ký tự ASCII a-z (không có dấu).
2. Tra từ điển phoneme nội bộ (không cần model AI, nhanh).
3. Với từ không có trong whitelist → giữ nguyên (an toàn).
4. Log lại các từ đã sửa để debug.
"""

from __future__ import annotations

import re
import unicodedata
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Từ điển ánh xạ: từ không dấu → từ có dấu phổ biến nhất ─────────────────
# Tập trung vào các thuật ngữ y học / từ thông dụng hay bị thiếu dấu
_TONE_MAP: dict[str, str] = {
    # ── Thân thể / giải phẫu ──
    "nao":        "não",
    "tim":        "tim",
    "gan":        "gan",
    "phoi":       "phổi",
    "than":       "thận",
    "ruot":       "ruột",
    "da day":     "dạ dày",
    "tuyen":      "tuyến",
    "xuong":      "xương",
    "co":         "cơ",
    "mach":       "mạch",
    "mau":        "máu",
    "huyet":      "huyết",
    "te bao":     "tế bào",
    "mo":         "mô",
    # ── Bệnh / triệu chứng ──
    "viem":       "viêm",
    "dau":        "đau",
    "sot":        "sốt",
    "ho":         "ho",
    "kho tho":    "khó thở",
    "xuat huyet": "xuất huyết",
    "ung thu":    "ung thư",
    "tieu duong": "tiểu đường",
    "cao huyet ap": "cao huyết áp",
    "thap khop":  "thấp khớp",
    "suy tim":    "suy tim",
    "suy than":   "suy thận",
    "suy gan":    "suy gan",
    "dot quy":    "đột quỵ",
    "nhoi mau co tim": "nhồi máu cơ tim",
    "ung thu phoi": "ung thư phổi",
    "viem phoi":  "viêm phổi",
    "viem gan":   "viêm gan",
    "viem khop":  "viêm khớp",
    "viem ruot":  "viêm ruột",
    "tieu chay":  "tiêu chảy",
    "tao bon":    "táo bón",
    "buon non":   "buồn nôn",
    "chong mat":  "chóng mặt",
    "mat ngu":    "mất ngủ",
    "lo au":      "lo âu",
    "tram cam":   "trầm cảm",
    "parkinson":  "parkinson",
    "alzheimer":  "alzheimer",
    # ── Điều trị / thủ thuật ──
    "phau thuat": "phẫu thuật",
    "xa tri":     "xạ trị",
    "hoa tri":    "hóa trị",
    "ghep":       "ghép",
    "noi soi":    "nội soi",
    "cat bo":     "cắt bỏ",
    "giam dau":   "giảm đau",
    "khang sinh": "kháng sinh",
    "vaccine":    "vaccine",
    "tiem":       "tiêm",
    "uong thuoc": "uống thuốc",
    # ── Xét nghiệm / chẩn đoán ──
    "xet nghiem": "xét nghiệm",
    "sieu am":    "siêu âm",
    "chup x quang": "chụp X-quang",
    "ct scan":    "CT scan",
    "mri":        "MRI",
    "sinh thiet": "sinh thiết",
    "noi soi da day": "nội soi dạ dày",
    # ── Từ phổ thông hay bị thiếu dấu ──
    "benh":       "bệnh",
    "benh nhan":  "bệnh nhân",
    "bac si":     "bác sĩ",
    "thuoc":      "thuốc",
    "kham benh":  "khám bệnh",
    "dieu tri":   "điều trị",
    "chan doan":  "chẩn đoán",
    "trieu chung": "triệu chứng",
    "bien chung": "biến chứng",
    "tai phat":   "tái phát",
    "phong ngua": "phòng ngừa",
    "suc khoe":   "sức khỏe",
    "y te":       "y tế",
    "nha thuoc":  "nhà thuốc",
    "benh vien":  "bệnh viện",
    "phong kham": "phòng khám",
}

# Tiền xử lý để tra cứu nhanh: lowercase + remove multiple spaces
_TONE_LOOKUP: dict[str, str] = {
    k.strip().lower(): v for k, v in _TONE_MAP.items()
}

# Chiều dài tối đa của cụm từ cần kiểm tra (số từ)
_MAX_PHRASE_LEN = max(len(k.split()) for k in _TONE_LOOKUP.keys())


def _strip_diacritics(text: str) -> str:
    """Loại bỏ dấu thanh và dấu chữ cái tiếng Việt → ASCII thuần."""
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _is_no_tone_word(word: str) -> bool:
    """
    Kiểm tra xem word có phải là từ tiếng Việt KHÔNG CÓ DẤU không.
    Điều kiện: chỉ chứa ký tự ASCII a-z (sau khi loại dấu, giống hệt gốc).
    """
    w = word.lower()
    return bool(re.fullmatch(r"[a-z]+", w)) and len(w) >= 2


def _has_vietnamese_chars(text: str) -> bool:
    """Kiểm tra text có chứa ký tự tiếng Việt có dấu không."""
    for ch in text:
        if ord(ch) > 127:
            return True
    return False


def restore_tones(text: str) -> tuple[str, list[dict]]:
    """
    Khôi phục dấu tiếng Việt trong văn bản bị thiếu/sai dấu.

    Args:
        text: Văn bản đầu vào (có thể thiếu dấu).

    Returns:
        (restored_text, changes_log)
        - restored_text: Văn bản đã được phục hồi dấu.
        - changes_log: Danh sách các thay đổi [{original, corrected, position}].
    """
    if not text or not text.strip():
        return text, []

    # Nếu văn bản đã đầy đủ dấu tiếng Việt → bỏ qua (tối ưu hóa)
    # Heuristic: nếu > 30% ký tự là tiếng Việt có dấu thì coi là đã có dấu
    viet_chars = sum(1 for c in text if ord(c) > 127)
    total_alpha = sum(1 for c in text if c.isalpha())
    if total_alpha > 0 and (viet_chars / total_alpha) > 0.3:
        return text, []

    changes: list[dict] = []
    words = text.split()
    result_words = list(words)
    i = 0

    while i < len(words):
        # Thử khớp cụm từ dài nhất trước (greedy matching)
        matched = False
        for length in range(min(_MAX_PHRASE_LEN, len(words) - i), 0, -1):
            phrase = " ".join(words[i:i + length]).lower()
            if phrase in _TONE_LOOKUP:
                corrected = _TONE_LOOKUP[phrase]
                original = " ".join(words[i:i + length])
                if original.lower() != corrected.lower():
                    changes.append({
                        "original":  original,
                        "corrected": corrected,
                        "word_pos":  i,
                    })
                    corrected_words = corrected.split()
                    result_words[i:i + length] = corrected_words
                matched = True
                i += length
                break

        if not matched:
            i += 1

    restored = " ".join(result_words)

    if changes:
        log.info(
            "tone_restore: %d sửa đổi — %s",
            len(changes),
            [(c["original"], "→", c["corrected"]) for c in changes],
        )

    return restored, changes


def needs_tone_restoration(text: str) -> bool:
    """
    Heuristic nhanh: kiểm tra xem text có cần phục hồi dấu không.
    Trả về True nếu phát hiện từ không dấu chiếm đa số.
    """
    if not text:
        return False
    words = text.split()
    if not words:
        return False
    no_tone_count = sum(1 for w in words if _is_no_tone_word(w))
    return (no_tone_count / len(words)) > 0.5


# ── Self-test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    tests = [
        "benh nhan bi viem phoi cap va sot cao",
        "chan doan: ung thu phoi giai doan 3",
        "bac si chi dinh xet nghiem mau va sieu am",
        "Bệnh nhân bị viêm phổi và sốt cao",          # đã có dấu → không đổi
        "Patient has pneumonia and high fever",         # không phải tiếng Việt → không đổi
    ]
    for t in tests:
        result, log_entries = restore_tones(t)
        print(f"IN : {t}")
        print(f"OUT: {result}")
        print(f"LOG: {log_entries}")
        print()
