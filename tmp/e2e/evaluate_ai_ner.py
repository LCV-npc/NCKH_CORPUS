import json
import os
import urllib.request
import urllib.error

import mysql.connector
from dotenv import load_dotenv

from evaluate_manual_ner import GOLD_TERMS, metrics, non_overlapping_gold


def api_ai(text):
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/ai-label",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


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
    invalid_terms = []
    per_article = []
    valid_labels = {"Bệnh lý", "Triệu chứng", "Điều trị", "Xét nghiệm", "Hình ảnh", "Sinh lý"}
    for article_id, labels in GOLD_TERMS.items():
        text = articles[article_id]["abstract"]
        gold = non_overlapping_gold(text, labels)
        try:
            result = api_ai(text)
        except urllib.error.HTTPError as exc:
            invalid_terms.append({"id": article_id, "http_error": exc.code, "detail": exc.read().decode("utf-8", "replace")[:1000]})
            per_article.append({"id": article_id, "gold": len(gold), "error": exc.code})
            continue
        predicted = set()
        for label, entries in result.items():
            if label not in valid_labels:
                invalid_terms.append({"id": article_id, "label": label, "term": "<invalid-label>"})
                continue
            normalized_label = {
                "Bệnh lý": "Bệnh Lý", "Triệu chứng": "Triệu Chứng", "Điều trị": "Điều Trị",
                "Xét nghiệm": "Xét Nghiệm", "Hình ảnh": "Hình Ảnh", "Sinh lý": "Sinh Lý",
            }[label]
            for entry in entries:
                term = entry.get("term", "") if isinstance(entry, dict) else str(entry)
                starts = []
                position = 0
                while term and (position := text.find(term, position)) >= 0:
                    starts.append(position)
                    position += max(1, len(term))
                if not starts:
                    invalid_terms.append({"id": article_id, "label": label, "term": term})
                for start in starts:
                    predicted.add((start, start + len(term), normalized_label))

        labels_seen = sorted({item[2] for item in gold | predicted})
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
        per_article.append({"id": article_id, "gold": len(gold), "predicted": len(predicted)})

    all_counts = [sum(values[i] for values in totals.values()) for i in range(3)]
    print(json.dumps({
        "per_article": per_article,
        "verbatim_failures": invalid_terms,
        "per_label": {label: metrics(*counts) for label, counts in sorted(totals.items())},
        "micro": metrics(*all_counts),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
