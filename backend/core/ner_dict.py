import os
import re
import json
import logging
import unicodedata
from pathlib import Path
from typing import Optional
from core.custom_entities import inject_custom_entities

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s [ner_dict]: %(message)s")
BASE_DIR    = Path(__file__).parent
PROJECT_DIR = BASE_DIR.parent                        # d:\NCKH\demo_7
DICT_DIR    = BASE_DIR / "Tu Dien Y Hoc"           
PHULUC_PDF  = PROJECT_DIR / "data" / "PhuLuc1.pdf"
ICD10_EXCEL = PROJECT_DIR / "data" / "phu-lu-c-1-danh-mu-c-icd-10-thay-the-dmdc-phie-n-ba-n-6.xlsx"

# Cũng thử path tiếng Việt nếu tồn tại
_VN_DICT_DIR = BASE_DIR / "Từ Điển Y Học"
if _VN_DICT_DIR.exists():
    DICT_DIR = _VN_DICT_DIR

ICD10_JSON  = DICT_DIR / "01_icd10_dictionary.json"
YHCT_JSON   = DICT_DIR / "02_phuluc1_yhct.json"

_LEGACY_ICD10_JSON = PROJECT_DIR / "data" / "icd10_dictionary.json"
_LEGACY_YHCT_JSON  = PROJECT_DIR / "data" / "yhct_dictionary.json"

term_dict: dict = {}
COLOR_MAP: dict[str, str] = {
    "Benh Ly":                 "#86efac",
    "Bệnh Lý":                 "#86efac",
    "Trieu Chung":             "#93c5fd",
    "Triệu Chứng":             "#93c5fd",
    "Tien Trinh Benh Ly":      "#f9a8d4",
    "Tiến Trình Bệnh Lý":      "#f9a8d4",
    "Dieu Tri":                "#c4b5fd",
    "Điều Trị":                "#c4b5fd",
    "Xet Nghiem/Can Lam Sang":  "#fcd34d",
    "Xét Nghiệm/Cận Lâm Sàng": "#fcd34d",
    "Chan Doan Hinh Anh":      "#5eead4",
    "Chẩn Đoán Hình Ảnh":      "#5eead4",
    "Dong Y / YHCT":           "#fde047",
    "Đông Y / YHCT":           "#fde047",
    # fallback EN
    "DISEASE":   "#86efac",
    "SYMPTOM":   "#93c5fd",
    "PROCESS":   "#f9a8d4",
}
def get_color(cat: str) -> str:
    """Trả về mã màu HEX cho nhãn entity."""
    return COLOR_MAP.get(cat, "#e2e8f0")
def icd10_entity_type(ma: str) -> Optional[str]:
    """Phân loại entity từ mã ICD-10. None = bỏ qua."""
    ma = str(ma).strip().upper()
    if ma.startswith("R"):
        return "Triệu Chứng"
    if ma.startswith(("V", "W", "X", "Y")):
        return None
    if ma.startswith("Z"):
        return None
    return "Bệnh Lý"
_PROCESS_KEYWORDS: frozenset[str] = frozenset({
    "xơ hóa", "xơ chai", "xơ cứng",
    "thoái hóa", "thoái biến",
    "hoại tử", "hoại thư",
    "sung huyết", "xung huyết", "ứ huyết",
    "teo ", "phì đại", "phì to",
    "tăng sản", "quá sản",
    "vôi hóa", "canxi hóa",
    "nhiễm mỡ", "thoái mỡ",
    "di căn", "xâm lấn",
    "tắc nghẽn", "tắc mạch",
    "lắng đọng", "tích tụ",
    "tái phát", "tái tạo",
})
AMBIGUOUS_SOLO: set[str] = {
    "u", "tran", "dau", "hoai", "roi", "suy", "nhiem", "loet",
    "xuat", "chay", "mat", "giam", "tang", "thieu", "thua",
    "vo", "tac", "hep", "dan", "co", "liet",
    "thuoc", "mau", "te", "bao", "khang", "the", "nguyen",
    "benh", "hoi", "chung", "hop", "loan",
    "lipid", "gen", "protein",
    "nghi", "ngo", "phap", "cu", "viec", "gay",
    "gioi", "tuoi", "nhan", "trung", "binh",
    "ty", "le", "muc", "do", "dan",
    # Tiếng Việt có dấu
    "tràn", "đau", "hoại", "rối", "nhiễm", "loét",
    "xuất", "chảy", "mất", "giảm", "tăng", "thiếu", "thừa",
    "vỡ", "tắc", "hẹp", "dãn", "liệt",
    "thuốc", "máu", "tế", "bào", "kháng", "nguyên",
    "bệnh", "hội", "chứng", "hợp", "loạn",
    "nghĩ", "ngờ", "pháp", "việc", "gây",
    "giới", "tuổi", "nhân", "bình",
    "tỷ", "lệ", "mức", "độ", "dẫn",
    # Từ đơn YHCT hay bị bắt sai (cần cụm mới có nghĩa)
    "thống", "phong", "toại", "chẩn", "đản",
    "hàn", "nhiệt", "tý", "vựng", "khí",
    "huyễn", "suyễn", "thấp", "can", "tỳ",
    "đàm", "ứ", "hư", "thực", "dương", "âm",
    "khác", "thể", "các", "những", "loại", "dạng", "dấu", "ấn",
    # Từ "mạn" (mạn tính) không có nghĩa độc lập
    "mạn", "man",
    "cấp", "cap",
}
BLOCK_PHRASES: set[str] = {
    "khang the", "khang nguyen", "te bao", "huyet thanh", "nong do",
    "thu the", "enzyme", "cytokine", "dap ung mien dich",
    "co che", "phan ung",
    "kháng thể", "kháng nguyên", "tế bào", "huyết thanh", "nồng độ",
    "thụ thể", "đáp ứng miễn dịch", "biểu hiện gen", "cơ chế", "phản ứng",
    "vai trò", "vai trò quan trọng", "công cụ", "phương pháp",
    "xét nghiệm", "kết quả", "kết quả xét nghiệm",
    "đánh giá", "khuyến cáo", "triển khai", "biến chứng",
    "tác nhân", "tác nhân gây", "trung gian hóa học",
    "hoạt hóa", "hoạt hóa tế bào",
    "không xâm lấn", "không xâm nhập", "ít xâm lấn", "tối thiểu xâm lấn",
    "cơ bệnh tim", "nguy cơ bệnh tim", "nguy cơ tim mạch",
    "lâm sàng", "cận lâm sàng", "điều trị", "triệt căn",
    "mô tả", "đặc điểm", "chẩn đoán","mật độ", "mat do", 
    "tỷ trọng", "ty trong","nguyên phát", "nguyen phat",
    "kéo dài", "keo dai",
    "tổn thương phổi", "ton thuong phoi",
    "tổn thương não", "ton thuong nao",
    "tổn thương gan", "ton thuong gan",
    "tổn thương thận", "ton thuong than",
    "tổn thương tim", "ton thuong tim",
    "tổn thương cơ tim", "ton thuong co tim",
}
STOP_WORDS: set[str] = {
    "va", "hoac", "cua", "cac", "nhung", "da", "la", "thi", "ma", "bi",
    "duoc", "trong", "voi", "de", "khi", "co", "khong", "mot", "nguoi",
    "tu", "thang", "lan", "o", "tai", "cho", "vao", "ra", "dang", "se",
    "den", "nhu", "nay", "qua", "ve", "do", "boi", "theo", "tren", "duoi",
    "sau", "truoc", "giua", "ca", "nhieu", "it", "lai", "hon", "nhat",
    "cung", "moi", "vi", "nen", "neu", "moi", "nhom",
    # Tiếng Việt có dấu
    "và", "hoặc", "của", "các", "những", "đã", "là", "thì", "mà", "bị",
    "được", "trong", "với", "để", "khi", "có", "không", "một", "người",
    "từ", "tháng", "lần", "ở", "tại", "cho", "vào", "ra", "đang", "sẽ",
    "đến", "như", "này", "qua", "về", "do", "bởi", "theo", "trên", "dưới",
    "sau", "trước", "giữa", "cả", "nhiều", "ít", "lại", "hơn", "nhất",
    "cũng", "mới", "vì", "nên", "nếu", "mỗi", "nhóm",
}
COORD_CONJUNCTIONS: set[str] = {"và", "hoặc", "hay", "với", "cùng", "va", "hoac", "hay", "voi", "cung"}

def _add(term: str, cat: str, code: str, label_vn: str = "", source: str = "") -> None:
    """Chuẩn hóa và thêm thuật ngữ vào term_dict với kiểm tra đầu vào."""
    t = unicodedata.normalize("NFC", term.strip().lower())
    if len(t) < 3:
        return
    if re.fullmatch(r"[\d\s\.\,\%\-\+\/\(\)]+", t):
        return
    if t in STOP_WORDS:
        return
    if t in AMBIGUOUS_SOLO and len(t.split()) == 1:
        return
    if t in BLOCK_PHRASES:
        return
    term_dict[t] = {
        "cat":       cat,
        "code":      code,
        "label_vn":  label_vn or term.strip(),
        "is_dagger": "†" in code or code.endswith("*"),
        "source":    source,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PHẦN 4: PARSE PhuLuc1.pdf → YHCT JSON
# ══════════════════════════════════════════════════════════════════════════════

def _clean_cell(text: str) -> str:
    """Làm sạch cell PDF: chuẩn hóa Unicode, xóa newline, khoảng trắng thừa."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    text = re.sub(r"[\r\n\t\f\v]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()

def _is_valid_term(text: str) -> bool:
    """Kiểm tra chuỗi có phải thuật ngữ y khoa hợp lệ không."""
    t = text.strip()
    if not t or len(t) < 3:
        return False
    if re.fullmatch(r"[\d\s\.\,\%\-\+\/\(\)\[\]_]+", t):
        return False
    noise_patterns = [
        r"^(STT|So|TT|Ma|MA|Cot|Ghi chu|Trang)[\s\d]*$",
        r"^\d+$",
        r"^[A-Z]{1,3}\d{1,3}[\.\d]*$",
        r"^[-_]+$",
    ]
    for pat in noise_patterns:
        if re.match(pat, t, re.IGNORECASE):
            return False
    return True

def _extract_terms_from_combined(text: str) -> tuple[str, str]:
    """
    Tách cột kết hợp: "Đau cột sống thắt lưng (Yêu thống)"
    → ("Đau cột sống thắt lưng", "Yêu thống")
    """
    text = _clean_cell(text)
    m = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text, ""

def _build_yhct_json() -> list[dict]:
    """
    Parse PhuLuc1.pdf và trích xuất đúng 4 cột phục vụ cho hệ thống NER.
    Cấu trúc bảng thực tế (nếu pdfplumber nhận diện 24 cột do vỡ margin):
      - Col[4] : Tên kết hợp YHCT+YHHĐ  (Cột 2 theo logic)
      - Col[7] : Tên bệnh theo YHHĐ     (Cột 3 theo logic)
      - Col[13]: Bệnh danh YHCT         (Cột 5 theo logic)
      - Col[19]: Các thể lâm sàng       (Cột 7 theo logic)
    """
    try:
        import pdfplumber
    except ImportError:
        log.error("Thiếu pdfplumber. Chạy: pip install pdfplumber")
        return []

    if not PHULUC_PDF.exists():
        log.error(f"Không tìm thấy: {PHULUC_PDF}")
        return []

    # Gom config index lên đầu để dễ sửa đổi nếu file PDF thay đổi format
    CI_KET_HOP = 4
    CI_YHHD    = 7
    CI_YHCT    = 13
    CI_THE_LS  = 19

    # Tập hợp các chuỗi rác/header từ PDF
    _NOISE = {
        "ma dung", "chung", "ma icd", "ten benh", "benh danh",
        "cac the", "voi yhhd", "yhct, ket hop yhct", "yhct",
        "chan doan benh theo", "ten benh theo yhhd",
        "lam sang", "huong dan",
        "mã dùng", "mã icd", "tên bệnh", "bệnh danh",
        "các thể", "với yhhd", "yhct, kết hợp yhct",
        "chẩn đoán bệnh theo", "tên bệnh theo yhhđ",
        "lâm sàng", "hướng dẫn",
    }

    def _is_noise(v: str) -> bool:
        t = v.strip().lower()
        if not t or len(t) < 3: return True
        for n in _NOISE:
            if n in t: return True
        if re.fullmatch(r"[\d\s/\.\,\%\-\+\(\)\[\]_:;]+", t): return True
        if re.match(r"^\d\s+\d+\s*[A-Za-z/]", t): return True
        return False

    def _get_val(row: list, idx: int) -> str:
        """Lấy giá trị an toàn theo index và làm sạch rác."""
        if idx >= len(row): return ""
        v = _clean_cell(str(row[idx] or ""))
        v = re.sub(r"^\d+\s+\d+\s*[A-Z]?/", "", v).strip()
        return "" if _is_noise(v) else v

    # ── Bước 1: Thu thập raw fragments (Các hàng dữ liệu bị vỡ) ──
    fragments: list[dict] = []
    log.info(f"Đang parse PDF: {PHULUC_PDF}")

    with pdfplumber.open(PHULUC_PDF) as pdf:
        n_pages = len(pdf.pages)
        for pg_idx, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            if not tables: continue
            
            for row in tables[0][3:]:  # Bỏ 3 hàng header đầu trang
                if not row or len(row) <= CI_THE_LS: continue
                
                # Chỉ lấy đúng 4 cột cần thiết
                kh  = _get_val(row, CI_KET_HOP)
                yh  = _get_val(row, CI_YHHD)
                yhc = _get_val(row, CI_YHCT)
                tls = _get_val(row, CI_THE_LS)
                
                if any([kh, yh, yhc, tls]):
                    fragments.append({"kh": kh, "yh": yh, "yhc": yhc, "tls": tls, "pg": pg_idx})

    log.info(f"Raw fragments: {len(fragments)}")

    # ── Bước 2: Gom fragment → records hoàn chỉnh ──
    records: list[dict] = []
    seen: set[str] = set()

    def _flush(buf: dict) -> None:
        yh  = buf.get("yh", "").strip()
        yhc = buf.get("yhc", "").strip()
        kh  = buf.get("kh", "").strip()
        tls = buf.get("tls", "").strip()

        if not any([yh, yhc, kh]): return
        
        # Lọc các thuật ngữ rác không hợp lệ lọt qua
        for v in [yh, yhc, kh]:
            if v and not _is_valid_term(v): return

        # Cơ chế chống trùng lặp dựa trên combo YHHĐ và YHCT
        dedup = f"{yh.lower()}|{yhc.lower()}"
        if dedup in seen or dedup == "|": return
        seen.add(dedup)

        # Lưu record chính thức với bộ key tương thích với _load_yhct_into_dict
        records.append({
            "ten_ket_hop":   kh,
            "ten_yhhd":      yh,
            "ten_yhct":      yhc,
            "the_lam_sang":  tls,
            "icd_code":      "",
            "page":          buf.get("pg", 0),
        })

        # Nếu một bệnh có nhiều thể lâm sàng, tách các thể này thành thực thể độc lập để NER nhận diện
        if tls:
            for the in re.split(r"[,;\n]+", tls):
                the = the.strip()
                if len(the) >= 4 and _is_valid_term(the) and the.lower() != tls.lower():
                    sk = f"|{the.lower()}"
                    if sk not in seen:
                        seen.add(sk)
                        records.append({
                            "ten_ket_hop":  "",
                            "ten_yhhd":     yh,
                            "ten_yhct":     the,
                            "the_lam_sang": "",
                            "icd_code":     "",
                            "page":         buf.get("pg", 0),
                        })

    cur: dict = {}
    for f in fragments:
        # Nếu gặp Tên YHHĐ mới VÀ bộ đệm (cur) đã có Tên YHHĐ cũ -> ngắt dòng để lưu
        if f["yh"] and cur.get("yh"):
            _flush(cur)
            cur = {}
            
        # Nối dòng cho các cột text thường
        for k in ("kh", "yh", "yhc"):
            if f[k]:
                cur[k] = (cur.get(k, "") + " " + f[k]).strip()
                
        # Nối dòng cho các thể lâm sàng (cách nhau bởi dấu phẩy)
        if f["tls"]:
            old = cur.get("tls", "")
            cur["tls"] = (old + (", " if old else "") + f["tls"]).strip()
            
        cur.setdefault("pg", f["pg"])

    _flush(cur)

    log.info(f"PDF parse xong: {len(records)} records từ {n_pages} trang")
    return records

def _save_yhct_json(records: list[dict]) -> None:
    """Lưu danh sách YHCT records vào JSON."""
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    with open(YHCT_JSON, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    log.info(f"Đã lưu thành công: {YHCT_JSON} ({len(records)} records)")

def _build_icd10_json() -> list[dict]:
    """
    Parse Excel ICD-10 và trích xuất 5 cột:
      - Tên chương, Tên nhóm chính, Mã bệnh, Disease name (EN), Tên bệnh (VI)
    """
    try:
        import pandas as pd
    except ImportError:
        log.error("Thieu pandas. Chay: pip install pandas openpyxl")
        return []

    if not ICD10_EXCEL.exists():
        log.error(f"Khong tim thay Excel: {ICD10_EXCEL}")
        return []

    log.info(f"Dang parse Excel ICD-10: {ICD10_EXCEL}")
    try:
        df = pd.read_excel(ICD10_EXCEL, header=2)
        df.columns = [unicodedata.normalize("NFC", str(c).strip()) for c in df.columns]

        # Nhận diện cột tự động
        col_map: dict[str, str] = {}
        for col in df.columns:
            cl = col.lower()
            if "chương" in cl or "chuong" in cl:
                col_map.setdefault("chapter", col)
            elif "nhóm" in cl or "nhom" in cl:
                col_map.setdefault("group", col)
            elif "mã bệnh không" in cl or "ma benh khong" in cl:
                col_map["code"] = col
            elif "mã bệnh" in cl and "code" not in col_map:
                col_map["code"] = col
            elif "disease" in cl or "english" in cl:
                col_map.setdefault("name_en", col)
            elif "tên bệnh" in cl or "ten benh" in cl:
                col_map.setdefault("name_vi", col)
        log.info(f"Cot nhan dien duoc: {col_map}")
        records: list[dict] = []
        seen_codes: set[str] = set()
        def _nfc(v) -> str:
            s = str(v or "").strip()
            return unicodedata.normalize("NFC", s) if s not in ("nan", "NaN", "None") else ""
        for _, row in df.iterrows():
            ma   = _nfc(row.get(col_map.get("code", ""), ""))
            ten  = _nfc(row.get(col_map.get("name_vi", ""), ""))
            ten_en = _nfc(row.get(col_map.get("name_en", ""), ""))
            ch   = _nfc(row.get(col_map.get("chapter", ""), ""))
            nhom = _nfc(row.get(col_map.get("group", ""), ""))
            if not ma or not ten:
                continue
            if ma in seen_codes:
                continue
            seen_codes.add(ma)
            records.append({
                "ten_chuong":   ch,
                "ten_nhom":     nhom,
                "ma_benh":      ma,
                "disease_name": ten_en,
                "ten_benh":     ten,
            })

        log.info(f"Excel ICD-10 parse xong: {len(records)} records")
        return records

    except Exception as e:
        log.error(f"Loi parse Excel: {e}")
        return []


def _save_icd10_json(records: list[dict]) -> None:
    """Lưu danh sách ICD-10 records vào JSON."""
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    with open(ICD10_JSON, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    log.info(f"Da luu: {ICD10_JSON} ({len(records)} records)")

def _load_yhct_into_dict() -> int:
    """Nạp YHCT JSON vào term_dict. Trả về số thuật ngữ thêm được."""
    if not YHCT_JSON.exists():
        log.warning(f"Chua co {YHCT_JSON}. Chay build_dictionaries() truoc.")
        return 0
    n_before = len(term_dict)
    with open(YHCT_JSON, "r", encoding="utf-8") as f:
        records: list[dict] = json.load(f)
    for rec in records:
        icd_code = rec.get("icd_code", "YHCT") or "YHCT"
        if kh := rec.get("ten_ket_hop", "").strip():
            _add(kh, "Đông Y / YHCT", icd_code, kh, "phuluc1_col2")
            yh_part, yhc_part = _extract_terms_from_combined(kh)
            if yh_part:
                _add(yh_part, "Bệnh Lý", icd_code, kh, "phuluc1_col2_yhhd")
            if yhc_part:
                _add(yhc_part, "Đông Y / YHCT", icd_code, kh, "phuluc1_col2_yhct")

        if yh := rec.get("ten_yhhd", "").strip():
            _add(yh, "Bệnh Lý", icd_code, yh, "phuluc1_col3")

        if yhc := rec.get("ten_yhct", "").strip():
            _add(yhc, "Đông Y / YHCT", icd_code, yhc, "phuluc1_col5")

        if tls := rec.get("the_lam_sang", "").strip():
            for the in re.split(r"[,;\n]+", tls):
                the = the.strip()
                if len(the) >= 4:
                    _add(the, "Đông Y / YHCT", icd_code, the, "phuluc1_col7")

    added = len(term_dict) - n_before
    log.info(f"YHCT -> {added} thuat ngu moi vao term_dict")
    return added


def _load_icd10_into_dict() -> int:
    """Nạp ICD-10 JSON vào term_dict. Ưu tiên JSON mới, fallback cũ."""
    json_path = ICD10_JSON if ICD10_JSON.exists() else _LEGACY_ICD10_JSON
    if not json_path.exists():
        log.warning("Khong co ICD-10 JSON")
        return 0

    n_before = len(term_dict)
    with open(json_path, "r", encoding="utf-8") as f:
        records: list[dict] = json.load(f)

    for rec in records:
        ma  = str(rec.get("ma_benh") or rec.get("MÃ BỆNH") or "").strip()
        ten = str(rec.get("ten_benh") or rec.get("TÊN BỆNH") or "").strip()
        if not ma or not ten or ten == "nan":
            continue
        cat = icd10_entity_type(ma)
        if cat is None:
            continue
        # Phân loại lại thành "Tiến Trình Bệnh Lý" nếu tên bệnh chứa từ khóa tiến trình
        if cat == "Bệnh Lý":
            ten_lower = ten.lower()
            if any(kw in ten_lower for kw in _PROCESS_KEYWORDS):
                cat = "Tiến Trình Bệnh Lý"

        _add(ten, cat, ma, ten, "icd10")

        # Alias trước dấu phẩy đầu tiên (ví dụ: "Không có, teo và hẹp..." → "Không có")
        # Chỉ thêm nếu alias không phải stop word / quá mơ hồ
        short = ten.split(",")[0].strip()
        if short != ten and len(short) >= 5:
            short_lower = short.lower()
            short_words = short_lower.split()
            # Bỏ qua nếu chỉ 1–2 từ và từ đầu nằm trong STOP_WORDS
            if not (len(short_words) <= 2 and short_words[0] in STOP_WORDS):
                _add(short, cat, ma, ten, "icd10_short")

        # Alias bỏ ngoặc đơn: "Gan (biến đổi) nhiễm mỡ" → "Gan nhiễm mỡ"
        # Giúp khớp với các cách viết không có mô tả trong ngoặc
        noparen = re.sub(r'\s*\([^)]+\)', '', ten).strip()
        noparen = re.sub(r'\s{2,}', ' ', noparen)
        if noparen and noparen != ten:
            _add(noparen, cat, ma, ten, "icd10_noparen")

        # Alias "ung thư": ICD-10 dùng "U ác của X" nhưng văn bản VN dùng "ung thư X"
        # Tự động tạo cả hai dạng alias để khớp cách viết thực tế
        ten_lower = ten.lower()
        _U_AC_PREFIXES = ("u ác của ", "u ác tính của ", "u ác ")
        for prefix in _U_AC_PREFIXES:
            if ten_lower.startswith(prefix):
                organ = ten[len(prefix):].strip()          # phần cơ quan/bộ phận
                organ_noparen = re.sub(r'\s*\([^)]+\)', '', organ).strip()
                organ_short   = organ_noparen.split(",")[0].strip()
                for variant in {organ_noparen, organ_short}:
                    if variant and len(variant) >= 3:
                        _add(f"ung thư {variant}",      cat, ma, ten, "icd10_ungthư")
                        _add(f"ung thư của {variant}",  cat, ma, ten, "icd10_ungthư")
                        _add(f"ung thư ác tính {variant}", cat, ma, ten, "icd10_ungthư")
                break
    added = len(term_dict) - n_before
    log.info(f"ICD-10 -> {added} thuat ngu moi vao term_dict")
    return added
def build_dictionaries(force_rebuild: bool = False) -> None:
    """Build (hoặc rebuild) 2 file JSON từ nguồn thô."""
    DICT_DIR.mkdir(parents=True, exist_ok=True)
    if force_rebuild or not ICD10_JSON.exists():
        log.info("Building ICD-10 JSON tu Excel...")
        records = _build_icd10_json()
        if records:
            _save_icd10_json(records)
    else:
        log.info(f"ICD-10 JSON da ton tai ({ICD10_JSON.stat().st_size // 1024} KB). Bo qua.")

    if force_rebuild or not YHCT_JSON.exists():
        log.info("Building YHCT JSON tu PDF...")
        records = _build_yhct_json()
        if records:
            _save_yhct_json(records)
    else:
        log.info(f"YHCT JSON da ton tai ({YHCT_JSON.stat().st_size // 1024} KB). Bo qua.")

_loaded: bool = False

def load_ner_dictionary(force_rebuild: bool = False) -> None:
    global term_dict, _loaded
    if _loaded and not force_rebuild:
        return  # Chỉ khởi tạo 1 lần duy nhất
    _loaded = True
    term_dict.clear()
    build_dictionaries(force_rebuild=force_rebuild)
    _load_icd10_into_dict()
    _load_yhct_into_dict()
    inject_custom_entities(_add)
    log.info(f"=== Tong tu dien: {len(term_dict)} thuat ngu ===")
if __name__ == "__main__":
    import sys as _sys
    import io as _io
    import argparse
    if _sys.stdout.encoding and _sys.stdout.encoding.lower() != "utf-8":
        _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Build NER Dictionary")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild JSON tu nguon tho")
    args = parser.parse_args()
    if args.rebuild:
        log.info("=== REBUILD MODE ===")
    load_ner_dictionary(force_rebuild=args.rebuild)
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"Tong thuat ngu trong term_dict: {len(term_dict)}")
    if term_dict:
        print("Mau 5 thuat ngu YHCT:")
        yhct_sample = [(k, v) for k, v in term_dict.items() if v.get("source", "").startswith("phuluc")][:5]
        for k, v in yhct_sample:
            print(f"  [{v['cat']}] {k!r} -> {v['code']}")
    print(sep)
