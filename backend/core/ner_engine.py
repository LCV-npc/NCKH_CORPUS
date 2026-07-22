import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, TypedDict


from core.ner_dict import (
    term_dict,
    AMBIGUOUS_SOLO,
    BLOCK_PHRASES,
    STOP_WORDS,
    COORD_CONJUNCTIONS,
    get_color,
)

from core.text_normalize import normalize_vietnamese_tones

# Import các module mới (lazy-safe: lỗi import không làm crash)
try:
    from core.tone_restore import restore_tones as _restore_tones
    _TONE_RESTORE_AVAILABLE = True
except ImportError:
    _TONE_RESTORE_AVAILABLE = False
    def _restore_tones(text):  # type: ignore
        return text, []

try:
    from core.nlp_extractor import analyze_with_noun_phrases as _np_analyze
    _NP_EXTRACTOR_AVAILABLE = True
except ImportError:
    _NP_EXTRACTOR_AVAILABLE = False
    def _np_analyze(text, td, exact_matches):  # type: ignore
        return []

@dataclass
class Token:
    word:  str    # Nội dung token (lowercase khi so sánh, original khi lấy text)
    start: int    # Offset bắt đầu trong văn bản gốc
    end:   int    # Offset kết thúc trong văn bản gốc

@dataclass
class EntityMatch:
    text:              str
    start:             int
    end:               int
    icd_code:          str
    icd_label_vn:      str
    entity_type:       str
    is_dagger:         bool
    matched_by:        str   # "exact" | "fuzzy" | "noun_phrase"
    paired_cause_code: str   = ""
    paired_cause_text: str   = ""

class NEREngine:
    _EXTRA_BOUNDARY_WORDS: frozenset[str] = frozenset({
        "giữ", "trị", "điều", "chữa", "bị", "mắc", "khỏi", "làm", "cho", "thấy",
        "thể", "có", "sự", "như", "là", "bằng", "để", "do", "bởi", "với", "tại",
        "ở", "từ", "đến", "các", "những", "một", "cái", "này", "kia", "đó", "đây",
        "lại", "còn", "đã", "đang", "sẽ", "phải", "nhận", "được", "mang",
        "gây", "trở", "thành", "thuộc", "về", "rất", "quá", "lắm", "hơn", "nhất",
        "gồm", "kèm", "theo", "cùng", "hay", "hoặc", "và", "nhưng", "tuy", "nhiên",
        "việc", "nhằm", "giúp", "góp", "phần"
    })
    _EXTRA_AMBIGUOUS_SOLO: frozenset[str] = frozenset({
        "u", "mạch", "di", "căn", "nang", "nhọt", "mô", "nội", "ngoại",
        "viêm", "đau", "hội", "chứng", "bệnh", "tật", "khối", "tuyến",
        "giảm", "tăng", "cao", "thấp", "nhiều", "ít", "rối", "loạn"
    })
    _ABBREVIATIONS: dict[str, str] = {
        "yhhđ":   "y học hiện đại",
        "yhct":   "y học cổ truyền",
        "hdl-c":  "hdl cholesterol",
        "ldl-c":  "ldl cholesterol",
        "hba1c":  "hemoglobin a1c",
        "copd":   "bệnh phổi tắc nghẽn mạn tính",
        "covid":  "covid",
        "masld":  "masld",
        "nafld":  "nafld",
    }
    _ORG_PREFIXES: tuple[str, ...] = (
        "bệnh viện", "viện", "phòng khám", "trung tâm", "khoa",
    )

    # Ngữ cảnh nguy cơ / mô tả — bỏ qua entity nếu xuất hiện sau
    _RISK_PREFIXES: tuple[str, ...] = (
        "nguy cơ", "dấu hiệu", "triệu chứng của", "tiền sử",
    )

    # Ngữ cảnh phương pháp — bỏ qua entity
    _METHOD_PREFIXES: tuple[str, ...] = (
        "phương pháp", "kỹ thuật", "công cụ", "đánh giá", "tiến hành", "sử dụng",
    )

    def __init__(
        self,
        max_window_size: int = 0,
    ) -> None:
        if max_window_size > 0:
            self.max_window_size = max_window_size
        else:
            self.max_window_size = self._compute_max_window()

    def _preprocess(self, text: str, enable_tone_restore: bool = True) -> tuple[str, list[dict]]:
        """
        Tiền xử lý văn bản:
        1. (Mới) Khôi phục dấu tiếng Việt bị thiếu/sai (nếu bật)
        2. Sửa lỗi đặt dấu Unicode (như cũ)
        3. Chuẩn hóa NFC

        Returns:
            (cleaned_text, tone_restore_log)
        """
        detailed_log: list[dict] = []

        # Bước 1 (MỚI): Khôi phục dấu tiếng Việt
        if enable_tone_restore and _TONE_RESTORE_AVAILABLE:
            old_text = text
            text, tone_log = _restore_tones(text)
            if tone_log:
                changes = [f"{t['original']} → {t['restored']} (độ tin cậy: {t['confidence']})" for t in tone_log]
                detailed_log.append({
                    "step": "Bước 1: Phục hồi dấu",
                    "description": "Phục hồi dấu tiếng Việt bị thiếu/sai cho các thuật ngữ y khoa",
                    "changes": changes
                })

        # Bước 1.5: Chuẩn hóa bộ gõ dấu thanh tiếng Việt (hòa -> hoà)
        old_text_15 = text
        text = normalize_vietnamese_tones(text)
        if text != old_text_15:
            # We don't have detailed word-by-word diff, just mark it was applied
            detailed_log.append({
                "step": "Bước 2: Chuẩn hóa vị trí dấu thanh",
                "description": "Thống nhất vị trí dấu thanh theo chuẩn tiếng Việt mới (ví dụ: hòa → hoà)",
                "changes": []
            })

        # Bước 2: Sửa lỗi đặt dấu Unicode
        text = unicodedata.normalize("NFC", text)
        detailed_log.append({
            "step": "Bước 3: Chuẩn hóa Unicode NFC",
            "description": "Đưa các ký tự tổ hợp về dạng ký tự dựng sẵn chuẩn Unicode",
            "changes": []
        })

        replacements = {
            # Nhóm "oa" — ĐÃ XÓA: "oá"→"óa" phá hỏng "thoáng", "oà"→"òa" phá hỏng "hoàn"
            # Chỉ giữ nhóm dấu nặng trên o (ít gây va chạm hơn)
            "ọan": "oạn", "ọang": "oạng", "ọanh": "oạnh",
            "ọac": "oạc", "ọat": "oạt", "ọam": "oạm", "ọap": "oạp",
            # Nhóm "uy" — ĐÃ XÓA: "uỵ"→"ụy" phá hỏng "quỵ" (đột quỵ → đột qụy)
            #   "uý"→"úy" có thể phá hỏng các từ khác có chuỗi uý
            # Dấu đặt sai vị trí: âm đôi "oe"
            "oè": "òe", "oé": "óe", "oẹ": "ọe",
            "yỳ": "yỳ", "yý": "yý", "yỷ": "yỷ", "yỹ": "yỹ", "yỵ": "yỵ",
        }
        legacy_changes = []
        for old, new in replacements.items():
            if old in text:
                text = text.replace(old, new)
                legacy_changes.append(f"{old} → {new}")
        
        if legacy_changes:
            detailed_log.append({
                "step": "Bước 4: Sửa dấu lỗi thủ công",
                "description": "Sửa các lỗi dấu thanh trên nguyên âm đôi/ba do bộ gõ cũ",
                "changes": legacy_changes
            })
            
        return text, detailed_log



    def _tokenize(self, text: str) -> list[Token]:
        tokens: list[Token] = []
        pattern = re.compile(r'\w+(?:-\w+)*', re.UNICODE)
        for m in pattern.finditer(text):
            tokens.append(Token(
                word=m.group(),
                start=m.start(),
                end=m.end(),
            ))
        return tokens

    def _compute_max_window(self) -> int:
        if not term_dict:
            return 7
        max_len = max(len(k.split()) for k in term_dict.keys())
        return min(max_len, 12)


    def _is_in_org_context(self, text: str, start: int) -> bool:
        prefix = text[max(0, start - 20):start].lower()
        return any(kw in prefix for kw in self._ORG_PREFIXES)

    def _is_in_risk_context(self, text: str, start: int) -> bool:
        prefix = text[max(0, start - 15):start].lower()
        return any(kw in prefix for kw in self._RISK_PREFIXES)

    def _is_in_method_context(self, text: str, start: int) -> bool:
        prefix = text[max(0, start - 30):start].lower()
        return any(kw in prefix for kw in self._METHOD_PREFIXES)

    def _should_skip_exact_match(
        self,
        chunk_words: list[str],
        phrase: str,
        phrase_lower: str,
        n: int,
    ) -> bool:
        """
        Guard nhẹ dành riêng cho exact match — chỉ chặn các trường hợp rõ ràng sẽ sai.
        Không chặn biên từ (STOP_WORDS / AMBIGUOUS_SOLO) vì thực thể đã được
        đăng ký chính xác trong từ điển (ví dụ: "thiếu máu não thoáng qua").
        """
        # Vẫn chặn dấu câu trong cụm ("viêm,gan" không phải 1 thực thể)
        if n > 1 and re.search(r'[.,;:]', phrase):
            return True

        # Vẫn chặn chuỗi thuần số / ký tự đặc biệt
        if re.fullmatch(r'[\d\s\.\,\%\-\+\/\(\)]+', phrase_lower):
            return True

        # Vẫn chặn block phrase (những cụm từ rõ ràng không phải thực thể y khoa)
        if phrase_lower in BLOCK_PHRASES:
            return True

        # Vẫn chặn chuỗi không có ký tự hợp lệ
        if not re.search(r'[a-zA-Z0-9\u00C0-\u1EF9]', phrase_lower):
            return True

        # Vẫn chặn từ đơn mơ hồ (chỉ khi n==1)
        if n == 1 and (phrase_lower in AMBIGUOUS_SOLO or phrase_lower in self._EXTRA_AMBIGUOUS_SOLO):
            return True

        # Vẫn chặn cụm có liên từ phối hợp ở giữa (chỉ khi n>2)
        if n > 2 and any(w in COORD_CONJUNCTIONS for w in chunk_words[1:-1]):
            return True

        return False

    class _Candidate(TypedDict):
        entity_text:     str
        start_token_idx: int
        end_token_idx:   int
        char_start:      int
        char_end:        int
        matched_by:      str
        icd_code:        str
        icd_label_vn:    str
        entity_type:     str
        is_dagger:       bool

    def _sliding_window_exact(self, tokens: list[Token], text: str) -> list["NEREngine._Candidate"]:
        candidates: list[NEREngine._Candidate] = []
        n_tokens = len(tokens)
        max_n = min(self.max_window_size, n_tokens)

        for n in range(max_n, 0, -1):
            for i in range(n_tokens - n + 1):
                chunk        = tokens[i:i + n]
                start        = chunk[0].start
                end          = chunk[-1].end
                chunk_words  = [tokens[i + k].word.lower() for k in range(n)]
                phrase       = text[start:end].strip()
                phrase_lower = phrase.lower()
                key          = unicodedata.normalize("NFC", phrase_lower)

                # Kiểm tra từ điển TRƯỚC — nếu không có thì bỏ qua ngay (nhanh nhất)
                if key not in term_dict:
                    continue

                # Exact match → chỉ dùng guard nhẹ (giữ lại các cụm đã đăng ký dù bắt đầu/cuối
                # bằng từ mơ hồ như "thiếu máu não thoáng qua", "xuất huyết não", ...)
                if self._should_skip_exact_match(chunk_words, phrase, phrase_lower, n):
                    continue

                info = term_dict[key]
                candidates.append(NEREngine._Candidate(
                    entity_text     = phrase, start_token_idx = i, end_token_idx   = i + n - 1,
                    char_start      = start, char_end        = end,
                    matched_by      = "exact", icd_code        = info["code"], icd_label_vn    = info["label_vn"],
                    entity_type     = info["cat"], is_dagger       = info["is_dagger"],
                ))
        return candidates

    def _resolve_overlaps(self, candidates: list["NEREngine._Candidate"]) -> list["NEREngine._Candidate"]:
        def _sort_key(c: "NEREngine._Candidate"):
            token_len = c["end_token_idx"] - c["start_token_idx"] + 1
            return -token_len

        sorted_cands = sorted(candidates, key=_sort_key)
        chosen: list[NEREngine._Candidate] = []
        chosen_intervals: list[tuple[int, int]] = []

        for cand in sorted_cands:
            cs = cand["start_token_idx"]
            ce = cand["end_token_idx"]
            if not any(cs <= ae and ce >= as_ for as_, ae in chosen_intervals):
                chosen.append(cand)
                chosen_intervals.append((cs, ce))

        chosen.sort(key=lambda c: c["char_start"])
        return chosen

    def _pair_daggers(self, entities: list[EntityMatch]) -> None:
        non_daggers = [e for e in entities if not e.is_dagger]
        for ent in entities:
            if not ent.is_dagger:
                continue
            best_cause, best_dist = None, 9999
            for nd in non_daggers:
                dist = abs(nd.start - ent.start)
                if dist < 200 and dist < best_dist:
                    best_dist  = dist
                    best_cause = nd
            if best_cause:
                ent.paired_cause_code = best_cause.icd_code
                ent.paired_cause_text = best_cause.icd_label_vn

    def analyze(
        self,
        text: str,
        enable_tone_restore: bool = True,
        enable_noun_phrase: bool = True,
    ) -> dict:
        """
        Phân tích NER với pipeline nâng cấp:
        1. Tiền xử lý dấu (tone restore)
        2. Exact match (sliding window)
        3. Noun phrase extraction (bổ sung)

        Returns:
            dict với keys: entities, tone_log, np_matches, preprocessed_text
        """
        if not text or not text.strip():
            return {"entities": [], "tone_log": [], "np_matches": [], "preprocessed_text": ""}

        cleaned, tone_log = self._preprocess(text, enable_tone_restore=enable_tone_restore)
        tokens  = self._tokenize(cleaned)

        if not tokens:
            return {"entities": [], "tone_log": tone_log, "np_matches": [], "preprocessed_text": cleaned}

        # ── Bước 1: Exact match ──────────────────────────────────────────────
        exact_candidates = self._sliding_window_exact(tokens, cleaned)
        final_candidates = self._resolve_overlaps(exact_candidates)

        all_entities: list[EntityMatch] = [
            EntityMatch(
                text          = c["entity_text"],
                start         = c["char_start"],
                end           = c["char_end"],
                icd_code      = c["icd_code"],
                icd_label_vn  = c["icd_label_vn"],
                entity_type   = c["entity_type"],
                is_dagger     = c["is_dagger"],
                matched_by    = c["matched_by"],
            )
            for c in final_candidates
        ]

        self._pair_daggers(all_entities)

        entity_dicts = [
            {
                "text":              e.text,
                "start":             e.start,
                "end":               e.end,
                "icd_code":          e.icd_code,
                "icd_label_vn":      e.icd_label_vn,
                "entity_type":       e.entity_type,
                "is_dagger":         e.is_dagger,
                "matched_by":        e.matched_by,
                "paired_cause_code": e.paired_cause_code,
                "paired_cause_text": e.paired_cause_text,
            }
            for e in all_entities
        ]

        # ── Bước 2: Noun Phrase matching (bổ sung) ───────────────────────────
        np_matches: list[dict] = []
        if enable_noun_phrase and _NP_EXTRACTOR_AVAILABLE:
            np_matches = _np_analyze(cleaned, term_dict, entity_dicts)

        return {
            "entities":          entity_dicts,
            "tone_log":          tone_log,
            "np_matches":        np_matches,
            "preprocessed_text": cleaned,
        }

# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON ENGINE INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_engine: Optional[NEREngine] = None


def _get_engine() -> NEREngine:
    """Trả về singleton NEREngine, khởi tạo lần đầu nếu chưa có."""
    global _engine
    if _engine is None:
        _engine = NEREngine()
    return _engine


# ══════════════════════════════════════════════════════════════════════════════
# HTML BUILDER
# ══════════════════════════════════════════════════════════════════════════════

# Ánh xạ nhãn tiếng Anh → tiếng Việt chuẩn
_LABEL_MAP: dict[str, str] = {
    "DISEASE":              "Bệnh Lý",
    "Tây Y - Bệnh lý":     "Bệnh Lý",
    "SYMPTOM":              "Triệu Chứng",
    "Tây Y - Triệu chứng": "Triệu Chứng",
    "PROCESS":              "Tiến Trình Bệnh Lý",
    "TREATMENT":            "Điều Trị",
    "TEST / METHOD":        "Xét Nghiệm/Cận Lâm Sàng",
    "IMAGING_METHOD":       "Chẩn Đoán Hình Ảnh",
    "BIOMARKER":            "Chỉ Số Sinh Học",
}


def _build_mark(
    original:          str,
    cat:               str,
    code:              str,
    label_vn:          str = "",
    is_dagger:         bool = False,
    paired_cause_code: str = "",
    paired_cause_text: str = "",
    matched_by:        str = "exact",
) -> str:
    """
    Tạo chuỗi HTML `<mark>` có tooltip cho một entity.

    Args:
        original         : Chuỗi gốc trong văn bản.
        cat              : Nhãn hiển thị (tiếng Việt).
        code             : Mã ICD-10.
        label_vn         : Tên chuẩn tiếng Việt.
        is_dagger        : Entity có phải dagger (†) không.
        paired_cause_code: Mã bệnh nguyên nhân (chỉ khi is_dagger=True).
        paired_cause_text: Tên bệnh nguyên nhân.
        matched_by       : Phương thức khớp ("exact" | "noun_phrase").

    Returns:
        Chuỗi HTML `<mark ...>...</mark>`.
    """
    color = get_color(cat)

        # Loại bỏ nội dung trong ngoặc đơn (...) khỏi label hiển thị
    label_vn_clean = re.sub(r'\s*\([^)]*\)', '', label_vn).strip() if label_vn else label_vn
    paired_cause_text_clean = re.sub(r'\s*\([^)]*\)', '', paired_cause_text).strip() if paired_cause_text else paired_cause_text

    if is_dagger and paired_cause_code:
        tooltip      = f"{cat} | Mã: {code} | Biểu hiện của: {paired_cause_code} - {paired_cause_text_clean}"
        border_style = "border:1.5px dashed rgba(239,68,68,0.55);"
    else:
        tooltip = f"{cat} | Mã: {code}"
        if label_vn_clean and label_vn_clean.lower() != original.lower():
            tooltip += f" | {label_vn_clean}"
        border_style = "border:1px solid rgba(0,0,0,0.08);"

    # Thêm badge noun_phrase nếu khớp bằng NLP
    np_badge = ""
    if matched_by == "noun_phrase":
        np_badge = '<sup style="font-size:0.6em;color:#6366f1;margin-left:2px;font-weight:700;">NP</sup>'
        border_style = "border:1.5px dashed rgba(99,102,241,0.6);"
        tooltip += " | [Noun Phrase]"

    dagger_badge = (
        '<sup style="font-size:0.65em;color:#ef4444;margin-left:1px;">*</sup>'
        if is_dagger else ""
    )
    return (
        f'<mark class="concept-highlight" '
        f'style="background:{color};padding:2px 5px;border-radius:4px;'
        f'font-weight:600;cursor:help;{border_style}" '
        f'title="{tooltip}">{original}{dagger_badge}{np_badge}</mark>'
    )


def _get_parenthesis_ranges(text: str) -> list[tuple[int, int]]:
    """
    Trả về danh sách các đoạn (start, end) nằm BÊN TRONG ngoặc đơn (...)
    trong văn bản gốc (bao gồm cả dấu ngoặc).
    """
    ranges: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '(':
            if depth == 0:
                start = i
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0 and start != -1:
                ranges.append((start, i + 1))
                start = -1
    return ranges


# ══════════════════════════════════════════════════════════════════════════════
# BACKWARD-COMPATIBLE API (dùng bởi main.py / api.py / script.js)
# ══════════════════════════════════════════════════════════════════════════════

def ner_with_fuzzy(
    text: str,
    threshold: int = 90,
    enable_tone_restore: bool = True,
    enable_noun_phrase: bool = True,
) -> dict:
    """
    Phân tích NER — backward compatible wrapper.

    Returns dict: {entities, tone_log, np_matches, preprocessed_text}
    """
    if not text:
        return {"entities": [], "tone_log": [], "np_matches": [], "preprocessed_text": ""}

    engine = _get_engine()
    # threshold bị bỏ qua do đã loại bỏ kỹ thuật fuzzy match
    return engine.analyze(
        text,
        enable_tone_restore=enable_tone_restore,
        enable_noun_phrase=enable_noun_phrase,
    )


def run_ner(
    text: str,
    threshold: int = 100,
    enable_tone_restore: bool = True,
    enable_noun_phrase: bool = True,
):
    """
    Chạy NER pipeline đầy đủ.

    Returns:
        (highlighted_html, concepts, raw_entities, preprocessing_log)
    """
    if not text:
        return "", [], [], {}

    result = ner_with_fuzzy(
        text,
        threshold,
        enable_tone_restore=enable_tone_restore,
        enable_noun_phrase=enable_noun_phrase,
    )
    raw          = result["entities"]
    tone_log     = result["tone_log"]
    np_matches   = result["np_matches"]
    preprocessed = result["preprocessed_text"]

    # ── Lọc bỏ entity nằm hoàn toàn bên trong ngoặc đơn (...) ───────────────
    paren_ranges = _get_parenthesis_ranges(text)
    if paren_ranges:
        raw = [
            e for e in raw
            if not any(ps <= e["start"] and e["end"] <= pe for ps, pe in paren_ranges)
        ]

    # ── Build HTML — duyệt từ cuối để offset không lệch ─────────────────────
    highlighted = text
    for ent in sorted(raw, key=lambda x: -x["start"]):
        cat_stripped = ent["entity_type"].strip()
        display_cat  = _LABEL_MAP.get(cat_stripped, cat_stripped)

        mark = _build_mark(
            original          = ent["text"],
            cat               = display_cat,
            code              = ent["icd_code"],
            label_vn          = ent["icd_label_vn"],
            is_dagger         = ent["is_dagger"],
            paired_cause_code = ent["paired_cause_code"],
            paired_cause_text = ent.get("paired_cause_text", ""),
            matched_by        = ent.get("matched_by", "exact"),
        )
        highlighted = highlighted[:ent["start"]] + mark + highlighted[ent["end"]:]

    # ── Concepts list (unique, theo thứ tự xuất hiện) ───────────────────────
    concepts: list[dict] = []
    seen: set[str] = set()

    # Exact matches trước
    for ent in sorted(raw, key=lambda x: x["start"]):
        name = ent["text"].strip()
        if name.lower() not in seen:
            seen.add(name.lower())
            cat_stripped = ent["entity_type"].strip()
            concepts.append({
                "name":              name,
                "type":              _LABEL_MAP.get(cat_stripped, cat_stripped),
                "code":              ent["icd_code"],
                "icd_label_vn":      ent["icd_label_vn"],
                "paired_cause_code": ent.get("paired_cause_code", ""),
                "paired_cause_text": ent.get("paired_cause_text", ""),
                "matched_by":        ent.get("matched_by", "exact"),
            })

    # Noun phrase matches (bổ sung, không trùng)
    for ent in np_matches:
        name = ent["text"].strip()
        if name.lower() not in seen:
            seen.add(name.lower())
            cat_stripped = ent.get("entity_type", "").strip()
            concepts.append({
                "name":              name,
                "type":              _LABEL_MAP.get(cat_stripped, cat_stripped),
                "code":              ent.get("icd_code", ""),
                "icd_label_vn":      ent.get("icd_label_vn", name),
                "paired_cause_code": "",
                "paired_cause_text": "",
                "matched_by":        "noun_phrase",
            })

    preprocessing_log = {
        "tone_restore":     tone_log,
        "preprocessed_text": preprocessed,
        "np_count":         len(np_matches),
        "exact_count":      len(raw),
    }

    all_raw = raw + np_matches
    return highlighted, concepts, all_raw, preprocessing_log
