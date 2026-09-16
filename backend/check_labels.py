import mysql.connector

conn = mysql.connector.connect(
    host="127.0.0.1", user="root", password="17092005khang", database="yhoc_corpus"
)
cur = conn.cursor(dictionary=True)

queries = [
    ("Tong articles",                   "SELECT COUNT(*) AS n FROM articles"),
    ("Co NER (extracted_concepts)",     "SELECT COUNT(DISTINCT article_id) AS n FROM extracted_concepts WHERE concept_code != ''"),
    ("Co AI Label",                     "SELECT COUNT(DISTINCT article_id) AS n FROM ai_document_labels"),
    ("Co ca NER lan AI",                """
        SELECT COUNT(*) AS n FROM articles a
        WHERE EXISTS(SELECT 1 FROM extracted_concepts ec WHERE ec.article_id=a.id AND ec.concept_code != '')
          AND EXISTS(SELECT 1 FROM ai_document_labels ai WHERE ai.article_id=a.id)
    """),
    ("Chi co NER (khong AI)",           """
        SELECT COUNT(*) AS n FROM articles a
        WHERE EXISTS(SELECT 1 FROM extracted_concepts ec WHERE ec.article_id=a.id AND ec.concept_code != '')
          AND NOT EXISTS(SELECT 1 FROM ai_document_labels ai WHERE ai.article_id=a.id)
    """),
    ("Chi co AI (khong NER)",           """
        SELECT COUNT(*) AS n FROM articles a
        WHERE NOT EXISTS(SELECT 1 FROM extracted_concepts ec WHERE ec.article_id=a.id AND ec.concept_code != '')
          AND EXISTS(SELECT 1 FROM ai_document_labels ai WHERE ai.article_id=a.id)
    """),
    ("Chua gan nhan gi",                """
        SELECT COUNT(*) AS n FROM articles a
        WHERE NOT EXISTS(SELECT 1 FROM extracted_concepts ec WHERE ec.article_id=a.id AND ec.concept_code != '')
          AND NOT EXISTS(SELECT 1 FROM ai_document_labels ai WHERE ai.article_id=a.id)
    """),
    ("AI label co ICD-10 code",         "SELECT COUNT(DISTINCT article_id) AS n FROM ai_document_labels WHERE primary_icd10_code IS NOT NULL AND primary_icd10_code != ''"),
    ("AI label KHONG co ICD-10 code",   "SELECT COUNT(DISTINCT article_id) AS n FROM ai_document_labels WHERE primary_icd10_code IS NULL OR primary_icd10_code = ''"),
]

print(f"\n{'='*55}")
print(f"{'Metric':<40} {'Count':>10}")
print(f"{'='*55}")
for label, q in queries:
    cur.execute(q)
    row = cur.fetchone()
    print(f"{label:<40} {row['n']:>10}")
print(f"{'='*55}\n")

# Chi tiet: nhung article co NER nhung khong xuat hien trong ICD10 list
print("\n--- Articles co NER nhung co_concept_code rong (khong hien ICD-10 list) ---")
cur.execute("""
    SELECT a.id, a.title, COUNT(ec.id) AS total_concepts,
           SUM(CASE WHEN ec.concept_code != '' THEN 1 ELSE 0 END) AS with_code
    FROM articles a
    JOIN extracted_concepts ec ON ec.article_id = a.id
    GROUP BY a.id, a.title
    HAVING with_code = 0
    LIMIT 10
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        print(f"  id={r['id']} total_concepts={r['total_concepts']} | {r['title'][:60]}")
else:
    print("  Khong co truong hop nao (tat ca NER deu co code).")

cur.close()
conn.close()
