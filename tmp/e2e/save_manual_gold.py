import json
import os
import urllib.request

import mysql.connector
from dotenv import load_dotenv

from evaluate_manual_ner import GOLD_TERMS


def post(path, payload):
    request = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main():
    load_dotenv()
    connection = mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"), user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""), database=os.getenv("DB_NAME", "yhoc_corpus"),
    )
    cursor = connection.cursor(dictionary=True)
    placeholders = ",".join(["%s"] * len(GOLD_TERMS))
    cursor.execute(f"SELECT id, abstract FROM articles WHERE id IN ({placeholders})", tuple(GOLD_TERMS))
    articles = {row["id"]: row["abstract"] for row in cursor.fetchall()}
    cursor.close()
    connection.close()

    results = []
    for article_id in GOLD_TERMS:
        ner = post("/api/ner", {"text": articles[article_id]})
        saved = post("/api/save-highlight", {
            "article_id": article_id,
            "highlighted_html": ner["highlighted_html"],
            "matched_concepts": ner["matched_concepts"],
        })
        results.append({
            "id": article_id,
            "entities": len(ner["entities"]),
            "concepts": len(ner["matched_concepts"]),
            "saved": saved,
        })
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
