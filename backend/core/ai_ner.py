import os
import json
import logging
import re
import google.generativeai as genai
from config.env import load_backend_env

load_backend_env()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
logger = logging.getLogger(__name__)

ALLOWED_TYPES = {
    "DISEASE",
    "SYMPTOM",
    "CAUSE",
    "DIAGNOSTIC_PROCEDURE",
    "TREATMENT",
}

# Dự phòng cho trường hợp Gemini trả rỗng/không đúng JSON. Các cụm này chỉ
# được dùng khi xuất hiện nguyên văn trong bài, nên không làm sai dữ liệu gốc.
FALLBACK_TERMS = {
    "DISEASE": [
        "viêm phổi", "viêm dạ dày ruột", "bệnh sởi", "sởi", "bệnh nền",
        "đái tháo đường", "tăng huyết áp", "ung thư", "viêm gan",
    ],
    "SYMPTOM": [
        "sốt cao", "ho có đờm", "khó thở", "đau đầu", "đau bụng",
        "biến chứng nặng",
    ],
    "CAUSE": [
        "chưa được tiêm vắc xin", "tiêm vắc xin", "bệnh nền", "yếu tố nguy cơ",
    ],
    "DIAGNOSTIC_PROCEDURE": [
        "chụp X-quang", "xét nghiệm CRP", "xét nghiệm", "siêu âm", "chụp cắt lớp",
    ],
    "TREATMENT": [
        "điều trị", "phẫu thuật", "kháng sinh", "tiêm vắc xin", "chăm sóc",
    ],
}


def _fallback_entities(text: str) -> list[dict]:
    """Trích các cụm chắc chắn có trong văn bản khi AI không trả kết quả."""
    found = []
    seen = set()
    for entity_type, terms in FALLBACK_TERMS.items():
        for term in terms:
            match = re.search(re.escape(term), text, flags=re.IGNORECASE)
            if not match:
                continue
            name = match.group(0)
            key = (name.casefold(), entity_type)
            if key not in seen:
                seen.add(key)
                found.append({"name": name, "type": entity_type, "code": ""})
    return found

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
        
    if not text or not text.strip():
        raise ValueError("Văn bản gửi đến Gemini đang rỗng")

    model = genai.GenerativeModel(GEMINI_MODEL)
    
    prompt = f"""Bạn là chuyên gia y tế và NLP. Hãy trích xuất các thực thể y tế từ văn bản tiếng Việt nằm trong thẻ <van_ban>.

Mỗi thực thể phải có "type" là đúng một trong 5 nhãn:
- DISEASE: bệnh hoặc tình trạng bệnh lý
- SYMPTOM: dấu hiệu, triệu chứng
- CAUSE: nguyên nhân, tác nhân gây bệnh, yếu tố nguy cơ
- DIAGNOSTIC_PROCEDURE: xét nghiệm, thăm khám, chẩn đoán hình ảnh, phương pháp chẩn đoán
- TREATMENT: thuốc, thủ thuật, phẫu thuật, phương pháp điều trị (kể cả gây tê, giảm đau, tiêm chủng điều trị)

Quy tắc bắt buộc:
1. "name" phải sao chép nguyên văn từ văn bản, giữ nguyên dấu và chữ hoa/thường, không dịch, không chuẩn hóa, không viết lại.
2. Lấy cụm ngắn gọn nhưng đủ nghĩa, không kèm số liệu thống kê, tỉ lệ, p-value hay khoảng tin cậy. Ví dụ: "sốt cao > 39°C" lấy là "sốt cao".
3. Mỗi thực thể chỉ liệt kê một lần dù xuất hiện nhiều lần.
4. Một thực thể chỉ có một type, chọn theo vai trò của nó trong câu. Ví dụ: "vi rút sởi" là CAUSE, "bệnh nền" khi được nêu như yếu tố làm tăng nguy cơ là CAUSE.
5. Bỏ qua các từ không thuộc 5 nhãn (giải phẫu, nhóm tuổi, giới tính, chỉ số thống kê, cảm xúc, mức độ hài lòng).
6. "code" chỉ điền mã ICD-10 cho DISEASE khi thật sự chắc chắn, các trường hợp còn lại để "". Không đoán mã.
7. Nếu không có thực thể nào, trả về [].

Chỉ trả về một mảng JSON hợp lệ, không giải thích và không dùng Markdown.
Định dạng đầu ra: chỉ một mảng JSON hợp lệ, không giải thích, không Markdown, không có chữ nào ngoài JSON.
Mỗi phần tử phải có đúng dạng:
{{"name":"chuỗi nguyên văn","type":"DISEASE|SYMPTOM|CAUSE|DIAGNOSTIC_PROCEDURE|TREATMENT","code":""}}

<van_ban>
{text}
</van_ban>
"""

    response = model.generate_content(prompt)
    raw_output = getattr(response, "text", "") or ""
    logger.info("Gemini raw response (%d chars): %s", len(raw_output), raw_output[:4000])

    output = raw_output.strip()
    # Gemini đôi khi bọc JSON trong Markdown hoặc thêm câu dẫn.
    if "```" in output:
        output = output.replace("```json", "").replace("```JSON", "").replace("```", "").strip()

    try:
        entities = json.loads(output)
    except json.JSONDecodeError:
        # Lấy phần JSON đầu tiên nếu model thêm lời giải thích trước/sau mảng.
        start = output.find("[")
        end = output.rfind("]")
        if start >= 0 and end > start:
            try:
                entities = json.loads(output[start:end + 1])
            except json.JSONDecodeError:
                entities = None
        else:
            entities = None

    # Chấp nhận cả dạng {"entities": [...]} để không làm mất kết quả.
    if isinstance(entities, dict):
        entities = entities.get("entities", entities.get("results", []))
    if not isinstance(entities, list):
        logger.warning("Gemini response không phải JSON list hợp lệ: %s", raw_output[:4000])
        return _fallback_entities(text)

    seen = set()
    cleaned = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        entity_type = str(entity.get("type") or "").strip().upper()
        if not name or entity_type not in ALLOWED_TYPES:
            logger.warning("Bỏ qua entity không hợp lệ: %s", entity)
            continue
        key = (name, entity_type)
        if key in seen:
            continue
        seen.add(key)
        code = str(entity.get("code") or "").strip() if entity_type == "DISEASE" else ""
        cleaned.append({"name": name, "type": entity_type, "code": code})

    if not cleaned:
        cleaned = _fallback_entities(text)
        logger.warning("Gemini không trả entity hợp lệ; dùng fallback: %d entities", len(cleaned))
    else:
        logger.info("Gemini extracted %d valid entities", len(cleaned))
    return cleaned

if __name__ == "__main__":
    test_text = "Bệnh nhân nam 45 tuổi nhập viện vì sốt cao, ho có đờm. Được chẩn đoán viêm phổi cộng đồng (J15). Bác sĩ chỉ định chụp X-quang phổi và xét nghiệm CRP."
    try:
        res = extract_entities_with_ai(test_text)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"Lỗi: {e}")
