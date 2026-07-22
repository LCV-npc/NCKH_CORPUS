import threading
import json
import os
import io
import mysql.connector
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
from core.ner_engine import run_ner
from core.scraper import scrape_status, run_scraping, clean_filename, stop_scraping
from core.ai_ner import extract_entities_with_ai
from core.ai_label import extract_with_ai_label

router = APIRouter()

# Đường dẫn file Từ Điển
DICTIONARY_PATH = os.path.join("Kho Ngữ Liệu Y Học Tiếng Việt", "Từ_Điển.json")
_db_config: dict = {}
_output_folder: str = ""

def init_router(db_config: dict, output_folder: str) -> None:
    """Khởi tạo các biến cấu hình cho router."""
    global _db_config, _output_folder
    _db_config = db_config
    _output_folder = output_folder

class HighlightRequest(BaseModel):
    text:                str
    threshold:           int  = 100
    enable_tone_restore: bool = True
    enable_noun_phrase:  bool = True

class NerRequest(BaseModel):
    text:                str
    threshold:           int  = 100
    enable_tone_restore: bool = True
    enable_noun_phrase:  bool = True

class AiLabelRequest(BaseModel):
    text: str

class SaveHighlightRequest(BaseModel):
    article_id:       int
    highlighted_html: str
    matched_concepts: list

class ScrapeRequest(BaseModel):
    start_year: int
    end_year:   int
    target_url: str

class SaveDictionaryRequest(BaseModel):
    matched_concepts: list

def _get_conn():
    return mysql.connector.connect(**_db_config)

@router.post("/api/highlight-text")
def highlight_text_endpoint(req: HighlightRequest):
    """Phân tích văn bản — trả về HTML highlight + danh sách khái niệm."""
    highlighted, concepts, _, preproc_log = run_ner(
        req.text,
        req.threshold,
        enable_tone_restore=req.enable_tone_restore,
        enable_noun_phrase=req.enable_noun_phrase,
    )
    note = "" if concepts else "Đoạn văn mô tả cơ chế sinh lý, không chứa chẩn đoán bệnh lý cụ thể"
    return {
        "highlighted_html":  highlighted,
        "matched_concepts":  concepts,
        "note":              note,
        "preprocessing_log": preproc_log,
    }


@router.post("/api/ner")
def ner_endpoint(req: NerRequest):
    """Endpoint NER chuẩn — trả về JSON entities theo schema nghiên cứu."""
    highlighted, concepts, entities, preproc_log = run_ner(
        req.text,
        req.threshold,
        enable_tone_restore=req.enable_tone_restore,
        enable_noun_phrase=req.enable_noun_phrase,
    )
    note = "" if entities else "Đoạn văn mô tả cơ chế sinh lý, không chứa chẩn đoán bệnh lý cụ thể"
    return {
        "highlighted_html": highlighted,
        "entities": [
            {
                "text":         e["text"],
                "label":        e["entity_type"],   # schema mới
                "entity_type":  e["entity_type"],   # legacy
                "start":        e.get("start", -1),
                "end":          e.get("end", -1),
                "icd_code":     e.get("icd_code", ""),
                "icd_label_vn": e.get("icd_label_vn", ""),
                "matched_by":   e.get("matched_by", "exact"),
            }
            for e in entities
        ],
        "note":              note or None,
        "matched_concepts":  concepts,
        "preprocessing_log": preproc_log,
    }


@router.post("/api/ai-ner")
def ai_ner_endpoint(req: NerRequest):
    """Endpoint dùng AI (Gemini) để gán nhãn văn bản và tìm kiếm các thuật ngữ mới."""
    try:
        entities = extract_entities_with_ai(req.text)
        return {
            "entities": entities,
            "message": "Trích xuất thành công bằng AI"
        }
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as e:
        raise HTTPException(500, f"Lỗi AI: {str(e)}")


@router.post("/api/ai-label")
def ai_label_endpoint(req: AiLabelRequest):
    """Endpoint dùng Gemini để gán nhãn thực thể y khoa (6 nhóm)."""
    try:
        result = extract_with_ai_label(req.text)
        return result
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as e:
        raise HTTPException(500, f"Lỗi AI Gán Nhãn: {str(e)}")





# ── Endpoint mới: Tách PDF & Nhật ký ──────────────────────────────────

@router.get("/api/crawl-logs")
def get_crawl_logs():
    """Lấy danh sách nhật ký thu thập."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM crawl_logs ORDER BY created_at DESC LIMIT 100")
        return cursor.fetchall()
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()

@router.get("/api/verify-data")
def verify_data():
    """Kiểm chứng dữ liệu: đối chiếu DB và thư mục file PDF/TXT."""
    conn = cursor = None
    try:
        conn = _get_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, title, publication_year FROM articles")
        articles = cursor.fetchall()
        
        pdf_dir = os.path.abspath("Văn_Bản_Y_Tế_PDF")
        txt_dir = os.path.abspath(_output_folder)
        
        missing_pdfs = []
        missing_txts = []
        
        all_pdfs = []
        if os.path.exists(pdf_dir):
            for root, dirs, files in os.walk(pdf_dir):
                all_pdfs.extend([f.lower() for f in files if f.endswith('.pdf')])
                
        all_txts = []
        if os.path.exists(txt_dir):
            for root, dirs, files in os.walk(txt_dir):
                all_txts.extend([f.lower() for f in files if f.endswith('.txt')])
                
        for art in articles:
            safe_title = clean_filename(art['title'])
            
            # Kiểm tra PDF: safe_title có thể đã bị cắt gọn
            pdf_found = False
            short_title = safe_title[:45].lower()
            for pdf_file in all_pdfs:
                if short_title in pdf_file:
                    pdf_found = True
                    break
            if not pdf_found:
                missing_pdfs.append({
                    "id": art["id"], 
                    "title": art["title"], 
                    "year": art["publication_year"], 
                    "reason": "Không tìm thấy file PDF (có thể trang web không có PDF hoặc bị lỗi tải)"
                })
                
            # Kiểm tra TXT (có chứa ID trong tên file, ví dụ _0001_)
            txt_found = False
            id_str = f"_{art['id']:04d}"
            for txt_file in all_txts:
                if id_str in txt_file:
                    txt_found = True
                    break
            if not txt_found:
                missing_txts.append({
                    "id": art["id"], 
                    "title": art["title"], 
                    "year": art["publication_year"], 
                    "reason": "Lỗi lưu file txt hoặc dữ liệu đã bị xóa"
                })
                
        return {
            "total_articles_in_db": len(articles),
            "total_pdfs_on_disk": len(all_pdfs),
            "total_txts_on_disk": len(all_txts),
            "missing_pdfs_count": len(missing_pdfs),
            "missing_txts_count": len(missing_txts),
            "missing_pdfs": missing_pdfs[:50],  # Trả về 50 lỗi đầu tiên để tránh quá tải
            "missing_txts": missing_txts[:50]
        }
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()

import shutil
import tempfile
from core.pdf_pipeline import ExtractorPipeline

@router.post("/api/extract-pdf")
async def extract_pdf_endpoint(file: UploadFile = File(...)):
    """Tách file PDF thành các section txt."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Chỉ hỗ trợ file PDF (.pdf)")

    temp_dir = tempfile.mkdtemp()
    
    # Lấy tên file an toàn (tránh lỗi khi filename chứa đường dẫn thư mục con)
    safe_filename = os.path.basename(file.filename)
    if not safe_filename:
        safe_filename = "temp.pdf"
        
    temp_path = os.path.join(temp_dir, safe_filename)
    
    try:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        pipeline = ExtractorPipeline()
        metadata = pipeline.run(temp_path)
        
        return {
            "message": "Tách PDF thành công",
            "files_created": metadata.extracted_files,
            "hash": metadata.file_hash_sha256
        }
    except Exception as e:
        raise HTTPException(500, f"Lỗi xử lý PDF: {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        os.rmdir(temp_dir)

@router.post("/api/save-highlight")
def save_highlight_endpoint(req: SaveHighlightRequest):
    """Lưu kết quả highlight vào DB."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id FROM articles WHERE id = %s", (req.article_id,))
        if not cursor.fetchone():
            raise HTTPException(404, f"Không tìm thấy bài báo id={req.article_id}")

        cursor.execute(
            "UPDATE articles SET highlighted_html = %s WHERE id = %s",
            (req.highlighted_html, req.article_id),
        )
        cursor.execute(
            "DELETE FROM extracted_concepts WHERE article_id = %s",
            (req.article_id,),
        )

        seen, rows = set(), []
        for c in req.matched_concepts:
            name  = (c.get("name") or "").strip()
            ctype = (c.get("type") or "DISEASE").strip()
            code  = (c.get("code") or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                rows.append((req.article_id, name, ctype, code))

        if rows:
            cursor.executemany(
                "INSERT INTO extracted_concepts "
                "(article_id, concept_name, concept_type, concept_code) "
                "VALUES (%s, %s, %s, %s)",
                rows,
            )
        conn.commit()
        return {"message": "Lưu thành công", "article_id": req.article_id, "concepts_saved": len(rows)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


@router.get("/api/dictionary")
def get_dictionary():
    """Trả về toàn bộ từ điển y khoa."""
    try:
        if os.path.exists(DICTIONARY_PATH) and os.path.getsize(DICTIONARY_PATH) > 0:
            with open(DICTIONARY_PATH, "r", encoding="utf-8") as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return []
        return []
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/api/save-to-dictionary")
def save_to_dictionary_endpoint(req: SaveDictionaryRequest):
    """Lưu các cụm từ được highlight vào file Từ_Điển.json, bỏ qua nếu đã tồn tại."""
    try:
        # Đọc dữ liệu hiện có trong từ điển
        if os.path.exists(DICTIONARY_PATH) and os.path.getsize(DICTIONARY_PATH) > 0:
            with open(DICTIONARY_PATH, "r", encoding="utf-8") as f:
                try:
                    dictionary = json.load(f)
                except json.JSONDecodeError:
                    dictionary = []
        else:
            dictionary = []

        # Tập hợp các từ đã có (lowercase để so sánh không phân biệt hoa/thường)
        existing_terms = {entry.get("term", "").strip().lower() for entry in dictionary}

        added = []
        skipped = []

        for c in req.matched_concepts:
            name  = (c.get("name") or "").strip()
            ctype = (c.get("type") or "DISEASE").strip()
            code  = (c.get("code") or "").strip()

            if not name:
                continue

            if name.lower() in existing_terms:
                skipped.append(name)
            else:
                existing_terms.add(name.lower())
                dictionary.append({
                    "term":  name,
                    "type":  ctype,
                    "code":  code,
                })
                added.append(name)

        # Ghi lại file từ điển
        os.makedirs(os.path.dirname(DICTIONARY_PATH), exist_ok=True)
        with open(DICTIONARY_PATH, "w", encoding="utf-8") as f:
            json.dump(dictionary, f, ensure_ascii=False, indent=2)

        return {
            "added": added,
            "skipped": skipped,
            "total_in_dictionary": len(dictionary),
        }

    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/api/articles")
def get_articles(q: str = ""):
    """Lấy danh sách bài báo, hỗ trợ tìm kiếm theo tiêu đề."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)
        sql = (
            "SELECT id, title, authors, abstract, publication_year, highlighted_html "
            "FROM articles "
        )
        if q:
            cursor.execute(sql + "WHERE title LIKE %s ORDER BY id DESC", (f"%{q}%",))
        else:
            cursor.execute(sql + "ORDER BY id DESC")
        return cursor.fetchall()
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


@router.get("/api/top-concepts")
def get_top_concepts(limit: int = 20, label: str = ""):
    """Lấy các khái niệm xuất hiện nhiều nhất."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)
        if label:
            cursor.execute(
                "SELECT concept_name, concept_type, COUNT(*) as frequency "
                "FROM extracted_concepts WHERE concept_type=%s "
                "GROUP BY concept_name, concept_type ORDER BY frequency DESC LIMIT %s",
                (label, limit),
            )
        else:
            cursor.execute(
                "SELECT concept_name, concept_type, COUNT(*) as frequency "
                "FROM extracted_concepts "
                "GROUP BY concept_name, concept_type ORDER BY frequency DESC LIMIT %s",
                (limit,),
            )
        return cursor.fetchall()
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()


@router.get("/api/status")
def get_status():
    """Trả về trạng thái tiến trình scraping."""
    return scrape_status


@router.post("/api/scrape")
def trigger_scraping(request: ScrapeRequest):
    """Kích hoạt thu thập dữ liệu ở nền."""
    if scrape_status["running"]:
        raise HTTPException(409, "Đang có tác vụ chạy, vui lòng đợi!")
    threading.Thread(
        target=run_scraping,
        args=(request, _db_config, _output_folder),
        daemon=True,
    ).start()
    return {"message": "Đã bắt đầu thu thập ở nền"}


@router.post("/api/scrape/stop")
def stop_scraping_endpoint():
    """Yêu cầu dừng tiến trình thu thập đang chạy nền."""
    stop_scraping()
    return {"message": "Đã gửi lệnh dừng crawler"}
