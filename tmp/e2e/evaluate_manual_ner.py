import json
import os
import urllib.request

import mysql.connector
from dotenv import load_dotenv


GOLD_TERMS = {
    860: {
        "Bệnh Lý": ["Xơ gan", "xơ gan", "Cổ trướng", "cổ trướng", "Thiếu máu", "Giảm albumin máu"],
        "Điều Trị": ["điều trị nội trú", "điều trị cổ trướng"],
        "Xét Nghiệm": ["Hb", "Siêu âm"],
    },
    865: {
        "Bệnh Lý": ["khuyết sọ", "chấn thương sọ não", "suy nhược thần kinh"],
        "Triệu Chứng": ["Triệu chứng thần kinh khu trú", "triệu chứng thần kinh khu trú", "đau đầu"],
        "Điều Trị": ["phẫu thuật mở sọ giảm áp", "phẫu thuật mở nắp sọ", "mổ ghép sọ"],
        "Hình Ảnh": ["cắt lớp vi tính sọ não", "CLVT sọ não"],
        "Xét Nghiệm": ["GCS"],
    },
    875: {
        "Bệnh Lý": ["đột quỵ không điển hình", "đột quỵ não", "đột quỵ"],
        "Triệu Chứng": ["không liệt chi", "không liệt dây VII", "chóng mặt", "rối loạn cảm giác khu trú", "đau đầu", "liệt"],
        "Xét Nghiệm": ["FAST", "BEFAST"],
    },
    876: {
        "Bệnh Lý": ["Đái tháo đường type 2", "đái tháo đường type 2", "bệnh đái tháo đường", "chứng Tiêu khát", "rối loạn chuyển hóa"],
        "Điều Trị": ["thuốc điều trị", "y học cổ truyền Việt Nam", "y học cổ truyền", "vị thuốc", "bài thuốc Nam", "bài thuốc đa thành phần", "liệu pháp bổ trợ", "phác đồ điều trị"],
    },
    904: {
        "Bệnh Lý": ["rối loạn giấc ngủ", "hưng cảm", "rối loạn nhịp thức ngủ sinh học", "mất ngủ"],
        "Triệu Chứng": ["giảm nhu cầu ngủ"],
    },
    17: {
        "Bệnh Lý": ["trầm cảm"],
        "Triệu Chứng": ["căng thẳng tâm lý", "Cảm xúc tiêu cực"],
    },
    21: {
        "Bệnh Lý": ["đái tháo đường type 2", "biến chứng"],
        "Điều Trị": ["tự tiêm Insulin", "tự tiêm insulin"],
    },
    24: {
        "Bệnh Lý": ["thoái hoá cột sống cổ"],
        "Triệu Chứng": ["VAS cổ", "VAS vai", "đau vùng cổ, vai"],
        "Điều Trị": ["tập vận động khớp cổ, vai", "bài tập khớp cột sống cổ", "phục hồi chức năng", "dùng thuốc", "tập vận động", "tự vận động cột sống cổ"],
        "Xét Nghiệm": ["thước đo tầm vận động khớp"],
    },
    31: {
        "Bệnh Lý": ["nhiễm trùng đường hô hấp dưới"],
        "Xét Nghiệm": ["PCR đa mồi tự động", "Film Array", "nuôi cấy"],
    },
    41: {
        "Bệnh Lý": ["loãng xương nặng", "loãng xương", "thiếu xương", "gãy xương"],
        "Xét Nghiệm": ["FRAX"],
    },
}


def non_overlapping_gold(text, labels):
    candidates = []
    for label, terms in labels.items():
        for term in terms:
            pos = 0
            while True:
                start = text.find(term, pos)
                if start < 0:
                    break
                candidates.append((start, start + len(term), label, term))
                pos = start + 1
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    kept = []
    for candidate in candidates:
        if any(candidate[0] < old[1] and old[0] < candidate[1] for old in kept):
            continue
        kept.append(candidate)
    return {(start, end, label) for start, end, label, _ in kept}


def normalize_label(label):
    return {
        "Bệnh lý": "Bệnh Lý",
        "Triệu chứng": "Triệu Chứng",
        "Điều trị": "Điều Trị",
        "Xét nghiệm": "Xét Nghiệm",
        "Hình ảnh": "Hình Ảnh",
    }.get(label, label)


def api_ner(text):
    data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/ner",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def metrics(tp, fp, fn):
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def main():
    load_dotenv()
    connection = mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "yhoc_corpus"),
    )
    cursor = connection.cursor(dictionary=True)
    placeholders = ",".join(["%s"] * len(GOLD_TERMS))
    cursor.execute(
        f"SELECT id, publication_year, abstract FROM articles WHERE id IN ({placeholders})",
        tuple(GOLD_TERMS),
    )
    articles = {row["id"]: row for row in cursor.fetchall()}
    cursor.close()
    connection.close()

    totals = {}
    per_article = []
    deterministic = True
    integrity = {"predicted_mentions": 0, "source_text_matches": 0, "overlaps": 0, "duplicate_spans": 0}
    for article_id, labels in GOLD_TERMS.items():
        text = articles[article_id]["abstract"]
        gold = non_overlapping_gold(text, labels)
        first = api_ner(text)
        second = api_ner(text)
        deterministic &= first == second
        predicted = {
            (entity.get("start", -1), entity.get("end", -1), normalize_label(entity.get("label", "")))
            for entity in first.get("entities", [])
            if entity.get("start", -1) >= 0
        }
        raw_entities = [entity for entity in first.get("entities", []) if entity.get("start", -1) >= 0]
        integrity["predicted_mentions"] += len(raw_entities)
        integrity["source_text_matches"] += sum(
            text[entity["start"]:entity["end"]] == entity.get("text") for entity in raw_entities
        )
        spans = [(entity["start"], entity["end"]) for entity in raw_entities]
        integrity["duplicate_spans"] += len(spans) - len(set(spans))
        spans = sorted(set(spans))
        integrity["overlaps"] += sum(left[0] < right[1] and right[0] < left[1] for left, right in zip(spans, spans[1:]))
        labels_seen = sorted({item[2] for item in gold | predicted})
        row = {"id": article_id, "year": articles[article_id]["publication_year"], "gold": len(gold), "predicted": len(predicted)}
        for label in labels_seen:
            gold_label = {item for item in gold if item[2] == label}
            pred_label = {item for item in predicted if item[2] == label}
            tp = len(gold_label & pred_label)
            fp = len(pred_label - gold_label)
            fn = len(gold_label - pred_label)
            bucket = totals.setdefault(label, [0, 0, 0])
            bucket[0] += tp
            bucket[1] += fp
            bucket[2] += fn
        per_article.append(row)

    output = {
        "gold_definition": "10 Vietnamese abstracts; manually selected exact source spans; longest non-overlapping mention wins",
        "deterministic_two_runs": deterministic,
        "per_article": per_article,
        "offset_integrity": integrity,
        "per_label": {label: metrics(*counts) for label, counts in sorted(totals.items())},
    }
    all_counts = [sum(values[i] for values in totals.values()) for i in range(3)]
    output["micro"] = metrics(*all_counts)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
