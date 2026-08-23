from __future__ import annotations

import json
import os
import re
from typing import Any

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

AI_CATEGORIES = (
    "Bệnh lý",
    "Triệu chứng",
    "Điều trị",
    "Xét nghiệm",
    "Hình ảnh",
    "Sinh lý",
)

DICTIONARY_TO_AI_CATEGORY = {
    "Bệnh Lý": "Bệnh lý",
    "Triệu Chứng": "Triệu chứng",
    # Giao diện AI hiện có 6 nhóm. Các bệnh danh YHCT được xếp vào nhóm
    # bệnh lý nhưng vẫn giữ dictionary_type để tooltip hiển thị đúng nguồn.
    "Đông Y / YHCT": "Bệnh lý",
}

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")


def _empty_result() -> dict[str, list[dict[str, Any]]]:
    return {category: [] for category in AI_CATEGORIES}


def _find_spans(text: str, term: str) -> list[dict[str, int]]:
    """Find whole-token occurrences without splitting decomposed Vietnamese.

    ``re.finditer`` on a raw substring incorrectly finds ``ho`` inside a
    decomposed ``học``.  Reusing the rule engine's tokenizer gives AI and
    dictionary labels the same source-safe word-boundary semantics.
    """
    if not text or not term:
        return []
    from core.ner_engine import NEREngine
    from core.ner_dict import normalize_match_text

    token_keys = normalize_match_text(term).split()
    if not token_keys:
        return []
    tokens = NEREngine()._tokenize(text)
    width = len(token_keys)
    spans: list[dict[str, int]] = []
    for index in range(0, len(tokens) - width + 1):
        chunk = tokens[index:index + width]
        if [token.key for token in chunk] == token_keys:
            spans.append({"start": chunk[0].start, "end": chunk[-1].end})
    return spans


def _dictionary_candidates(text: str) -> list[dict[str, Any]]:
    """Chỉ lấy các mục Từ_Điển_v1 thực sự xuất hiện trong abstract."""
    from core.ner_engine import run_ner

    _, _, entities, _ = run_ner(
        text,
        enable_tone_restore=False,
        enable_noun_phrase=False,
    )
    return [
        {
            "term": entity["text"],
            "start": entity["start"],
            "end": entity["end"],
            "code": entity.get("display_code") or entity.get("icd_code", ""),
            "label_vn": entity.get("icd_label_vn", ""),
            "dictionary_type": entity.get("entity_type", ""),
            "matched_by": entity.get("matched_by", "exact"),
        }
        for entity in entities
    ]


def _merge_ai_and_dictionary(
    text: str,
    ai_data: Any,
    dictionary_candidates: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Xác thực span AI, gắn metadata từ điển và bổ sung mục AI bỏ sót."""
    from core.ner_dict import normalize_match_text

    result = _empty_result()
    dictionary_by_term: dict[str, dict[str, Any]] = {}
    dictionary_spans: dict[tuple[str, str], list[dict[str, int]]] = {}
    dictionary_intervals: list[tuple[int, int]] = []

    for candidate in dictionary_candidates:
        key = normalize_match_text(candidate.get("term", ""))
        code = str(candidate.get("code") or "")
        if not key:
            continue
        dictionary_by_term.setdefault(key, candidate)
        span = {"start": int(candidate["start"]), "end": int(candidate["end"])}
        dictionary_spans.setdefault((key, code), []).append(span)
        dictionary_intervals.append((span["start"], span["end"]))

    collected: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_item(category: str, term: str, dictionary_info: dict[str, Any] | None = None) -> None:
        clean_term = str(term or "").strip()
        if not clean_term:
            return
        spans = _find_spans(text, clean_term)
        if not spans:
            # Không nhận thuật ngữ do AI diễn giải hoặc bịa ra ngoài nguyên văn.
            return

        normalized = normalize_match_text(clean_term)
        info = dictionary_info or dictionary_by_term.get(normalized)
        if info:
            category = DICTIONARY_TO_AI_CATEGORY.get(
                str(info.get("dictionary_type") or ""), category
            )
            code = str(info.get("code") or "")
            spans = dictionary_spans.get((normalized, code), spans)
            label_vn = str(info.get("label_vn") or "")
            dictionary_type = str(info.get("dictionary_type") or "")
            matched_by = str(info.get("matched_by") or "dictionary")
            source = "ai+dictionary"
        else:
            # A shorter AI term cannot be emitted in addition to a validated
            # dictionary term that fully contains it.  Example: do not label
            # "u mô" separately inside "ung thư biểu mô tuyến tiền liệt".
            spans = [
                span for span in spans
                if not any(
                    left <= span["start"]
                    and span["end"] <= right
                    and (right - left) > (span["end"] - span["start"])
                    for left, right in dictionary_intervals
                )
            ]
            if not spans:
                return
            code = ""
            label_vn = ""
            dictionary_type = ""
            matched_by = "ai"
            source = "ai"

        if category not in AI_CATEGORIES:
            return
        first = spans[0]
        surface = text[first["start"]:first["end"]]
        identity = (normalize_match_text(surface), category, code)
        item = collected.get(identity)
        if item:
            existing = {(span["start"], span["end"]) for span in item["spans"]}
            item["spans"].extend(
                span for span in spans if (span["start"], span["end"]) not in existing
            )
            item["spans"].sort(key=lambda span: (span["start"], span["end"]))
            return

        collected[identity] = {
            "term": surface,
            "code": code,
            "label_vn": label_vn,
            "dictionary_type": dictionary_type,
            "matched_by": matched_by,
            "source": source,
            "spans": sorted(spans, key=lambda span: (span["start"], span["end"])),
        }

    if isinstance(ai_data, dict):
        for category in AI_CATEGORIES:
            terms = ai_data.get(category, [])
            if not isinstance(terms, list):
                continue
            for value in terms:
                term = value.get("term", "") if isinstance(value, dict) else value
                add_item(category, str(term or ""))

    # Từ điển là nguồn chuẩn: mục đã khớp không bị mất ngay cả khi AI bỏ sót.
    for candidate in dictionary_candidates:
        target_category = DICTIONARY_TO_AI_CATEGORY.get(
            str(candidate.get("dictionary_type") or ""), "Bệnh lý"
        )
        add_item(target_category, str(candidate.get("term") or ""), candidate)

    for (_, category, _), item in collected.items():
        result[category].append(item)

    for category in AI_CATEGORIES:
        result[category].sort(
            key=lambda item: (
                item["spans"][0]["start"] if item["spans"] else len(text),
                -len(item["term"]),
            )
        )
    return result


def extract_with_ai_label(text: str) -> dict[str, list[dict[str, Any]]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Chưa cấu hình GEMINI_API_KEY trong file .env")
    genai.configure(api_key=api_key)

    dictionary_candidates = _dictionary_candidates(text)
    dictionary_context = [
        {
            "term": item["term"],
            "category": item["dictionary_type"],
            "code": item["code"],
            "canonical_label": item["label_vn"],
        }
        for item in dictionary_candidates
    ]

    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = f"""Bạn là chuyên gia NLP y khoa Việt Nam. Hãy trích xuất đúng nguyên văn
các thực thể y khoa trong văn bản và phân loại vào đúng 6 nhóm sau:

- Bệnh lý: bệnh, hội chứng, tổn thương hoặc tình trạng bất thường.
- Triệu chứng: dấu hiệu và biểu hiện lâm sàng.
- Điều trị: thuốc, thủ thuật, phẫu thuật hoặc phương pháp điều trị.
- Xét nghiệm: xét nghiệm, chỉ số hoặc phương pháp phân tích mẫu.
- Hình ảnh: siêu âm, X-quang, MRI, CT và phương pháp chẩn đoán hình ảnh.
- Sinh lý: quá trình hoặc chức năng sinh lý bình thường.

Các mục Từ_Điển_v1 đã được hệ thống đối chiếu trong chính văn bản này:
{json.dumps(dictionary_context, ensure_ascii=False)}

Quy tắc bắt buộc:
1. Chỉ trả thuật ngữ xuất hiện nguyên văn trong văn bản, không diễn giải lại.
2. Các mục từ điển trên là dữ kiện chuẩn; phải giữ lại và dùng category/code/canonical_label
   làm căn cứ. Backend sẽ xác nhận lại mã và chú thích sau câu trả lời của bạn.
3. Chỉ trả JSON, không Markdown và không giải thích.
4. Dùng đúng sáu khóa dưới đây, mỗi giá trị là mảng chuỗi.

{{
  "Bệnh lý": [],
  "Triệu chứng": [],
  "Điều trị": [],
  "Xét nghiệm": [],
  "Hình ảnh": [],
  "Sinh lý": []
}}

Văn bản:
{text}
"""

    response = model.generate_content(prompt, generation_config={"temperature": 0})
    output = response.text.strip()
    output = re.sub(r"^```(?:json)?\s*", "", output, flags=re.IGNORECASE)
    output = re.sub(r"\s*```$", "", output).strip()

    try:
        ai_data = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        # Vẫn trả kết quả chắc chắn từ Từ_Điển_v1 nếu AI sai định dạng.
        ai_data = {}

    return _merge_ai_and_dictionary(text, ai_data, dictionary_candidates)
