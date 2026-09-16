import sys, mysql.connector
sys.stdout.reconfigure(encoding="utf-8")

conn = mysql.connector.connect(
    host="127.0.0.1", user="root", password="17092005khang", database="yhoc_corpus"
)
cur = conn.cursor(dictionary=True)

# Chi co AI Label (hien trong AI Labeled tab)
cur.execute("""
    SELECT a.id, a.title,
           ai.primary_icd10_code, ai.primary_icd10_label
    FROM articles a
    JOIN ai_document_labels ai ON ai.id=(
        SELECT MAX(ai2.id) FROM ai_document_labels ai2 WHERE ai2.article_id=a.id
    )
    WHERE NOT EXISTS(
        SELECT 1 FROM extracted_concepts ec
        WHERE ec.article_id=a.id AND ec.concept_code != ''
    )
    ORDER BY a.id DESC
""")
print("=== Chi co AI Label (khong NER) - chi hien trong AI Labeled ===")
for r in cur.fetchall():
    icd = r["primary_icd10_code"] or "NONE"
    print(f"  id={r['id']:4d}  icd={icd:15s} | {r['title'][:65]}")

# Co ca hai (hien trong CA HAI tab)
cur.execute("""
    SELECT a.id, a.title
    FROM articles a
    WHERE EXISTS(
        SELECT 1 FROM extracted_concepts ec
        WHERE ec.article_id=a.id AND ec.concept_code != ''
    )
    AND EXISTS(
        SELECT 1 FROM ai_document_labels ai WHERE ai.article_id=a.id
    )
    ORDER BY a.id DESC
""")
print()
print("=== Co ca NER lan AI - hien trong CA HAI tab (ICD-10 Labeled va AI Labeled) ===")
for r in cur.fetchall():
    print(f"  id={r['id']:4d} | {r['title'][:65]}")

cur.close()
conn.close()
