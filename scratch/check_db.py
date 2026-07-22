import mysql.connector
import sys

# Thiết lập encoding cho terminal Windows
sys.stdout.reconfigure(encoding='utf-8')

db_config = {
    'user': 'root',
    'password': '17092005Khang',
    'host': 'localhost',
    'database': 'yhoc_corpus',
    'charset': 'utf8mb4'
}

try:
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(dictionary=True)
    
    # Lấy các khái niệm vừa được trích xuất
    cursor.execute("SELECT concept_name, concept_type FROM extracted_concepts ORDER BY id DESC LIMIT 30")
    rows = cursor.fetchall()
    print("TOP 30 CONCEPTS TRONG DB:")
    for r in rows:
        print(f"- {r['concept_name']} ({r['concept_type']})")
        
    cursor.close()
    conn.close()
except Exception as e:
    print(f"Error: {e}")
