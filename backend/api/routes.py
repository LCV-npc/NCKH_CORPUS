import threading
import json
import os
import io
import hashlib
import mysql.connector
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
from core.ner_engine import reset_ner_engine, run_ner
from core.ner_dict import (
    DICT_DIR,
    MANIFEST_PATH,
    VERSIONED_CUSTOM_PATH,
    dictionary_metadata,
    normalize_match_text,
    reload_ner_dictionary,
)
from core.scraper import scrape_status, run_scraping, clean_filename, stop_scraping
from core.ai_ner import extract_entities_with_ai
from core.ai_label import extract_with_ai_label

router = APIRouter()

# Đường dẫn file Từ Điển
DICTIONARY_PATH = VERSIONED_CUSTOM_PATH
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
    enable_tone_restore: bool = False
    enable_noun_phrase:  bool = False

class NerRequest(BaseModel):
    text:                str
    threshold:           int  = 100
    enable_tone_restore: bool = False
    enable_noun_phrase:  bool = False

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
            "hash": metadata.file_hash_sha256,
            "validation": metadata.validation_report,
        }
    except Exception as e:
        raise HTTPException(500, f"Lỗi xử lý PDF: {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        os.rmdir(temp_dir)

@router.post("/api/save-highlight")
def save_highlight_endpoint(req: SaveHighlightRequest):
    """Chạy lại NER chuẩn ở server rồi mới lưu, không tin HTML từ trình duyệt."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id, abstract FROM articles WHERE id = %s", (req.article_id,))
        article = cursor.fetchone()
        if not article:
            raise HTTPException(404, f"Không tìm thấy bài báo id={req.article_id}")

        canonical_html, canonical_concepts, _, _ = run_ner(
            article.get("abstract") or "",
            enable_tone_restore=False,
            enable_noun_phrase=False,
        )

        cursor.execute(
            "UPDATE articles SET highlighted_html = %s WHERE id = %s",
            (canonical_html, req.article_id),
        )
        cursor.execute(
            "DELETE FROM extracted_concepts WHERE article_id = %s",
            (req.article_id,),
        )

        seen, rows = set(), []
        for c in canonical_concepts:
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
        return {
            "message": "Lưu thành công bằng luật + từ điển",
            "article_id": req.article_id,
            "concepts_saved": len(rows),
            "dictionary_version": dictionary_metadata.get("dictionary_version"),
        }

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
        if DICTIONARY_PATH.is_file() and DICTIONARY_PATH.stat().st_size > 0:
            with DICTIONARY_PATH.open("r", encoding="utf-8") as f:
                payload = json.load(f)
                return payload.get("entries", payload if isinstance(payload, list) else [])
        return []
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/api/save-to-dictionary")
def save_to_dictionary_endpoint(req: SaveDictionaryRequest):
    """Chỉ thêm bí danh trỏ tới mã đã tồn tại trong ICD-10/YHCT nguồn."""
    try:
        payload = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
        dictionary = payload.get("entries", [])
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        valid_codes = {}
        for source_key in ("icd10", "yhct"):
            source_path = DICT_DIR / manifest["files"][source_key]["path"]
            source_payload = json.loads(source_path.read_text(encoding="utf-8"))
            for entry in source_payload.get("entries", []):
                if not entry.get("active_for_ner") or entry.get("ambiguous"):
                    continue
                code = str(entry.get("code") or "").strip()
                if code:
                    valid_codes[code.casefold()] = (
                        entry.get("canonical_term", ""), entry.get("category", "Bệnh Lý")
                    )

        existing_terms = {normalize_match_text(entry.get("term", "")) for entry in dictionary}
        added, skipped = [], []

        for c in req.matched_concepts:
            name  = (c.get("name") or "").strip()
            code  = (c.get("code") or "").strip()

            if not name:
                continue

            key = normalize_match_text(name)
            canonical = valid_codes.get(code.casefold())
            if not canonical:
                skipped.append({"term": name, "reason": "Mã không tồn tại trong ICD-10/YHCT nguồn"})
            elif key in existing_terms:
                skipped.append(name)
            else:
                existing_terms.add(key)
                dictionary.append({
                    "term": name,
                    "canonical_term": canonical[0],
                    "type": canonical[1],
                    "code": code,
                    "active_for_ner": True,
                    "ambiguous": False,
                    "case_sensitive": name.isupper() and len(name) <= 10,
                    "source": "user_alias",
                })
                added.append(name)

        payload["entries"] = dictionary
        payload["counts"] = {
            "all": len(dictionary),
            "active": sum(bool(item.get("active_for_ner", True)) for item in dictionary),
            "ambiguous": sum(bool(item.get("ambiguous", False)) for item in dictionary),
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        temp_path = DICTIONARY_PATH.with_suffix(".tmp")
        temp_path.write_bytes(encoded)
        os.replace(temp_path, DICTIONARY_PATH)

        manifest["files"]["custom"]["sha256"] = hashlib.sha256(encoded).hexdigest()
        manifest["files"]["custom"]["count"] = len(dictionary)
        manifest_temp = MANIFEST_PATH.with_suffix(".tmp")
        manifest_temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(manifest_temp, MANIFEST_PATH)
        reload_ner_dictionary()
        reset_ner_engine()

        return {
            "added": added,
            "skipped": skipped,
            "total_in_dictionary": len(dictionary),
            "dictionary_version": dictionary_metadata.get("dictionary_version"),
        }

    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/api/articles")
def get_articles(q: str = "", full: bool = False):
    """Lấy danh sách bài báo, hỗ trợ tìm kiếm theo tiêu đề. Mặc định tải siêu nhanh bằng cách chỉ lấy metadata."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)
        if full:
            sql = (
                "SELECT id, title, authors, abstract, publication_year, highlighted_html "
                "FROM articles "
            )
        else:
            sql = (
                "SELECT id, title, authors, publication_year, "
                "(highlighted_html IS NOT NULL AND highlighted_html != '') AS is_labeled "
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


@router.get("/api/articles/{article_id}")
def get_article_detail(article_id: int):
    """Lấy chi tiết 1 bài báo theo ID bao gồm tóm tắt và HTML highlight."""
    conn = cursor = None
    try:
        conn   = _get_conn()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, title, authors, abstract, publication_year, highlighted_html "
            "FROM articles WHERE id = %s",
            (article_id,)
        )
        article = cursor.fetchone()
        if not article:
            raise HTTPException(404, f"Không tìm thấy bài báo id={article_id}")
        cursor.execute(
            "SELECT concept_name AS name, concept_type AS type, concept_code AS code "
            "FROM extracted_concepts WHERE article_id = %s ORDER BY id",
            (article_id,),
        )
        article["matched_concepts"] = cursor.fetchall()
        return article
    except HTTPException:
        raise
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
