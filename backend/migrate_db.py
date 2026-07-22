import mysql.connector

db_config = {
    "user": "root",
    "password": "17092005Khang",
    "host": "localhost",
    "database": "yhoc_corpus",
    "charset": "utf8mb4",
}

try:
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor()
    
    # Try adding status
    try:
        cursor.execute("ALTER TABLE crawl_logs ADD COLUMN status VARCHAR(20) DEFAULT 'running'")
        print("Added status column.")
    except Exception as e:
        print(f"status column error: {e}")

    # Try adding stopped_at
    try:
        cursor.execute("ALTER TABLE crawl_logs ADD COLUMN stopped_at TIMESTAMP NULL")
        print("Added stopped_at column.")
    except Exception as e:
        print(f"stopped_at column error: {e}")
        
    conn.commit()
    cursor.close()
    conn.close()
    print("Migration finished.")
except Exception as e:
    print(f"DB Error: {e}")
