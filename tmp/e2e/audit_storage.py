import json
import os
import re
from collections import Counter
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from langdetect import DetectorFactory, detect


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
DetectorFactory.seed = 0


def expected_language(text):
    if not text or len(text.strip()) < 20:
        return "Vietnamese"
    try:
        return "Vietnamese" if detect(text) == "vi" else "English"
    except Exception:
        return "Vietnamese"


def main():
    load_dotenv()
    connection = mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "yhoc_corpus"),
    )
    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT id, publication_year, abstract, source_url FROM articles ORDER BY id")
    articles = cursor.fetchall()
    cursor.close()
    connection.close()

    txt_root = BACKEND / "Kho_Ngu_Lieu_Txt" / "tapchiyhcd.vn"
    paths_by_id = {}
    for path in txt_root.rglob("*.txt"):
        match = re.search(r"_(\d{4})_", path.name)
        if match:
            paths_by_id[int(match.group(1))] = path

    stats = Counter()
    missing = []
    wrong_language = []
    wrong_year = []
    txt_content_mismatch = []
    for article in articles:
        expected = expected_language(article["abstract"])
        stats[(article["publication_year"], expected, "db")] += 1
        path = paths_by_id.get(article["id"])
        if not path:
            missing.append(article["id"])
            continue
        relative = path.relative_to(txt_root)
        actual_language = relative.parts[0] if len(relative.parts) else ""
        actual_year = relative.parts[1] if len(relative.parts) > 1 else ""
        stats[(article["publication_year"], actual_language, "txt")] += 1
        if actual_language != expected:
            wrong_language.append({"id": article["id"], "expected": expected, "actual": actual_language, "path": str(relative)})
        if actual_year != str(article["publication_year"]):
            wrong_year.append({"id": article["id"], "expected": article["publication_year"], "actual": actual_year, "path": str(relative)})
        content = path.read_text(encoding="utf-8", errors="replace")
        stored_abstract = content.split("TÓM TẮT:\n", 1)[1].strip() if "TÓM TẮT:\n" in content else ""
        if stored_abstract != (article["abstract"] or "").strip():
            txt_content_mismatch.append(article["id"])

    rows = [
        {"year": year, "language": language, "kind": kind, "count": count}
        for (year, language, kind), count in sorted(stats.items())
    ]
    pdf_root = BACKEND / "Văn_Bản_Y_Tế_PDF" / "tapchiyhcd.vn"
    pdfs = list(pdf_root.rglob("*.pdf")) if pdf_root.exists() else []
    pdf_language_paths = [path for path in pdfs if "English" in path.parts or "Vietnamese" in path.parts]
    print(json.dumps({
        "articles": len(articles),
        "rows": rows,
        "txt_missing_count": len(missing),
        "txt_missing_examples": missing[:20],
        "txt_wrong_language_count": len(wrong_language),
        "txt_wrong_language_examples": wrong_language[:10],
        "txt_wrong_year_count": len(wrong_year),
        "txt_content_mismatch_count": len(txt_content_mismatch),
        "txt_content_mismatch_examples": txt_content_mismatch[:20],
        "pdf_count": len(pdfs),
        "pdf_under_language_folder_count": len(pdf_language_paths),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
