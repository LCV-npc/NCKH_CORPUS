import os
import json
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Cấu hình API key từ biến môi trường
api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    genai.configure(api_key=api_key)

def extract_entities_with_ai(text: str) -> list[dict]:
    """
    Sử dụng Gemini AI để trích xuất thực thể y tế từ văn bản.
    Trả về danh sách dictionary: [{"name": "...", "type": "...", "code": "..."}, ...]
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        genai.configure(api_key=api_key)
    if not api_key:
        raise ValueError("Chưa cấu hình GEMINI_API_KEY trong file .env")
        
    model = genai.GenerativeModel("gemini-2.5-flash")
    
    prompt = f"""Bạn là một chuyên gia y tế và chuyên gia NLP. Hãy đọc đoạn văn bản y khoa tiếng Việt dưới đây và trích xuất tất cả các thực thể y tế.
Phân loại chúng vào một trong các loại sau:
- DISEASE (Bệnh lý)
- SYMPTOM (Triệu chứng)
- TREATMENT (Phương pháp điều trị)
- LAB_TEST (Xét nghiệm, chỉ số)
- IMAGING (Chẩn đoán hình ảnh)
- TRAD_MED (Đông y, y học cổ truyền)

Trả về kết quả DƯỚI DẠNG MẢNG JSON, mỗi phần tử là 1 object có các trường: "name" (tên thực thể trích xuất chính xác từ văn bản), "type" (loại thực thể bằng tiếng Anh như trên), "code" (mã ICD-10 nếu có, nếu không biết để trống "").
Tuyệt đối chỉ trả về mảng JSON, không giải thích gì thêm, không bọc trong markdown block.
Ví dụ định dạng trả về:
[
  {{"name": "đái tháo đường", "type": "DISEASE", "code": "E11"}},
  {{"name": "đau đầu", "type": "SYMPTOM", "code": "R51"}}
]

VĂN BẢN:
{text}
"""

    response = model.generate_content(prompt)
    output = response.text.strip()
    
    # Xử lý nếu model trả về markdown block
    if output.startswith("```json"):
        output = output[7:-3].strip()
    elif output.startswith("```"):
        output = output[3:-3].strip()
        
    try:
        entities = json.loads(output)
        return entities
    except json.JSONDecodeError:
        return []

if __name__ == "__main__":
    test_text = "Bệnh nhân nam 45 tuổi nhập viện vì sốt cao, ho có đờm. Được chẩn đoán viêm phổi cộng đồng (J15). Bác sĩ chỉ định chụp X-quang phổi và xét nghiệm CRP."
    try:
        res = extract_entities_with_ai(test_text)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"Lỗi: {e}")
