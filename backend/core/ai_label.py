import os
import json
import re
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Cấu hình API key (đọc từ biến môi trường đã nạp trong main.py)
api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    genai.configure(api_key=api_key)

def extract_with_ai_label(text: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        genai.configure(api_key=api_key)
    if not api_key:
        raise ValueError("Chưa cấu hình GEMINI_API_KEY trong file .env")

    # Chọn model theo yêu cầu (gemini-2.5-flash)
    model = genai.GenerativeModel("gemini-2.5-flash")

    # Prompt do người dùng chỉ định
    prompt = f"""Bạn là một chuyên gia AI về Xử lý Ngôn ngữ Tự nhiên (NLP) chuyên ngành y khoa tại Việt Nam.

Nhiệm vụ: Đọc đoạn văn bản y tế dưới đây, nhận diện (extract) chính xác các cụm từ chỉ thực thể y khoa và phân loại chúng vào 6 nhóm nhãn dán cụ thể.

Danh sách các nhãn (Categories):

Bệnh lý: Tên các loại bệnh, hội chứng, tổn thương hoặc tình trạng y khoa bất thường (ví dụ: ung thư buồng trứng, đái tháo đường, suy tim).

Triệu chứng: Dấu hiệu lâm sàng, biểu hiện của bệnh (ví dụ: đau đầu, sốt cao, buồn nôn).

Điều trị: Các phương pháp can thiệp, thủ thuật chữa bệnh, phẫu thuật, hoặc tên thuốc (ví dụ: hóa trị, phẫu thuật nội soi, aspirin).

Xét nghiệm: Các kỹ thuật, chỉ số, hoặc phương pháp phân tích mẫu bệnh phẩm (ví dụ: ROMA test, CA125, HE4, xét nghiệm máu).

Hình ảnh: Các phương pháp chẩn đoán hình ảnh (ví dụ: siêu âm, X-quang, MRI, CT scan).

Sinh lý: Các quá trình, chức năng sinh lý bình thường của cơ thể (ví dụ: tình trạng kinh nguyệt, nhịp tim).

Yêu cầu bắt buộc:

Trích xuất đúng nguyên văn (exact match) cụm từ như nó xuất hiện trong văn bản.

Chỉ trích xuất những cụm từ thực sự quan trọng và có ý nghĩa y khoa.

Trả về kết quả CHỈ bằng định dạng JSON để hệ thống phần mềm có thể tự động parse và highlight, không kèm theo văn bản giải thích nào khác.

Định dạng JSON đầu ra mong muốn:

{{
  "Bệnh lý": ["cụm từ 1", "cụm từ 2"],
  "Triệu chứng": [],
  "Điều trị": [],
  "Xét nghiệm": [],
  "Hình ảnh": [],
  "Sinh lý": []
}}

Đoạn văn bản cần phân tích:
{text}
"""

    response = model.generate_content(prompt)
    out_text = response.text.strip()

    # Loại bỏ code block markdown nếu AI trả về (vd: ```json ... ```)
    out_text = re.sub(r"^```(?:json)?", "", out_text)
    out_text = re.sub(r"```$", "", out_text)
    out_text = out_text.strip()

    try:
        data = json.loads(out_text)
        from core.ner_dict import term_dict
        import unicodedata
        
        transformed_data = {}
        for cat, terms in data.items():
            transformed_terms = []
            for t in terms:
                normalized_t = unicodedata.normalize("NFC", t.strip().lower())
                dict_info = term_dict.get(normalized_t)
                if dict_info:
                    label_vn = dict_info.get("label_vn", "")
                    label_vn_clean = re.sub(r'\s*\([^)]*\)', '', label_vn).strip() if label_vn else label_vn
                    transformed_terms.append({
                        "term": t, 
                        "code": dict_info.get("code", ""),
                        "label_vn": label_vn_clean
                    })
                else:
                    transformed_terms.append({
                        "term": t, 
                        "code": "",
                        "label_vn": ""
                    })
            transformed_data[cat] = transformed_terms
            
        return transformed_data
    except Exception as e:
        print("Lỗi parse JSON từ AI Label:", e)
        print("Raw output:", out_text)
        return {
            "Bệnh lý": [],
            "Triệu chứng": [],
            "Điều trị": [],
            "Xét nghiệm": [],
            "Hình ảnh": [],
            "Sinh lý": []
        }
