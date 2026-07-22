import os
import json
import mysql.connector
from datetime import datetime
import re
from dotenv import load_dotenv

load_dotenv()

db_config = {
    'user': 'root',
    'password': os.getenv("DB_PASSWORD", "17092005Khang"),
    'host': 'localhost',
    'database': 'yhoc_corpus',
    'charset': 'utf8mb4'
}

OUTPUT_FOLDER = "Kho Ngữ Liệu Y Học Tiếng Việt"
if not os.path.exists(OUTPUT_FOLDER):
    os.makedirs(OUTPUT_FOLDER)

def clean_filename(filename):
    filename = re.sub(r'[\t\n\r\f\v]+', ' ', filename)
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = re.sub(r'\s+', ' ', filename)
    return filename[:150].strip()

def export_json():
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        
        # Get all concepts linked with their articles
        cursor.execute("""
            SELECT a.id, a.title, c.concept_name, c.concept_type 
            FROM articles a
            JOIN extracted_concepts c ON a.id = c.article_id
        """)
        
        rows = cursor.fetchall()
        
        if not rows:
            print("Không tìm thấy dữ liệu Khái niệm AI nào trong Database.")
            return

        # Group concepts by article
        articles_map = {}
        for row in rows:
            a_id = row['id']
            if a_id not in articles_map:
                articles_map[a_id] = {
                    'title': row['title'] or "Khong_Tieu_De",
                    'concepts': []
                }
            
            articles_map[a_id]['concepts'].append({
                'name': (row['concept_name'] or "").strip(),
                'type': (row['concept_type'] or "Khái niệm").strip()
            })
            
        today_str = datetime.now().strftime('%d%m%Y')
        exported_count = 0
        
        for a_id, data in articles_map.items():
            title = data['title']
            concepts = data['concepts']
            
            # 1. Lọc trùng lặp từ khóa trong cùng 1 bài (theo yêu cầu)
            seen = set()
            unique_concepts = []
            for c in concepts:
                name_lower = c['name'].lower()
                if name_lower not in seen:
                    seen.add(name_lower)
                    unique_concepts.append(c)
                    
            # 2. Xử lý tên bài báo (tối đa 40 từ)
            safe_title = clean_filename(title)
            words = safe_title.split()
            short_title = " ".join(words[:40])
            
            # 3. Tạo tên file theo định dạng: [Ngày_Tháng_Năm]_[Tên_Bài_Báo_40_Từ]_Từ_Khái_niệm_[ID_Bài].json
            stt = f"{a_id:04d}"
            filename = f"{today_str}_{short_title}_Từ_Khái_niệm_{stt}.json"
            filepath = os.path.join(OUTPUT_FOLDER, filename)
            
            # 4. Ghi file JSON với cấu trúc đơn thuần [ {"name": "...", "type": "..."} ]
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(unique_concepts, f, ensure_ascii=False, indent=4)
                
            exported_count += 1
            
        print(f"Đã xuất thành công {exported_count} file JSON vào thư mục '{OUTPUT_FOLDER}'.")
        
    except Exception as e:
        print(f"Lỗi: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

if __name__ == "__main__":
    export_json()
