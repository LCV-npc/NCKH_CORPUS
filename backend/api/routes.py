import threading
import json
import os
import io
import hashlib
from typing import Any
from urllib.parse import urlparse
import mysql.connector
from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, ConfigDict, Field
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
from core.auth import (
    ROLE_ADMIN,
    ROLE_EXPERT,
    authenticate,
    get_session_user,
    register_expert,
    revoke_session,
)
from core.tamanh_crawler import TamanhCrawlRequest, TamanhCrawlerJobManager

router = APIRouter()

# Đường dẫn file Từ Điển
DICTIONARY_PATH = VERSIONED_CUSTOM_PATH
_db_config: dict = {}
_output_folder: str = ""
tamanh_job_manager = TamanhCrawlerJobManager()

def init_router(db_config: dict, output_folder: str) -> None:
    """Khởi tạo các biến cấu hình cho router."""
    global _db_config, _output_folder
    _db_config = db_config
    _output_folder = output_folder
    tamanh_job_manager.configure_db(db_config)

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

class SaveAiLabelRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    article_id: int = Field(..., alias="articleId")
    labels: dict[str, Any]

class RegisterRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    full_name: str = Field(..., alias="fullName")
    email: str
    password: str
    confirm_password: str = Field(..., alias="confirmPassword")

class LoginRequest(BaseModel):
    email: str
    password: str

class ReviewCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    review_status: str = Field(..., alias="reviewStatus")
    suggested_icd10_code: str | None = Field(None, alias="suggestedIcd10Code")
    comment: str

class SaveHighlightRequest(BaseModel):
    article_id:       int
    highlighted_html: str
    matched_concepts: list

class ScrapeRequest(BaseModel):
    start_year: int
    end_year:   int
    target_url: str

class TamanhCrawlerRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    source_url: str = Field("https://tamanhhospital.vn/", alias="sourceUrl")
    start_year: int | None = Field(None, alias="startYear")
    end_year: int | None = Field(None, alias="endYear")

class SaveDictionaryRequest(BaseModel):
    matched_concepts: list

def _get_conn():
    return mysql.connector.connect(**_db_config)


def _first_ai_icd_label(label_data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return an actually produced ICD code; confidence is not fabricated."""
    for items in label_data.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if code:
                label = str(item.get("label_vn") or item.get("term") or "").strip() or None
                return code, label
    return None, None


def _persist_ai_label(article_id: int, label_data: dict[str, Any], user_id: int) -> dict[str, Any]:
    if not isinstance(label_data, dict) or not label_data:
        raise HTTPException(status_code=422, detail="Không có kết quả AI hợp lệ để lưu.")
    serialized_labels = json.dumps(label_data, ensure_ascii=False, separators=(",", ":"))
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor()
        cursor.execute("SELECT id FROM articles WHERE id = %s", (article_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"Không tìm thấy bài báo id={article_id}")
        cursor.execute(
            "SELECT id FROM ai_document_labels WHERE article_id = %s AND label_payload = %s ORDER BY id DESC LIMIT 1",
            (article_id, serialized_labels),
        )
        duplicate = cursor.fetchone()
        if duplicate:
            return {"saved": False, "duplicate": True, "labelId": int(duplicate[0])}
        code, label = _first_ai_icd_label(label_data)
        cursor.execute(
            """
            INSERT INTO ai_document_labels
            (article_id, model_name, label_payload, primary_icd10_code, primary_icd10_label, generated_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                article_id,
                os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"),
                serialized_labels,
                code,
                label,
                user_id,
            ),
        )
        label_id = cursor.lastrowid
        connection.commit()
        return {"saved": True, "duplicate": False, "labelId": int(label_id)}
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def _current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return get_session_user(_db_config, authorization)


def _require_admin(user: dict[str, Any] = Depends(_current_user)) -> dict[str, Any]:
    if user["role"].upper() != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Chỉ quản trị viên được phép thực hiện thao tác này.")
    return user


def _require_expert(user: dict[str, Any] = Depends(_current_user)) -> dict[str, Any]:
    if user["role"].upper() != ROLE_EXPERT:
        raise HTTPException(status_code=403, detail="Chức năng này dành cho chuyên gia.")
    return user


@router.post("/api/auth/login")
def login_endpoint(req: LoginRequest):
    try:
        user, token, expires_at = authenticate(_db_config, req.email, req.password)
        return {"user": user, "token": token, "expiresAt": expires_at}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/api/auth/me")
def auth_me_endpoint(user: dict[str, Any] = Depends(_current_user)):
    return {"user": user}


@router.post("/api/auth/logout")
def logout_endpoint(
    authorization: str | None = Header(default=None),
    user: dict[str, Any] = Depends(_current_user),
):
    revoke_session(_db_config, authorization)
    return {"message": "Đã đăng xuất."}

@router.post("/api/highlight-text")
def highlight_text_endpoint(req: HighlightRequest, _: dict[str, Any] = Depends(_require_admin)):
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
def ner_endpoint(req: NerRequest, _: dict[str, Any] = Depends(_require_admin)):
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
def ai_ner_endpoint(req: NerRequest, _: dict[str, Any] = Depends(_require_admin)):
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
def ai_label_endpoint(req: AiLabelRequest, _: dict[str, Any] = Depends(_require_admin)):
    """Endpoint dùng Gemini để gán nhãn thực thể y khoa (6 nhóm)."""
    try:
        result = extract_with_ai_label(req.text)
        return result
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as e:
        raise HTTPException(500, f"Lỗi AI Gán Nhãn: {str(e)}")


@router.post("/api/ai-label/save")
def save_ai_label_endpoint(req: SaveAiLabelRequest, user: dict[str, Any] = Depends(_require_admin)):
    """Persist the exact AI result only after the Admin explicitly confirms it."""
    result = _persist_ai_label(req.article_id, req.labels, int(user["id"]))
    message = "Kết quả AI đã được lưu vào cơ sở dữ liệu." if result["saved"] else "Kết quả AI này đã được lưu trước đó."
    return {"message": message, **result}





# ── Endpoint mới: Tách PDF & Nhật ký ──────────────────────────────────

@router.get("/api/crawl-logs")
def get_crawl_logs(_: dict[str, Any] = Depends(_require_admin)):
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
def verify_data(_: dict[str, Any] = Depends(_require_admin)):
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
from core.pdf_pipeline import ExtractorPipeline, PipelineError

@router.post("/api/extract-pdf")
async def extract_pdf_endpoint(file: UploadFile = File(...), relative_path: str = Form(""), _: dict[str, Any] = Depends(_require_admin)):
    """Extract title/authors/abstract and article sections from one PDF."""
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
        metadata = pipeline.run(
            temp_path,
            source=relative_path or "upload",
            require_vietnamese=True,
            use_llm=True,
        )
        
        return {
            "message": "Đã bỏ qua PDF trùng" if metadata.is_duplicate else "Tách PDF thành công",
            "duplicate": metadata.is_duplicate,
            "duplicateOf": metadata.duplicate_of,
            "files_created": metadata.extracted_files,
            "hash": metadata.file_hash_sha256,
            "validation": metadata.validation_report,
            "languageDecision": metadata.language_decision,
            "extraction": metadata.extraction,
            "article": {
                "title": metadata.title,
                "authors": metadata.authors,
                "abstract": metadata.abstract,
                "keywords": metadata.keywords,
                "affiliations": metadata.affiliations,
                "sections": len(metadata.sections),
                "pageCount": metadata.page_count,
            },
            "outputDirectory": metadata.output_directory,
            "metadataFile": metadata.metadata_file,
            "structuredDocumentFile": metadata.structured_document_file,
        }
    except PipelineError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Lỗi xử lý PDF: {str(e)}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        os.rmdir(temp_dir)

@router.post("/api/save-highlight")
def save_highlight_endpoint(req: SaveHighlightRequest, _: dict[str, Any] = Depends(_require_admin)):
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
def get_dictionary(_: dict[str, Any] = Depends(_require_admin)):
    """Trả về toàn bộ từ điển y khoa."""
    try:
        if DICTIONARY_PATH.is_file() and DICTIONARY_PATH.stat().st_size > 0:
            with DICTIONARY_PATH.open("r", encoding="utf-8") as f:
                payload = json.load(f)
                return payload.get("entries", payload if isinstance(payload, list) else [])
        return []
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/api/dictionary/status")
def get_dictionary_status(_: dict[str, Any] = Depends(_require_admin)):
    """Return the validated sources and current coverage of rule-based NER."""
    try:
        # Import-time loading verifies artifact and original-source hashes.
        return dictionary_metadata
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/api/save-to-dictionary")
def save_to_dictionary_endpoint(req: SaveDictionaryRequest, _: dict[str, Any] = Depends(_require_admin)):
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
def get_articles(q: str = "", full: bool = False, _: dict[str, Any] = Depends(_require_admin)):
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
def get_article_detail(article_id: int, _: dict[str, Any] = Depends(_require_admin)):
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


_icd_code_cache: dict[str, str] | None = None


def _icd_code_catalog() -> dict[str, str]:
    """Build a validated code-to-label index from the same dictionaries as NER."""
    global _icd_code_cache
    if _icd_code_cache is not None:
        return _icd_code_cache
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    catalog: dict[str, str] = {}
    for source_key in ("icd10", "yhct"):
        path = DICT_DIR / manifest["files"][source_key]["path"]
        for item in json.loads(path.read_text(encoding="utf-8")).get("entries", []):
            code = str(item.get("code") or "").strip()
            if code and item.get("active_for_ner") and not item.get("ambiguous"):
                catalog[code.casefold()] = str(item.get("canonical_term") or "").strip()
    _icd_code_cache = catalog
    return catalog


def _latest_ai_label(cursor, document_id: int) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT id, model_name, label_payload, primary_icd10_code, primary_icd10_label,
               confidence, created_at
        FROM ai_document_labels WHERE article_id = %s ORDER BY id DESC LIMIT 1
        """,
        (document_id,),
    )
    row = cursor.fetchone()
    if row:
        try:
            row["labels"] = json.loads(row.pop("label_payload"))
        except (KeyError, TypeError, json.JSONDecodeError):
            row["labels"] = {}
    return row


def _document_labels(cursor, document_id: int) -> list[dict[str, str]]:
    cursor.execute(
        """
        SELECT concept_name, concept_type, concept_code
        FROM extracted_concepts
        WHERE article_id = %s AND concept_code <> ''
        ORDER BY id
        """,
        (document_id,),
    )
    labels = [
        {
            "source": "ICD-10 dictionary",
            "code": row["concept_code"],
            "label": row["concept_name"],
            "type": row["concept_type"],
        }
        for row in cursor.fetchall()
    ]
    ai_label = _latest_ai_label(cursor, document_id)
    if ai_label:
        labels.append({
            "source": "AI",
            "code": ai_label.get("primary_icd10_code") or "",
            "label": ai_label.get("primary_icd10_label") or "",
            "type": "AI_LABEL",
        })
    return labels


def _assert_expert_document_access(cursor, document_id: int) -> None:
    cursor.execute("SELECT id FROM articles WHERE id = %s", (document_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy văn bản id={document_id}")
    cursor.execute(
        """
        SELECT EXISTS(SELECT 1 FROM extracted_concepts WHERE article_id = %s AND concept_code <> '')
               OR EXISTS(SELECT 1 FROM ai_document_labels WHERE article_id = %s) AS allowed
        """,
        (document_id, document_id),
    )
    if not cursor.fetchone()["allowed"]:
        raise HTTPException(status_code=403, detail="Chuyên gia chỉ được xem văn bản đã gán ICD-10 hoặc AI.")


def _review_history(cursor, document_id: int, expert_id: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT r.id, r.review_status, r.original_labels_json, r.suggested_icd10_code,
               r.suggested_icd10_label, r.comment, r.created_at, r.updated_at,
               u.id AS expert_id, u.full_name AS expert_name, u.email AS expert_email
        FROM expert_reviews r JOIN users u ON u.id = r.expert_id
        WHERE r.document_id = %s
    """
    params: list[Any] = [document_id]
    if expert_id is not None:
        sql += " AND r.expert_id = %s"
        params.append(expert_id)
    sql += " ORDER BY r.id DESC"
    cursor.execute(sql, tuple(params))
    rows = cursor.fetchall()
    for row in rows:
        try:
            row["original_labels"] = json.loads(row.pop("original_labels_json"))
        except (KeyError, TypeError, json.JSONDecodeError):
            row["original_labels"] = []
    return rows


@router.get("/api/expert/dashboard")
def expert_dashboard(user: dict[str, Any] = Depends(_require_expert)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT COUNT(DISTINCT article_id) AS value FROM extracted_concepts WHERE concept_code <> ''")
        icd_count = cursor.fetchone()["value"]
        cursor.execute("SELECT COUNT(DISTINCT article_id) AS value FROM ai_document_labels")
        ai_count = cursor.fetchone()["value"]
        cursor.execute("SELECT COUNT(DISTINCT document_id) AS value FROM expert_reviews WHERE expert_id = %s", (user["id"],))
        reviewed = cursor.fetchone()["value"]
        cursor.execute(
            """
            SELECT COUNT(*) AS value FROM (
                SELECT DISTINCT article_id AS document_id FROM extracted_concepts WHERE concept_code <> ''
                UNION
                SELECT DISTINCT article_id AS document_id FROM ai_document_labels
            ) eligible
            LEFT JOIN (
                SELECT DISTINCT document_id FROM expert_reviews WHERE expert_id = %s
            ) own_review ON own_review.document_id = eligible.document_id
            WHERE own_review.document_id IS NULL
            """,
            (user["id"],),
        )
        pending = cursor.fetchone()["value"]
        return {
            "icd10LabeledDocuments": icd_count,
            "aiLabeledDocuments": ai_count,
            "reviewed": reviewed,
            "pendingReview": pending,
        }
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


def _paginate(rows: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    safe_page = max(1, int(page))
    safe_size = min(100, max(1, int(page_size)))
    start = (safe_page - 1) * safe_size
    return {"items": rows[start:start + safe_size], "page": safe_page, "pageSize": safe_size, "total": len(rows)}


@router.get("/api/expert/documents/icd10")
def expert_icd_documents(
    q: str = "", icd: str = "", review_status: str = "", page: int = 1, page_size: int = 20,
    user: dict[str, Any] = Depends(_require_expert),
):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        query = """
            SELECT a.id, a.title, a.authors, a.publication_year,
                   GROUP_CONCAT(DISTINCT ec.concept_code ORDER BY ec.concept_code SEPARATOR ', ') AS icd10_codes,
                   GROUP_CONCAT(DISTINCT ec.concept_name ORDER BY ec.concept_name SEPARATOR ' | ') AS icd10_labels,
                   latest.review_status AS latest_review_status
            FROM articles a
            JOIN extracted_concepts ec ON ec.article_id = a.id AND ec.concept_code <> ''
            LEFT JOIN expert_reviews latest ON latest.id = (
                SELECT MAX(r.id) FROM expert_reviews r
                WHERE r.document_id = a.id AND r.expert_id = %s
            )
            WHERE (%s = '' OR a.title LIKE %s OR a.authors LIKE %s)
              AND (%s = '' OR ec.concept_code LIKE %s)
            GROUP BY a.id, a.title, a.authors, a.publication_year, latest.review_status, latest.id
            ORDER BY a.id DESC
        """
        params = (user["id"], q, f"%{q}%", f"%{q}%", icd, f"%{icd}%")
        cursor.execute(query, params)
        rows = cursor.fetchall()
        status = review_status.casefold()
        if status == "pending":
            rows = [row for row in rows if not row["latest_review_status"]]
        elif status == "reviewed":
            rows = [row for row in rows if row["latest_review_status"]]
        for row in rows:
            row["reviewStatus"] = "Reviewed" if row.pop("latest_review_status") else "Pending"
        return _paginate(rows, page, page_size)
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/expert/documents/ai-labeled")
def expert_ai_documents(
    q: str = "", review_status: str = "", page: int = 1, page_size: int = 20,
    user: dict[str, Any] = Depends(_require_expert),
):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT a.id, a.title, a.authors, a.publication_year,
                   ai.primary_icd10_code, ai.primary_icd10_label, ai.confidence, ai.created_at AS ai_labeled_at,
                   latest.review_status AS latest_review_status
            FROM articles a
            JOIN ai_document_labels ai ON ai.id = (
                SELECT MAX(ai2.id) FROM ai_document_labels ai2 WHERE ai2.article_id = a.id
            )
            LEFT JOIN expert_reviews latest ON latest.id = (
                SELECT MAX(r.id) FROM expert_reviews r
                WHERE r.document_id = a.id AND r.expert_id = %s
            )
            WHERE (%s = '' OR a.title LIKE %s OR a.authors LIKE %s)
            ORDER BY a.id DESC
            """,
            (user["id"], q, f"%{q}%", f"%{q}%"),
        )
        rows = cursor.fetchall()
        status = review_status.casefold()
        if status == "pending":
            rows = [row for row in rows if not row["latest_review_status"]]
        elif status == "reviewed":
            rows = [row for row in rows if row["latest_review_status"]]
        for row in rows:
            row["reviewStatus"] = "Reviewed" if row.pop("latest_review_status") else "Pending"
        return _paginate(rows, page, page_size)
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/expert/documents/reviewed")
def expert_reviewed_documents(
    q: str = "", page: int = 1, page_size: int = 20, user: dict[str, Any] = Depends(_require_expert),
):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT a.id, a.title, r.review_status, r.suggested_icd10_code, r.suggested_icd10_label,
                   r.comment, r.created_at AS reviewed_at
            FROM articles a JOIN expert_reviews r ON r.id = (
                SELECT MAX(r2.id) FROM expert_reviews r2
                WHERE r2.document_id = a.id AND r2.expert_id = %s
            )
            WHERE (%s = '' OR a.title LIKE %s)
            ORDER BY r.created_at DESC
            """,
            (user["id"], q, f"%{q}%"),
        )
        return _paginate(cursor.fetchall(), page, page_size)
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


def _expert_document_detail(document_id: int, user: dict[str, Any]) -> dict[str, Any]:
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        _assert_expert_document_access(cursor, document_id)
        cursor.execute(
            "SELECT id, title, authors, abstract, publication_year, source_url FROM articles WHERE id = %s",
            (document_id,),
        )
        article = cursor.fetchone()
        article["currentLabels"] = _document_labels(cursor, document_id)
        article["aiLabel"] = _latest_ai_label(cursor, document_id)
        article["reviewHistory"] = _review_history(cursor, document_id, int(user["id"]))
        return article
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/expert/documents/{document_id}")
def expert_document_detail(document_id: int, user: dict[str, Any] = Depends(_require_expert)):
    return _expert_document_detail(document_id, user)


@router.get("/api/expert/documents/{document_id}/reviews")
def expert_document_reviews(document_id: int, user: dict[str, Any] = Depends(_require_expert)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        _assert_expert_document_access(cursor, document_id)
        return {"items": _review_history(cursor, document_id, int(user["id"]))}
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.post("/api/expert/documents/{document_id}/reviews", status_code=201)
def save_expert_review(
    document_id: int, req: ReviewCreateRequest, user: dict[str, Any] = Depends(_require_expert),
):
    status = str(req.review_status or "").strip().upper()
    if status not in {"CORRECT", "INCORRECT", "NEEDS_REVISION"}:
        raise HTTPException(status_code=422, detail="Trạng thái review không hợp lệ.")
    comment = str(req.comment or "").strip()
    if not 3 <= len(comment) <= 8000:
        raise HTTPException(status_code=422, detail="Nhận xét cần từ 3 đến 8000 ký tự.")
    suggested_code = str(req.suggested_icd10_code or "").strip()
    if status in {"INCORRECT", "NEEDS_REVISION"} and not suggested_code:
        raise HTTPException(status_code=422, detail="Cần chọn mã ICD-10 đề xuất cho trạng thái này.")
    suggested_label = None
    if suggested_code:
        suggested_label = _icd_code_catalog().get(suggested_code.casefold())
        if not suggested_label:
            raise HTTPException(status_code=422, detail="Mã ICD-10/YHCT đề xuất không tồn tại trong từ điển đã xác thực.")

    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        _assert_expert_document_access(cursor, document_id)
        original_labels = _document_labels(cursor, document_id)
        cursor.execute(
            """
            INSERT INTO expert_reviews
            (document_id, expert_id, review_status, original_labels_json,
             suggested_icd10_code, suggested_icd10_label, comment)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                document_id, user["id"], status, json.dumps(original_labels, ensure_ascii=False),
                suggested_code or None, suggested_label, comment,
            ),
        )
        review_id = cursor.lastrowid
        connection.commit()
        return {"message": "Review saved successfully.", "reviewId": int(review_id)}
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/admin/reviews")
def admin_reviews(q: str = "", page: int = 1, page_size: int = 30, _: dict[str, Any] = Depends(_require_admin)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT r.id, r.document_id, a.title AS document_title, u.full_name AS expert_name,
                   r.review_status, r.original_labels_json, r.suggested_icd10_code,
                   r.suggested_icd10_label, r.comment, r.created_at, r.updated_at
            FROM expert_reviews r
            JOIN articles a ON a.id = r.document_id
            JOIN users u ON u.id = r.expert_id
            WHERE (%s = '' OR a.title LIKE %s OR u.full_name LIKE %s)
            ORDER BY r.id DESC
            """,
            (q, f"%{q}%", f"%{q}%"),
        )
        rows = cursor.fetchall()
        for row in rows:
            try:
                row["original_labels"] = json.loads(row.pop("original_labels_json"))
            except (KeyError, TypeError, json.JSONDecodeError):
                row["original_labels"] = []
        return _paginate(rows, page, page_size)
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/admin/users")
def admin_users(page: int = 1, page_size: int = 30, _: dict[str, Any] = Depends(_require_admin)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT u.id, u.full_name AS name, u.email, LOWER(u.role) AS role,
                   u.is_active, u.created_at, COUNT(r.id) AS review_count
            FROM users u LEFT JOIN expert_reviews r ON r.expert_id = u.id
            GROUP BY u.id, u.full_name, u.email, u.role, u.is_active, u.created_at
            ORDER BY u.created_at DESC
            """
        )
        return _paginate(cursor.fetchall(), page, page_size)
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.post("/api/admin/users", status_code=201)
def create_expert_account(req: RegisterRequest, _: dict[str, Any] = Depends(_require_admin)):
    """Create an Expert account from the authenticated Admin workspace."""
    if req.password != req.confirm_password:
        raise HTTPException(status_code=422, detail="Xác nhận mật khẩu không khớp.")
    try:
        user = register_expert(_db_config, req.full_name, req.email, req.password)
        return {"message": "Đã tạo tài khoản chuyên gia thành công.", "user": user}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/api/admin/documents/{document_id}")
def admin_document_detail(document_id: int, _: dict[str, Any] = Depends(_require_admin)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, title, authors, abstract, publication_year, source_url FROM articles WHERE id = %s",
            (document_id,),
        )
        article = cursor.fetchone()
        if not article:
            raise HTTPException(status_code=404, detail=f"Không tìm thấy văn bản id={document_id}")
        article["currentLabels"] = _document_labels(cursor, document_id)
        article["aiLabel"] = _latest_ai_label(cursor, document_id)
        article["reviewHistory"] = _review_history(cursor, document_id)
        return article
    finally:
        if cursor: cursor.close()
        if connection: connection.close()



@router.get("/api/top-concepts")
def get_top_concepts(limit: int = 20, label: str = "", _: dict[str, Any] = Depends(_require_admin)):
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


@router.get("/api/health")
def get_health():
    """Lightweight connectivity probe, independent of crawler state/DB work."""
    return {"ok": True}


@router.get("/api/status")
def get_status():
    """Trả về trạng thái tiến trình scraping."""
    return scrape_status


@router.post("/api/scrape")
def trigger_scraping(request: ScrapeRequest, _: dict[str, Any] = Depends(_require_admin)):
    """Kích hoạt thu thập dữ liệu ở nền."""
    parsed_target = urlparse(request.target_url.strip())
    if parsed_target.scheme not in {"http", "https"} or not parsed_target.netloc:
        raise HTTPException(400, "URL crawler phải là địa chỉ HTTP/HTTPS đầy đủ.")
    if request.start_year > request.end_year:
        raise HTTPException(400, "Năm bắt đầu không được lớn hơn năm kết thúc.")
    if scrape_status["running"]:
        raise HTTPException(409, "Đang có tác vụ chạy, vui lòng đợi!")
    threading.Thread(
        target=run_scraping,
        args=(request, _db_config, _output_folder),
        daemon=True,
    ).start()
    return {"message": "Đã bắt đầu thu thập ở nền"}


@router.post("/api/scrape/stop")
def stop_scraping_endpoint(_: dict[str, Any] = Depends(_require_admin)):
    """Yêu cầu dừng tiến trình thu thập đang chạy nền."""
    stop_scraping()
    return {"message": "Đã gửi lệnh dừng crawler"}


# ── Tâm Anh Medical Q&A Crawler ────────────────────────────────────────

@router.post("/api/crawler/tamanh/start")
def start_tamanh_crawler(request: TamanhCrawlerRequest, _: dict[str, Any] = Depends(_require_admin)):
    """Start a background collection job for public Tâm Anh Q&A pages."""
    if (
        request.start_year is not None
        and request.end_year is not None
        and request.start_year > request.end_year
    ):
        raise HTTPException(400, "Năm bắt đầu không được lớn hơn năm kết thúc.")
    try:
        job = tamanh_job_manager.start(TamanhCrawlRequest(
            source_url=request.source_url,
            start_year=request.start_year,
            end_year=request.end_year,
        ))
        return {
            "success": True,
            "message": "Crawler started",
            "jobId": job.job_id,
            "status": job.status,
        }
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@router.get("/api/crawler/tamanh/status/{job_id}")
def get_tamanh_crawler_status(job_id: str, _: dict[str, Any] = Depends(_require_admin)):
    job = tamanh_job_manager.get(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy crawler job.")
    return job.public()


@router.post("/api/crawler/tamanh/stop/{job_id}")
def stop_tamanh_crawler(job_id: str, _: dict[str, Any] = Depends(_require_admin)):
    job = tamanh_job_manager.stop(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy crawler job.")
    return {"success": True, "message": "Đã gửi lệnh dừng crawler", "jobId": job.job_id}
