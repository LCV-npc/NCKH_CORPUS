import threading
import json
import os
import io
import hashlib
import smtplib
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime
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
from core.xlmr_ner import XLMRInferenceError, xlmr_ner
from core.auth import (
    ROLE_ADMIN,
    ROLE_EXPERT,
    ROLE_REVIEWER,
    authenticate,
    get_session_user,
    register_expert,
    register_reviewer,
    revoke_session,
)
from core.tamanh_crawler import TamanhCrawlRequest, TamanhCrawlerJobManager
from core.ner_experiment import (
    LABELS as EXPERIMENT_LABELS,
    TEXT_ROOT,
    entities_to_bio,
    bio_text,
    ner_experiment_manager,
    normalize_entity,
    resolve_overlaps,
)

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
    ner_experiment_manager.configure_db(db_config)

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


class XLMRNERRequest(BaseModel):
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
    label_source: str = Field("icd10", alias="labelSource")  # 'icd10' | 'ai'
    annotations: list[dict[str, Any]] = Field(default_factory=list)
    suggested_icd10_code: str | None = Field(None, alias="suggestedIcd10Code")
    comment: str

class ReviewerAdjudicationRequest(BaseModel):
    decisions: list[dict[str, Any]]
    final_labels: list[dict[str, Any]] = Field(default_factory=list, alias="finalLabels")
    resolution_status: str = Field("RESOLVED", alias="resolutionStatus")
    note: str = ""

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


class NerExperimentCreateRequest(BaseModel):
    limit: int = Field(100, ge=1, le=100)
    note: str = ""
    parent_run_id: str | None = Field(None, alias="parentRunId")


class GoldAnnotationRequest(BaseModel):
    entities: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""

class LabelingSessionRequest(BaseModel):
    name: str
    description: str = ""
    article_ids: list[int] = Field(default_factory=list, alias="articleIds")
    expert_ids: list[int] = Field(default_factory=list, alias="expertIds")
    reviewer_id: int = Field(..., alias="reviewerId")
    send_email: bool = Field(True, alias="sendEmail")

    model_config = ConfigDict(populate_by_name=True)

def _get_conn():
    return mysql.connector.connect(**_db_config)

def _send_assignment_email(recipients: list[dict[str, Any]], session_name: str, article_count: int) -> list[str]:
    host = os.getenv("SMTP_HOST")
    if not host:
        return [str(item["email"]) for item in recipients]
    sent = []
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", username)
    message_template = (
        f"Hello {{name}},\n\nYou have been assigned to labeling session '{session_name}'.\n"
        f"Documents assigned: {article_count}. Please sign in to MedNLP Studio to begin.\n\n"
        "Best regards,\nMedNLP Studio"
    )
    with smtplib.SMTP(host, port, timeout=20) as server:
        if os.getenv("SMTP_TLS", "1") != "0":
            server.starttls()
        if username:
            server.login(username, password)
        for recipient in recipients:
            msg = EmailMessage()
            msg["Subject"] = f"MedNLP labeling assignment: {session_name}"
            msg["From"] = sender
            msg["To"] = recipient["email"]
            msg.set_content(message_template.format(name=recipient["name"]))
            server.send_message(msg)
            sent.append(recipient["email"])
    return sent


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
        metadata = label_data.get("_metadata") if isinstance(label_data.get("_metadata"), dict) else {}
        model_name = str(metadata.get("model_name") or os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"))
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
                model_name,
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


def _require_reviewer(user: dict[str, Any] = Depends(_current_user)) -> dict[str, Any]:
    if user["role"].upper() != ROLE_REVIEWER:
        raise HTTPException(status_code=403, detail="Chức năng này dành cho Reviewer.")
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


@router.post("/api/xlmr-ner")
def xlmr_ner_endpoint(req: XLMRNERRequest, _: dict[str, Any] = Depends(_require_admin)):
    """Run the locally fine-tuned XLM-R token-classification checkpoint."""
    try:
        return xlmr_ner.predict(req.text)
    except XLMRInferenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Lỗi XLM-R NER: {exc}") from exc

@router.get("/api/admin/labeling-options")
def labeling_options(_: dict[str, Any] = Depends(_require_admin)):
    connection = cursor = None
    try:
        connection = _get_conn(); cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT a.id, a.title, a.authors, a.publication_year, COUNT(DISTINCT lsa.session_id) AS assigned_sessions FROM articles a LEFT JOIN labeling_session_assignments lsa ON lsa.article_id = a.id GROUP BY a.id, a.title, a.authors, a.publication_year ORDER BY a.id DESC LIMIT 1000")
        articles = cursor.fetchall()
        cursor.execute("SELECT id, full_name AS name, email FROM users WHERE role = 'EXPERT' AND is_active = 1 ORDER BY full_name")
        experts = cursor.fetchall()
        cursor.execute("SELECT id, full_name AS name, email FROM users WHERE role = 'REVIEWER' AND is_active = 1 ORDER BY full_name")
        reviewers = cursor.fetchall()
        return {"articles": articles, "experts": experts, "reviewers": reviewers}
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

@router.post("/api/admin/labeling-sessions", status_code=201)
def create_labeling_session(req: LabelingSessionRequest, user: dict[str, Any] = Depends(_require_admin)):
    if not req.name.strip() or not req.article_ids or len(req.expert_ids) != 2:
        raise HTTPException(422, "A session name, at least one document, and exactly two experts are required.")
    if req.expert_ids[0] == req.expert_ids[1]:
        raise HTTPException(422, "Select two different experts.")
    connection = cursor = None
    try:
        connection = _get_conn(); cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, full_name AS name, email FROM users WHERE id IN (%s,%s) AND role='EXPERT' AND is_active=1", tuple(req.expert_ids))
        experts = cursor.fetchall()
        if len(experts) != 2: raise HTTPException(422, "One or more selected experts are invalid.")
        cursor.execute("SELECT id FROM users WHERE id=%s AND role='REVIEWER' AND is_active=1", (req.reviewer_id,))
        if not cursor.fetchone(): raise HTTPException(422, "Selected reviewer is invalid.")
        cursor.execute("INSERT INTO labeling_sessions (name, description, reviewer_id, created_by) VALUES (%s,%s,%s,%s)", (req.name.strip(), req.description.strip(), req.reviewer_id, user["id"]))
        session_id = cursor.lastrowid
        rows = [(session_id, article_id, expert_id) for article_id in sorted(set(req.article_ids)) for expert_id in req.expert_ids]
        cursor.executemany("INSERT INTO labeling_session_assignments (session_id, article_id, expert_id) VALUES (%s,%s,%s)", rows)
        connection.commit()
        if req.send_email:
            try:
                emailed = _send_assignment_email(experts, req.name.strip(), len(set(req.article_ids)))
                email_status = "sent" if emailed else "not_configured"
            except Exception as email_error:
                email_status = f"failed: {email_error}"
        else:
            email_status = "disabled"
        return {"sessionId": int(session_id), "emailStatus": email_status, "assignedExperts": len(experts)}
    except HTTPException:
        if connection: connection.rollback()
        raise
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


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


# ── NER experiment runner and gold workflow ──────────────────────────

@router.post("/api/ner-experiments")
def create_ner_experiment(req: NerExperimentCreateRequest, _: dict[str, Any] = Depends(_require_admin)):
    try:
        run = ner_experiment_manager.start(req.limit, req.note.strip(), req.parent_run_id)
        return run.public()
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@router.get("/api/ner-experiments")
def list_ner_experiments(_: dict[str, Any] = Depends(_current_user)):
    return {"runs": ner_experiment_manager.list(), "labels": list(EXPERIMENT_LABELS)}


@router.get("/api/ner-experiments/{run_id}")
def get_ner_experiment(run_id: str, _: dict[str, Any] = Depends(_current_user)):
    run = ner_experiment_manager.get(run_id)
    if not run:
        raise HTTPException(404, "Không tìm thấy run.")
    payload = run.public()
    for filename, key in (("metrics.json", "metrics"), ("fatal_errors.jsonl", "fatalErrors")):
        path = run.directory / filename
        if path.exists():
            if path.suffix == ".json":
                payload[key] = json.loads(path.read_text(encoding="utf-8"))
            else:
                payload[key] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return payload


@router.get("/api/ner-experiments/{run_id}/errors")
def get_ner_experiment_errors(run_id: str, _: dict[str, Any] = Depends(_current_user)):
    run = ner_experiment_manager.get(run_id)
    if not run:
        raise HTTPException(404, "Không tìm thấy run.")
    rows: list[dict[str, Any]] = []
    for system in ("current_ai", "vietbioner", "vimedner"):
        for filename in ("conversion_issues.jsonl",):
            path = run.directory / system / filename
            if path.exists():
                rows.extend({"system": system, **json.loads(line)} for line in path.read_text(encoding="utf-8").splitlines() if line)
    return {"errors": rows[:2000], "total": len(rows)}


@router.post("/api/ner-experiments/{run_id}/stop")
def stop_ner_experiment(run_id: str, _: dict[str, Any] = Depends(_require_admin)):
    run = ner_experiment_manager.stop(run_id)
    if not run:
        raise HTTPException(404, "Không tìm thấy run.")
    return run.public()


@router.post("/api/ner-experiments/{run_id}/resume")
def resume_ner_experiment(run_id: str, _: dict[str, Any] = Depends(_require_admin)):
    try:
        run = ner_experiment_manager.resume(run_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    if not run:
        raise HTTPException(404, "Không tìm thấy run.")
    return run.public()


@router.post("/api/ner-experiments/{run_id}/promote")
def promote_ner_experiment(run_id: str, _: dict[str, Any] = Depends(_require_admin)):
    try:
        run = ner_experiment_manager.promote(run_id)
        return {"message": "Đã chọn checkpoint làm bản hiện hành.", **run.public()}
    except KeyError:
        raise HTTPException(404, "Không tìm thấy run.")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


def _gold_article(article_id: int) -> dict[str, Any]:
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, title, abstract FROM articles WHERE id=%s", (article_id,))
        article = cursor.fetchone()
        if not article or not article.get("abstract"):
            raise HTTPException(404, "Không tìm thấy bài báo.")
        return {"article_id": int(article["id"]), "title": article.get("title") or "", "abstract": article["abstract"]}
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@router.get("/api/ner-gold/articles/{article_id}")
def get_gold_article(article_id: int, _: dict[str, Any] = Depends(_current_user)):
    if not 1 <= article_id <= 100:
        raise HTTPException(422, "Gold thử nghiệm chỉ gồm article_id 1..100.")
    return {**_gold_article(article_id), "labels": list(EXPERIMENT_LABELS)}


@router.post("/api/ner-gold/articles/{article_id}")
def save_gold_draft(article_id: int, req: GoldAnnotationRequest, user: dict[str, Any] = Depends(_require_expert)):
    article = _gold_article(article_id)
    normalized = []
    for entity in req.entities:
        item = normalize_entity(article["abstract"], entity)
        if item is None:
            raise HTTPException(422, f"Entity không hợp lệ hoặc không khớp nguyên văn: {entity}")
        normalized.append(item)
    normalized, conflicts = resolve_overlaps(normalized)
    if conflicts:
        raise HTTPException(422, "Gold không cho phép entity trùng hoặc chồng lấn.")
    path = TEXT_ROOT / "gold" / "drafts" / f"expert_{int(user['id'])}" / f"{article_id:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**article, "entities": normalized, "note": req.note, "expert_id": int(user["id"]), "saved_at": datetime.utcnow().isoformat() + "Z"}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"saved": True, "articleId": article_id, "entityCount": len(normalized)}


@router.get("/api/ner-gold/review")
def list_gold_drafts(_: dict[str, Any] = Depends(_require_reviewer)):
    rows = []
    for path in (TEXT_ROOT / "gold" / "drafts").glob("expert_*/*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.append({"articleId": payload["article_id"], "expertId": payload["expert_id"], "entityCount": len(payload["entities"]), "savedAt": payload["saved_at"]})
    return {"drafts": sorted(rows, key=lambda row: (row["articleId"], row["expertId"]))}


@router.post("/api/ner-gold/review/{article_id}/finalize")
def finalize_gold(article_id: int, req: GoldAnnotationRequest, user: dict[str, Any] = Depends(_require_reviewer)):
    article = _gold_article(article_id)
    normalized = []
    for entity in req.entities:
        item = normalize_entity(article["abstract"], entity)
        if item is None:
            raise HTTPException(422, f"Entity không hợp lệ: {entity}")
        normalized.append(item)
    normalized, conflicts = resolve_overlaps(normalized)
    if conflicts:
        raise HTTPException(422, "Gold không cho phép entity trùng hoặc chồng lấn.")
    final_dir = TEXT_ROOT / "gold" / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    payload = {**article, "entities": normalized, "note": req.note, "reviewer_id": int(user["id"]), "finalized_at": datetime.utcnow().isoformat() + "Z"}
    (final_dir / f"{article_id:04d}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _export_gold_files(final_dir)
    return {"finalized": True, "articleId": article_id, "entityCount": len(normalized)}


def _export_gold_files(final_dir: Path) -> None:
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(final_dir.glob("*.json"))]
    gold_dir = TEXT_ROOT / "gold"
    jsonl = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    (gold_dir / "gold.jsonl").write_text(jsonl, encoding="utf-8")
    conll = []
    for row in rows:
        tokens, issues = entities_to_bio(row["abstract"], row["entities"])
        if issues:
            raise HTTPException(422, f"Gold article {row['article_id']} không khớp token boundary.")
        conll.append(bio_text(tokens))
    (gold_dir / "test.txt").write_text("".join(conll), encoding="utf-8")
    digest = hashlib.sha256(jsonl.encode("utf-8")).hexdigest()
    (gold_dir / "manifest.json").write_text(json.dumps({"articles": len(rows), "checksum": digest, "updated_at": datetime.utcnow().isoformat() + "Z"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")





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
            identity = (
                name.casefold(),
                int(c.get("start", -1)),
                int(c.get("end", -1)),
                ctype.casefold(),
                code.casefold(),
            )
            if name and identity not in seen:
                seen.add(identity)
                rows.append((req.article_id, name, ctype, code, c.get("start"), c.get("end")))

        if rows:
            cursor.executemany(
                "INSERT INTO extracted_concepts "
                "(article_id, concept_name, concept_type, concept_code, concept_start, concept_end) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
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
                "SELECT a.id, a.title, a.authors, a.publication_year, "
                "(a.highlighted_html IS NOT NULL AND a.highlighted_html != '') AS is_labeled, "
                "EXISTS(SELECT 1 FROM ai_document_labels ai WHERE ai.article_id = a.id) AS is_ai_labeled "
                "FROM articles a "
            )
        if q:
            cursor.execute(sql + "WHERE a.title LIKE %s ORDER BY a.id DESC", (f"%{q}%",))
        else:
            cursor.execute(sql + "ORDER BY a.id DESC")
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


def _all_ai_labels(cursor, document_id: int) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, model_name, label_payload, primary_icd10_code, primary_icd10_label,
               confidence, created_at
        FROM ai_document_labels WHERE article_id = %s ORDER BY id DESC
        """,
        (document_id,),
    )
    rows = cursor.fetchall()
    for row in rows:
        try:
            row["labels"] = json.loads(row.pop("label_payload"))
        except (KeyError, TypeError, json.JSONDecodeError):
            row["labels"] = {}
    return rows


def _document_labels(cursor, document_id: int) -> list[dict[str, str]]:
    """Return only dictionary labels for the Current ICD-10 panel.

    AI predictions are returned separately through ``aiLabel`` so the
    dictionary and AI workflows cannot be mixed in the same result.
    """
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
    return labels


def _annotation_seed(text: str, label_source: str = "icd10", ai_payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build editable per-entity annotations with stable source offsets."""
    if label_source == "ai":
        result: list[dict[str, Any]] = []
        used_ranges: set[tuple[int, int]] = set()
        for category, values in (ai_payload or {}).items():
            if not isinstance(values, list):
                continue
            for raw in values:
                item = raw if isinstance(raw, dict) else {"term": str(raw)}
                term = str(item.get("term") or item.get("text") or "").strip()
                spans = item.get("spans") or []
                candidates = [
                    (int(span.get("start", -1)), int(span.get("end", -1)))
                    for span in spans
                    if isinstance(span, dict)
                ]
                if not candidates:
                    cursor = 0
                    while True:
                        found = text.casefold().find(term.casefold(), cursor)
                        if found < 0:
                            break
                        candidates.append((found, found + len(term)))
                        cursor = found + 1
                        if len(candidates) >= 1:
                            break
                for start, end in candidates:
                    if term and 0 <= start < end <= len(text) and (start, end) not in used_ranges:
                        used_ranges.add((start, end))
                        result.append({
                            "text": text[start:end], "start": start, "end": end,
                            "category": category, "type": category,
                            "code": str(item.get("code") or ""),
                            "label": str(item.get("label_vn") or term),
                            "action": "KEEP",
                        })
        return result
    _, _, entities, _ = run_ner(text or "", enable_tone_restore=False, enable_noun_phrase=False)
    return [
        {
            "text": entity["text"],
            "start": entity["start"],
            "end": entity["end"],
            "category": entity.get("entity_type", "DISEASE"),
            "type": entity.get("entity_type", "DISEASE"),
            "code": entity.get("icd_code", ""),
            "label": entity.get("icd_label_vn") or entity["text"],
            "action": "KEEP",
        }
        for entity in entities
    ]


def _normalize_annotations(raw: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    allowed_actions = {"KEEP", "EDIT", "DELETE", "ADD"}
    normalized: list[dict[str, Any]] = []
    used_ranges: set[tuple[int, int]] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        raw_start, raw_end = item.get("start"), item.get("end")
        start = int(raw_start) if raw_start is not None and str(raw_start) != "" else -1
        end = int(raw_end) if raw_end is not None and str(raw_end) != "" else -1
        action = str(item.get("action") or "KEEP").upper()
        surface = str(item.get("text") or "").strip()
        if action not in allowed_actions or not surface:
            raise HTTPException(status_code=422, detail="Annotation cần có đoạn thực thể và thao tác hợp lệ.")
        if start < 0 or end <= start or end > len(text) or text[start:end] != surface:
            start = end = -1
            search_from = 0
            while True:
                found = text.casefold().find(surface.casefold(), search_from)
                if found < 0:
                    break
                candidate = (found, found + len(surface))
                if candidate not in used_ranges:
                    start, end = candidate
                    break
                search_from = found + 1
        if start < 0 or end <= start or end > len(text):
            raise HTTPException(status_code=422, detail=f"Không tìm thấy đoạn thực thể trong văn bản: {surface}")
        used_ranges.add((start, end))
        if text[start:end] != surface:
            raise HTTPException(status_code=422, detail="Đoạn text của annotation không khớp văn bản gốc.")
        normalized.append({
            "text": surface,
            "start": start,
            "end": end,
            "category": str(item.get("category") or item.get("type") or "Khác"),
            "type": str(item.get("type") or item.get("category") or "Khác"),
            "code": str(item.get("code") or ""),
            "label": str(item.get("label") or surface),
            "action": action,
        })
    return normalized


def _stored_ner_result(cursor, document_id: int) -> dict[str, Any]:
    """Return the exact rule-based NER rendering saved by the Admin."""
    cursor.execute("SELECT highlighted_html, abstract FROM articles WHERE id = %s", (document_id,))
    article = cursor.fetchone() or {}
    article_text = article.get("abstract") or ""
    cursor.execute(
        """
        SELECT concept_name AS name, concept_type AS type, concept_code AS code,
               concept_start AS start, concept_end AS end
        FROM extracted_concepts
        WHERE article_id = %s
        ORDER BY id
        """,
        (document_id,),
    )
    stored = cursor.fetchall()
    concepts = [
        {
            "text": item["name"],
            "start": item["start"],
            "end": item["end"],
            "category": item["type"],
            "type": item["type"],
            "code": item["code"] or "",
            "label": item["name"],
            "action": "KEEP",
        }
        for item in stored
        if item.get("start") is not None and item.get("end") is not None
    ]
    if not concepts:
        concepts = _annotation_seed(article_text)
    return {
        "highlightedHtml": article.get("highlighted_html") or "",
        "concepts": concepts,
    }


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
        SELECT r.id, r.review_status, r.label_source, r.original_labels_json,
               r.annotations_json, r.suggested_icd10_code,
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
        try:
            row["annotations"] = json.loads(row.pop("annotations_json") or "[]")
        except (KeyError, TypeError, json.JSONDecodeError):
            row["annotations"] = row["original_labels"]
    return rows


def _case_resolution_status(cursor, document_id: int) -> str:
    cursor.execute(
        """
        SELECT COUNT(*) AS expert_count
        FROM (
            SELECT expert_id
            FROM expert_reviews
            WHERE document_id = %s
            GROUP BY expert_id
        ) experts
        """,
        (document_id,),
    )
    if int(cursor.fetchone()["expert_count"] or 0) < 2:
        return "PENDING"
    cursor.execute(
        """
        SELECT resolution_status
        FROM reviewer_adjudications
        WHERE document_id = %s
        ORDER BY id DESC LIMIT 1
        """,
        (document_id,),
    )
    adjudication = cursor.fetchone()
    return str(adjudication["resolution_status"]).upper() if adjudication else "CONFLICT"


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
              AND (NOT EXISTS (SELECT 1 FROM labeling_session_assignments own_assign WHERE own_assign.expert_id = %s)
                   OR EXISTS (SELECT 1 FROM labeling_session_assignments assigned WHERE assigned.article_id = a.id AND assigned.expert_id = %s))
            GROUP BY a.id, a.title, a.authors, a.publication_year, latest.review_status, latest.id
            ORDER BY a.id DESC
        """
        params = (user["id"], q, f"%{q}%", f"%{q}%", icd, f"%{icd}%", user["id"], user["id"])
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
              AND (NOT EXISTS (SELECT 1 FROM labeling_session_assignments own_assign WHERE own_assign.expert_id = %s)
                   OR EXISTS (SELECT 1 FROM labeling_session_assignments assigned WHERE assigned.article_id = a.id AND assigned.expert_id = %s))
            ORDER BY a.id DESC
            """,
            (user["id"], q, f"%{q}%", f"%{q}%", user["id"], user["id"]),
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
        article["aiLabels"] = _all_ai_labels(cursor, document_id)
        article["reviewHistory"] = _review_history(cursor, document_id, int(user["id"]))
        source = "icd10"
        if article["reviewHistory"]:
            source = article["reviewHistory"][0].get("label_source") or source
        article["annotationSeed"] = (
            article["reviewHistory"][0]["annotations"]
            if article["reviewHistory"] and article["reviewHistory"][0].get("annotations")
            else _annotation_seed(
                article.get("abstract") or "",
                source,
                article["aiLabel"]["labels"] if source == "ai" and article["aiLabel"] else None,
            )
        )
        article["nerResult"] = _stored_ner_result(cursor, document_id)
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
    label_source = str(req.label_source or "icd10").strip().lower()
    if label_source not in {"icd10", "ai"}:
        label_source = "icd10"
    comment = str(req.comment or "").strip()
    if not 3 <= len(comment) <= 8000:
        raise HTTPException(status_code=422, detail="Nhận xét cần từ 3 đến 8000 ký tự.")
    suggested_code = str(req.suggested_icd10_code or "").strip()
    if status in {"INCORRECT", "NEEDS_REVISION"} and not req.annotations and not suggested_code:
        raise HTTPException(status_code=422, detail="Cần gửi các nhãn đã chỉnh sửa hoặc mã ICD-10/YHCT đề xuất.")
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
        cursor.execute("SELECT abstract FROM articles WHERE id = %s", (document_id,))
        article_text = (cursor.fetchone() or {}).get("abstract") or ""

        # Lấy nhãn gốc tương ứng với nguồn gốc của bài báo
        if label_source == "ai":
            ai_row = _latest_ai_label(cursor, document_id)
            if ai_row and ai_row.get("labels"):
                # Chuẩn hoá về cùng schema [{code, label, source, type}]
                original_labels = _annotation_seed(article_text, "ai", ai_row.get("labels"))
            else:
                original_labels = []
        else:
            original_labels = _annotation_seed(article_text)
        annotations = _normalize_annotations(req.annotations or original_labels, article_text)
        changed_annotations = [item for item in annotations if item["action"] != "KEEP"]
        if status in {"INCORRECT", "NEEDS_REVISION"} and not changed_annotations and not suggested_code:
            raise HTTPException(status_code=422, detail="Incorrect hoặc Needs Revision cần ít nhất một nhãn được sửa, xóa hoặc thêm.")
        for item in annotations:
            if item["code"] and item["code"].casefold() not in _icd_code_catalog():
                raise HTTPException(status_code=422, detail=f"Mã nhãn không tồn tại trong từ điển: {item['code']}")

        cursor.execute(
            """
            INSERT INTO expert_reviews
            (document_id, expert_id, review_status, label_source, original_labels_json,
             annotations_json, suggested_icd10_code, suggested_icd10_label, comment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                document_id, user["id"], status, label_source,
                json.dumps(original_labels, ensure_ascii=False),
                json.dumps(annotations, ensure_ascii=False),
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


@router.get("/api/reviewer/dashboard")
def reviewer_dashboard(_: dict[str, Any] = Depends(_require_reviewer)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT COUNT(*) AS value FROM expert_reviews")
        total_reviews = cursor.fetchone()["value"]
        cursor.execute("SELECT COUNT(DISTINCT expert_id) AS value FROM expert_reviews")
        active_experts = cursor.fetchone()["value"]
        cursor.execute("SELECT COUNT(DISTINCT document_id) AS value FROM expert_reviews")
        reviewed_documents = cursor.fetchone()["value"]
        cursor.execute(
            "SELECT COUNT(*) AS value FROM expert_reviews WHERE review_status = 'CORRECT'"
        )
        correct_reviews = cursor.fetchone()["value"]
        cursor.execute(
            "SELECT COUNT(*) AS value FROM expert_reviews WHERE review_status = 'INCORRECT'"
        )
        incorrect_reviews = cursor.fetchone()["value"]
        cursor.execute(
            "SELECT COUNT(*) AS value FROM expert_reviews WHERE review_status = 'NEEDS_REVISION'"
        )
        needs_revision = cursor.fetchone()["value"]
        return {
            "totalReviews": total_reviews,
            "activeExperts": active_experts,
            "reviewedDocuments": reviewed_documents,
            "correctReviews": correct_reviews,
            "incorrectReviews": incorrect_reviews,
            "needsRevision": needs_revision,
        }
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/reviewer/reviews")
def reviewer_reviews(
    q: str = "", review_status: str = "", page: int = 1, page_size: int = 50,
    _: dict[str, Any] = Depends(_require_reviewer),
):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT r.id, r.document_id, a.title AS document_title,
                   u.full_name AS expert_name, u.email AS expert_email,
                   r.review_status, r.suggested_icd10_code, r.suggested_icd10_label,
                   r.comment, r.created_at, r.updated_at
            FROM expert_reviews r
            JOIN articles a ON a.id = r.document_id
            JOIN users u ON u.id = r.expert_id
            WHERE (%s = '' OR a.title LIKE %s OR u.full_name LIKE %s OR u.email LIKE %s)
            ORDER BY r.id DESC
            """,
            (q, f"%{q}%", f"%{q}%", f"%{q}%"),
        )
        rows = cursor.fetchall()
        if review_status:
            rows = [row for row in rows if row["review_status"] == review_status.upper()]
        return _paginate(rows, page, page_size)
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/reviewer/documents/{document_id}")
def reviewer_document_detail(document_id: int, user: dict[str, Any] = Depends(_require_reviewer)):
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
        article["annotationSeed"] = _annotation_seed(article.get("abstract") or "")
        article["nerResult"] = _stored_ner_result(cursor, document_id)
        article["aiLabel"] = _latest_ai_label(cursor, document_id)
        article["reviewHistory"] = _review_history(cursor, document_id)
        article["resolutionStatus"] = _case_resolution_status(cursor, document_id)
        cursor.execute(
            """
            SELECT id, decision_payload, note, created_at
                   , final_labels_json, resolution_status
            FROM reviewer_adjudications
            WHERE document_id = %s AND reviewer_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (document_id, user["id"]),
        )
        adjudication = cursor.fetchone()
        if adjudication:
            try:
                adjudication["decisions"] = json.loads(adjudication.pop("decision_payload"))
            except (TypeError, json.JSONDecodeError):
                adjudication["decisions"] = []
            try:
                adjudication["final_labels"] = json.loads(adjudication.pop("final_labels_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                adjudication["final_labels"] = []
        article["adjudication"] = adjudication
        return article
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.post("/api/reviewer/documents/{document_id}/adjudication", status_code=201)
def save_reviewer_adjudication(
    document_id: int,
    req: ReviewerAdjudicationRequest,
    user: dict[str, Any] = Depends(_require_reviewer),
):
    resolution = str(req.resolution_status or "").strip().upper()
    if resolution not in {"CONFLICT", "RESOLVED"}:
        raise HTTPException(status_code=422, detail="Trạng thái adjudication không hợp lệ.")
    if resolution == "RESOLVED" and not req.final_labels:
        # A document with no conflicts may legitimately resolve to an empty
        # final set, but a conflict resolution must contain the chosen labels.
        has_conflict = any(str(item.get("status") or "").lower() != "agree" for item in req.decisions)
        if has_conflict:
            raise HTTPException(status_code=422, detail="Kết quả RESOLVED cần có bộ nhãn cuối.")
    if len(req.note) > 8000:
        raise HTTPException(status_code=422, detail="Ghi chú không được vượt quá 8000 ký tự.")
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id FROM articles WHERE id = %s", (document_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"Không tìm thấy văn bản id={document_id}")
        cursor.execute("SELECT abstract FROM articles WHERE id = %s", (document_id,))
        article_text = (cursor.fetchone() or {}).get("abstract") or ""
        final_labels = _normalize_annotations(req.final_labels, article_text)
        if resolution == "RESOLVED":
            conflict_decisions = [item for item in req.decisions if str(item.get("status") or "").lower() != "agree"]
            unresolved = [
                item for item in conflict_decisions
                if str(item.get("decision") or "") not in {"expert_a", "expert_b", "custom"}
            ]
            if unresolved:
                raise HTTPException(status_code=422, detail="Vẫn còn mâu thuẫn chưa được reviewer quyết định.")
        cursor.execute(
            """
            INSERT INTO reviewer_adjudications
                (document_id, reviewer_id, decision_payload, final_labels_json, resolution_status, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                document_id,
                user["id"],
                json.dumps(req.decisions, ensure_ascii=False),
                json.dumps(final_labels, ensure_ascii=False),
                resolution,
                req.note.strip(),
            ),
        )
        connection.commit()
        return {"message": "Đã lưu quyết định reviewer.", "id": cursor.lastrowid}
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/reviewer/documents/{document_id}/adjudication")
def reviewer_adjudication_detail(document_id: int, user: dict[str, Any] = Depends(_require_reviewer)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT id, decision_payload, final_labels_json, resolution_status, note, created_at
            FROM reviewer_adjudications
            WHERE document_id = %s AND reviewer_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (document_id, user["id"]),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Chưa có quyết định reviewer cho văn bản này.")
        for source, target in (("decision_payload", "decisions"), ("final_labels_json", "finalLabels")):
            try:
                row[target] = json.loads(row.pop(source) or "[]")
            except (KeyError, TypeError, json.JSONDecodeError):
                row[target] = []
        return row
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/admin/users")
def admin_users(
    page: int = 1, page_size: int = 30, role: str = "",
    _: dict[str, Any] = Depends(_require_admin),
):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        query = """
            SELECT u.id, u.full_name AS name, u.email, LOWER(u.role) AS role,
                   u.is_active, u.created_at, COUNT(r.id) AS review_count
            FROM users u LEFT JOIN expert_reviews r ON r.expert_id = u.id
            WHERE (%s = '' OR u.role = %s)
            GROUP BY u.id, u.full_name, u.email, u.role, u.is_active, u.created_at
            ORDER BY u.created_at DESC
        """
        cursor.execute(query, (role.strip().upper(), role.strip().upper()))
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


@router.post("/api/admin/reviewers", status_code=201)
def create_reviewer_account(req: RegisterRequest, _: dict[str, Any] = Depends(_require_admin)):
    """Create a read-only Reviewer account from the Admin workspace."""
    if req.password != req.confirm_password:
        raise HTTPException(status_code=422, detail="Xác nhận mật khẩu không khớp.")
    try:
        user = register_reviewer(_db_config, req.full_name, req.email, req.password)
        return {"message": "Đã tạo tài khoản Reviewer thành công.", "user": user}
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
        article["aiLabels"] = _all_ai_labels(cursor, document_id)
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
