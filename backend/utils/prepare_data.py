import pandas as pd
import pdfplumber
import json
import os
import re
import sys
import unicodedata

# Fix cho lỗi in tiếng Việt trên console Windows
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')


def clean_text(text: str) -> str:
    """Làm sạch chuỗi văn bản: xóa ký tự xuống dòng và khoảng trắng thừa."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return unicodedata.normalize('NFC', text.strip())


def icd10_entity_type(ma: str) -> str | None:
    """
    Xác định loại entity từ mã ICD-10.
    Trả về None cho các mã cần bỏ qua (Z, V, W, X, Y).
    """
    ma = str(ma).strip().upper()
    if ma.startswith('R'):
        return 'SYMPTOM'
    if ma.startswith(('V', 'W', 'X', 'Y')):
        return None   # Nguyên nhân ngoại sinh — bỏ qua
    if ma.startswith('Z'):
        return None   # Yếu tố ảnh hưởng sức khỏe — bỏ qua
    return 'DISEASE'


def process_icd10(file_path: str, output_path: str) -> None:
    """
    Đọc file Excel ICD-10 (header ở dòng 3, tức header=2),
    chuẩn hóa Unicode NFC, lọc mã không cần thiết,
    rồi xuất ra JSON dùng cho ner_dict.py.
    """
    print(f"Đang xử lý ICD-10 ({os.path.basename(file_path)})...")
    try:
        all_data: list[dict] = []

        if file_path.endswith(('.xlsx', '.xls')):
            sheets = pd.read_excel(file_path, sheet_name=None, header=2)

            for sheet_name, df in sheets.items():
                print(f"  - Sheet: {sheet_name}")

                col_mapping = {
                    "Mã":                   "MÃ BỆNH",
                    "Mã bệnh chính":        "MÃ BỆNH",
                    "PDx":                  "MÃ BỆNH",
                    "Tên bệnh tiếng Anh":   "DISEASE NAME",
                    "Description":          "DISEASE NAME",
                    "Tên bệnh chính":       "TÊN BỆNH",
                }
                df.rename(columns=col_mapping, inplace=True)

                for col in df.columns:
                    if str(col).strip().lower() == "tên bệnh" and "TÊN BỆNH" not in df.columns:
                        df.rename(columns={col: "TÊN BỆNH"}, inplace=True)

                if "MÃ BỆNH" not in df.columns or "TÊN BỆNH" not in df.columns:
                    print(f"    -> Bỏ qua '{sheet_name}': thiếu cột MÃ BỆNH / TÊN BỆNH")
                    continue

                cols_keep = [c for c in
                             ["MÃ BỆNH", "TÊN BỆNH", "DISEASE NAME", "TÊN CHƯƠNG", "TÊN NHÓM CHÍNH"]
                             if c in df.columns]
                df = df[cols_keep].dropna(subset=["MÃ BỆNH", "TÊN BỆNH"]).fillna("")

                df["TÊN BỆNH"] = df["TÊN BỆNH"].apply(
                    lambda x: unicodedata.normalize('NFC', str(x).strip())
                )
                if "DISEASE NAME" in df.columns:
                    df["DISEASE NAME"] = df["DISEASE NAME"].apply(
                        lambda x: str(x).strip()
                    )

                records = []
                for _, row in df.iterrows():
                    ma = str(row["MÃ BỆNH"]).strip()
                    etype = icd10_entity_type(ma)
                    if etype is None:
                        continue
                    r = row.to_dict()
                    r["ENTITY_TYPE"] = etype
                    records.append(r)

                all_data.extend(records)

        else:
            df = pd.read_csv(file_path)
            cols_keep = [c for c in
                         ["MÃ BỆNH", "TÊN BỆNH", "DISEASE NAME", "TÊN CHƯƠNG", "TÊN NHÓM CHÍNH"]
                         if c in df.columns]
            df = df[cols_keep].dropna(subset=["MÃ BỆNH", "TÊN BỆNH"]).fillna("")
            df["TÊN BỆNH"] = df["TÊN BỆNH"].apply(
                lambda x: unicodedata.normalize('NFC', str(x).strip())
            )
            for _, row in df.iterrows():
                ma = str(row["MÃ BỆNH"]).strip()
                etype = icd10_entity_type(ma)
                if etype is None:
                    continue
                r = row.to_dict()
                r["ENTITY_TYPE"] = etype
                all_data.append(r)

        unique: dict = {}
        for d in all_data:
            key = (str(d["MÃ BỆNH"]).strip(), str(d["TÊN BỆNH"]).strip())
            if key not in unique:
                unique[key] = d

        result = list(unique.values())
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(f"✅ ICD-10: {len(result)} bản ghi → {output_path}")

    except FileNotFoundError:
        print(f"❌ Không tìm thấy file: {file_path}")
    except Exception as e:
        print(f"❌ Lỗi xử lý ICD-10: {e}")


def process_yhct(pdf_path: str, output_path: str) -> None:
    """
    Đọc file PDF YHCT, chuẩn hóa Unicode NFC, xử lý lỗi xuống dòng (merge rows),
    và xuất ra JSON dùng cho ner_dict.py.
    """
    print(f"Đang xử lý YHCT ({os.path.basename(pdf_path)})...")
    try:
        import pdfplumber
    except ImportError:
        print("❌ Lỗi: Cần cài đặt thư viện pdfplumber (pip install pdfplumber)")
        return

    try:
        all_data = []
        current_record = None
        
        # Danh sách các từ nhiễu (watermark) cần xóa
        noise_words = ['Manh', 'Nguyen', 'Tuan', 'yder', 'tuannra', 'tuannmyder', 'tuanam.ydet', 'tuansm.ydet_Ngu', 'Janam', 'điển']
        
        def _clean_noise(text):
            for word in noise_words:
                text = text.replace(word, '')
            text = re.sub(r'\d{2}/\d{2}/\d{4}.*?|\d{8,}', '', text) # Xóa ngày tháng 
            return clean_text(text)

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                if not tables:
                    continue
                    
                for table in tables:
                    # Bỏ qua các hàng tiêu đề (thường là 2-3 hàng đầu)
                    for row in table:
                        if not row or len(row) < 5:
                            continue
                            
                        # Kiểm tra xem đây có phải là hàng header không (bằng cách check ô đầu tiên)
                        col0 = clean_text(str(row[0] or ""))
                        if col0 in ["Mã dùng chung", "Mã dùng", "chung", "6500017", "6500040", "6500061"]: 
                             if not re.match(r'^65\d{5}$', col0):
                                 continue

                        ma_dung_chung = col0
                        
                        # Cấu trúc cột dựa trên PDF:
                        # 0: Mã dùng chung, 1: Tên kết hợp, 2: Tên YHHĐ, 3: Mã ICD-10
                        # 4: Bệnh danh YHCT, 5: Mã U, 6: Các thể lâm sàng, 7: Mã hóa
                        
                        ten_ket_hop = _clean_noise(str(row[1] or "")) if len(row) > 1 else ""
                        ten_yhhd = _clean_noise(str(row[2] or "")) if len(row) > 2 else ""
                        ma_icd10 = clean_text(str(row[3] or "")) if len(row) > 3 else ""
                        benh_danh_yhct = _clean_noise(str(row[4] or "")) if len(row) > 4 else ""
                        ma_u = clean_text(str(row[5] or "")) if len(row) > 5 else ""
                        the_lam_sang = _clean_noise(str(row[6] or "")) if len(row) > 6 else ""
                        
                        # Nếu là một mã chính (ví dụ 6500000) -> Khởi tạo record mới
                        if re.match(r'^65\d{5}$', ma_dung_chung):
                            # Nếu đã có record cũ thì lưu lại trước khi tạo mới
                            if current_record and (current_record["Tên bệnh theo YHHĐ"] or current_record["Bệnh danh YHCT"]):
                                all_data.append(current_record)
                                
                            current_record = {
                                "Mã ICD-10": ma_icd10,
                                "Tên bệnh theo YHHĐ": ten_yhhd,
                                "Bệnh danh YHCT": benh_danh_yhct,
                                "Mã U": ma_u,
                                "Các thể lâm sàng": the_lam_sang,
                                "Tên kết hợp": ten_ket_hop
                            }
                        
                        # Nếu dòng hiện tại không có mã số, nhưng có text -> Nối vào record hiện tại
                        elif current_record:
                            if ten_ket_hop: current_record["Tên kết hợp"] = (current_record["Tên kết hợp"] + " " + ten_ket_hop).strip()
                            if ten_yhhd: current_record["Tên bệnh theo YHHĐ"] = (current_record["Tên bệnh theo YHHĐ"] + " " + ten_yhhd).strip()
                            if ma_icd10: current_record["Mã ICD-10"] = (current_record["Mã ICD-10"] + " " + ma_icd10).strip()
                            if benh_danh_yhct: current_record["Bệnh danh YHCT"] = (current_record["Bệnh danh YHCT"] + " " + benh_danh_yhct).strip()
                            if ma_u: current_record["Mã U"] = (current_record["Mã U"] + " " + ma_u).strip()
                            if the_lam_sang: 
                                # Các thể lâm sàng thường nằm ở nhiều dòng khác nhau, ta nối bằng dấu phẩy
                                old_tls = current_record["Các thể lâm sàng"]
                                separator = ", " if old_tls and the_lam_sang not in old_tls else ""
                                current_record["Các thể lâm sàng"] = (old_tls + separator + the_lam_sang).strip()
                                
        # Đừng quên lưu record cuối cùng
        if current_record and (current_record["Tên bệnh theo YHHĐ"] or current_record["Bệnh danh YHCT"]):
            all_data.append(current_record)

        # Xóa các record rỗng hoàn toàn hoặc rác
        final_data = []
        for r in all_data:
            if r["Tên bệnh theo YHHĐ"] or r["Bệnh danh YHCT"] or r["Các thể lâm sàng"]:
                final_data.append(r)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(final_data, f, ensure_ascii=False, indent=2)
            
        print(f"✅ YHCT: {len(final_data)} bản ghi (đã gộp dòng) → {output_path}")

    except FileNotFoundError:
        print(f"❌ Không tìm thấy file: {pdf_path}")
    except Exception as e:
        print(f"❌ Lỗi xử lý YHCT: {e}")


if __name__ == "__main__":
    db_folder = "data"
    os.makedirs(db_folder, exist_ok=True)

    excel_file = "phu-lu-c-1-danh-mu-c-icd-10-thay-the-dmdc-phie-n-ba-n-6.xlsx"
    icd10_path = f"data/{excel_file}" if os.path.exists(f"data/{excel_file}") else excel_file
    phuluc_pdf = "data/PhuLuc1.pdf"   if os.path.exists("data/PhuLuc1.pdf")   else "PhuLuc1.pdf"

    process_icd10(icd10_path, os.path.join(db_folder, "icd10_dictionary.json"))
    process_yhct (phuluc_pdf,  os.path.join(db_folder, "yhct_dictionary.json"))

    print("\n🚀 ETL hoàn tất!")