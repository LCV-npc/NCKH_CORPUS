import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import mysql.connector
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


def inspect(row):
    try:
        response = requests.get(row["source_url"], timeout=40, verify=False, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        values = []
        for name in ("citation_publication_date", "citation_date", "DC.Date", "DC.Date.created"):
            tag = soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                values.append(tag["content"])
        published = soup.select_one(".published, .date_published, .item.published")
        if published:
            values.append(published.get_text(" ", strip=True))
        source_year = next((int(match.group(0)) for value in values if (match := re.search(r"20\d{2}", value))), None)
        return {"id": row["id"], "db_year": row["publication_year"], "source_year": source_year, "http": response.status_code}
    except Exception as exc:
        return {"id": row["id"], "db_year": row["publication_year"], "error": str(exc)}


def main():
    load_dotenv()
    connection = mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"), user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""), database=os.getenv("DB_NAME", "yhoc_corpus"),
    )
    cursor = connection.cursor(dictionary=True)
    cursor.execute("SELECT id, publication_year, source_url FROM articles ORDER BY id")
    rows = cursor.fetchall()
    cursor.close()
    connection.close()
    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(inspect, row) for row in rows]
        for future in as_completed(futures):
            results.append(future.result())
    errors = [row for row in results if row.get("error")]
    no_year = [row for row in results if not row.get("error") and row.get("source_year") is None]
    wrong = [row for row in results if row.get("source_year") is not None and row["source_year"] != row["db_year"]]
    counts = Counter((row["db_year"], row.get("source_year")) for row in results if not row.get("error"))
    print(json.dumps({
        "checked": len(results), "http_errors": len(errors), "source_year_missing": len(no_year),
        "wrong_year_count": len(wrong), "wrong_examples": wrong[:20],
        "year_pairs": [{"db": db, "source": source, "count": count} for (db, source), count in sorted(counts.items())],
        "error_examples": errors[:10],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
