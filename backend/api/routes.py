import threading
import json
import os
import io
import hashlib
import logging
import math
import re
import smtplib
import time
from collections import Counter, defaultdict, deque
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime
from threading import Lock
from typing import Any
from urllib.parse import urlparse
import mysql.connector
from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File, Form, Request
from pydantic import BaseModel, ConfigDict, Field
from core.ner_engine import ner_with_fuzzy, reset_ner_engine, run_ner
from core.ner_dict import (
    DICT_DIR,
    MANIFEST_PATH,
    dictionary_metadata,
    normalize_match_text,
    load_runtime_aliases,
    reload_ner_dictionary,
    term_candidates,
    term_dict,
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
    hash_password,
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
logger = logging.getLogger(__name__)

_db_config: dict = {}
_output_folder: str = ""
tamanh_job_manager = TamanhCrawlerJobManager()
_scrape_start_lock = Lock()

# Guardrails for browser/API inputs. These are intentionally conservative
# defaults for an admin tool and prevent accidental or malicious resource
# exhaustion before work reaches the PDF/LLM pipeline.
MAX_API_TEXT_CHARS = 200_000
MAX_AI_LABEL_PAYLOAD_CHARS = 2_000_000
MAX_PDF_UPLOAD_BYTES = 50 * 1024 * 1024

def init_router(db_config: dict, output_folder: str) -> None:
    """Khởi tạo các biến cấu hình cho router."""
    global _db_config, _output_folder
    _db_config = db_config
    _output_folder = output_folder
    tamanh_job_manager.configure_db(db_config)
    ner_experiment_manager.configure_db(db_config)
    _refresh_runtime_aliases()

class HighlightRequest(BaseModel):
    text:                str = Field(..., max_length=MAX_API_TEXT_CHARS)
    threshold:           int  = 100
    enable_tone_restore: bool = False
    enable_noun_phrase:  bool = False

class NerRequest(BaseModel):
    text:                str = Field(..., max_length=MAX_API_TEXT_CHARS)
    threshold:           int  = 100
    enable_tone_restore: bool = False
    enable_noun_phrase:  bool = False

class AiLabelRequest(BaseModel):
    text: str = Field(..., max_length=MAX_API_TEXT_CHARS)


class XLMRNERRequest(BaseModel):
    text: str = Field(..., max_length=MAX_API_TEXT_CHARS)

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


class UserStatusRequest(BaseModel):
    is_active: bool = Field(..., alias="isActive")

    model_config = ConfigDict(populate_by_name=True)


class ResetPasswordRequest(BaseModel):
    password: str
    confirm_password: str = Field(..., alias="confirmPassword")

    model_config = ConfigDict(populate_by_name=True)


class LabelingSessionStatusRequest(BaseModel):
    status: str

class ReviewCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    review_status: str = Field(..., alias="reviewStatus")
    label_source: str = Field("icd10", alias="labelSource")  # 'icd10' | 'ai'
    annotations: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    suggested_icd10_code: str | None = Field(None, alias="suggestedIcd10Code")
    comment: str = Field(..., max_length=8000)

class ReviewerAdjudicationRequest(BaseModel):
    decisions: list[dict[str, Any]] = Field(..., max_length=500)
    final_labels: list[dict[str, Any]] = Field(default_factory=list, max_length=500, alias="finalLabels")
    resolution_status: str = Field("RESOLVED", alias="resolutionStatus")
    note: str = Field("", max_length=8000)

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


class AliasDiscoveryRequest(BaseModel):
    limit: int = Field(1000, ge=1, le=10000)
    include_fuzzy_candidates: bool = False
    include_context: bool = True


class AliasSuggestionApprovalRequest(BaseModel):
    surface_form: str = Field(..., min_length=2, max_length=500)
    concept_id: str = Field(..., min_length=1, max_length=255)
    concept_code: str = Field(..., min_length=1, max_length=100)
    canonical_term: str = Field(..., min_length=1, max_length=500)
    concept_type: str = Field("Bệnh Lý", max_length=100)
    match_method: str = Field("fuzzy_candidate", max_length=40)
    confidence_score: float | None = Field(None, ge=0, le=1)
    article_id: int | None = None
    start: int | None = Field(None, ge=0)
    end: int | None = Field(None, ge=0)
    context: str = Field("", max_length=2000)


class NerExperimentCreateRequest(BaseModel):
    limit: int = Field(100, ge=1, le=100)
    note: str = ""
    parent_run_id: str | None = Field(None, alias="parentRunId")


class GoldAnnotationRequest(BaseModel):
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    note: str = Field("", max_length=8000)

class LabelingSessionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = Field("", max_length=4000)
    article_ids: list[int] = Field(default_factory=list, max_length=5000, alias="articleIds")
    expert_ids: list[int] = Field(default_factory=list, max_length=2, alias="expertIds")
    reviewer_id: int = Field(..., alias="reviewerId")
    send_email: bool = Field(True, alias="sendEmail")

    model_config = ConfigDict(populate_by_name=True)

def _get_conn():
    return mysql.connector.connect(**_db_config)


def _refresh_runtime_aliases() -> int:
    """Load only reviewer-approved aliases from the optional MySQL table."""
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT surface_form, concept_id, canonical_term, concept_code, alias_type
            FROM dictionary_alias_candidates
            WHERE status = 'APPROVED'
            ORDER BY id
            """
        )
        return load_runtime_aliases(cursor.fetchall())
    except mysql.connector.Error as exc:
        # The table is intentionally a manual migration. A deployment that
        # has not run the SQL keeps the base dictionary fully functional.
        logger.warning("Approved alias table is unavailable: %s", exc)
        load_runtime_aliases([])
        return 0
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def _dictionary_concept_for_identity(concept_id: str = "", code: str = "") -> dict[str, Any] | None:
    identities = {
        str(value or "").strip().casefold()
        for value in (concept_id, code)
        if str(value or "").strip()
    }
    for values in term_candidates.values():
        for info in values:
            if any(
                str(info.get(field) or "").strip().casefold() in identities
                for field in ("concept_id", "code")
            ):
                return info
    return None


def _candidate_variant_rows(limit: int, include_context: bool = True) -> list[dict[str, Any]]:
    """Find conservative grammar variants actually present in article text.

    This first discovery pass only removes Vietnamese leading determiners
    such as ``các``/``những``. It creates review candidates; it never activates
    an alias automatically.
    """
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, abstract FROM articles "
            "WHERE abstract IS NOT NULL AND abstract <> '' "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        articles = cursor.fetchall()
        concepts: dict[tuple[str, str], dict[str, Any]] = {}
        token_counts: Counter[str] = Counter()
        bigram_counts: Counter[tuple[str, str]] = Counter()
        total_tokens = 0
        for info in term_dict.values():
            code = str(info.get("code") or "").strip()
            concept_id = str(info.get("concept_id") or code).strip()
            canonical = str(info.get("canonical_term") or info.get("label_vn") or "").strip()
            key = (concept_id, normalize_match_text(canonical))
            if code and canonical and key not in concepts:
                concepts[key] = info

        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        determiners = ("các ", "những ", "một ")
        for article in articles:
            raw_text = str(article.get("abstract") or "")
            normalized_text = normalize_match_text(raw_text)
            article_tokens = normalized_text.split()
            token_counts.update(article_tokens)
            bigram_counts.update(zip(article_tokens, article_tokens[1:]))
            total_tokens += len(article_tokens)
            for info in concepts.values():
                canonical = str(info.get("canonical_term") or info.get("label_vn") or "").strip()
                canonical_key = normalize_match_text(canonical)
                for prefix in determiners:
                    if not canonical_key.startswith(prefix):
                        continue
                    alias = canonical[len(prefix):].strip()
                    alias_key = normalize_match_text(alias)
                    if len(alias_key) < 8:
                        continue
                    boundary_re = re.compile(rf"(?<!\w){re.escape(alias_key)}(?!\w)", re.UNICODE)
                    occurrences = list(boundary_re.finditer(normalized_text))
                    if not occurrences:
                        continue
                    concept_id = str(info.get("concept_id") or info.get("code") or "").strip()
                    code = str(info.get("code") or "").strip()
                    candidate_key = (alias_key, concept_id)
                    item = candidates.setdefault(candidate_key, {
                        "surface_form": alias,
                        "concept_id": concept_id,
                        "canonical_term": canonical,
                        "concept_code": code,
                        "concept_type": info.get("cat") or "Bệnh Lý",
                        "alias_type": "grammar_variant",
                        "discovery_source": "corpus_leading_determiner",
                        "occurrence_count": 0,
                        "document_frequency": 0,
                        "evidence": [],
                        "_evidence_keys": set(),
                    })
                    item["occurrence_count"] += len(occurrences)
                    item["document_frequency"] += 1
                    if len(item["evidence"]) < 3:
                        for occurrence in occurrences:
                            evidence_key = (int(article["id"]), occurrence.start())
                            if evidence_key in item["_evidence_keys"]:
                                continue
                            item["_evidence_keys"].add(evidence_key)
                            evidence = {
                                "article_id": int(article["id"]),
                                "start": occurrence.start(),
                                "end": occurrence.end(),
                                "text": alias,
                            }
                            if include_context:
                                start = max(0, occurrence.start() - 120)
                                end = min(len(normalized_text), occurrence.end() + 120)
                                evidence["context"] = normalized_text[start:end]
                            item["evidence"].append(evidence)
                            if len(item["evidence"]) >= 3:
                                break

        def mean_phrase_mi(phrase: str) -> float:
            words = phrase.split()
            if len(words) < 2 or total_tokens <= 0:
                return 0.0
            values = []
            for left, right in zip(words, words[1:]):
                pair_count = bigram_counts[(left, right)]
                if not pair_count or not token_counts[left] or not token_counts[right]:
                    continue
                values.append(math.log2((pair_count * total_tokens) / (token_counts[left] * token_counts[right])))
            return round(sum(values) / len(values), 5) if values else 0.0

        output = []
        for item in candidates.values():
            item.pop("_evidence_keys", None)
            item["mi_score"] = mean_phrase_mi(normalize_match_text(item["surface_form"]))
            item["boundary_score"] = 1.0
            item["context_consistency"] = 1.0 if item["document_frequency"] >= 2 else 0.5
            item["confidence_score"] = round(
                min(0.99, 0.55 + 0.15 * item["boundary_score"] +
                    0.15 * min(1.0, item["document_frequency"] / 3) +
                    0.10 * item["context_consistency"] +
                    0.05 * min(1.0, max(0.0, item["mi_score"]) / 5)),
                5,
            )
            item["uncertainty_score"] = round(1.0 - item["confidence_score"], 5)
            item["density_score"] = round(min(1.0, item["document_frequency"] / 5), 5)
            item["priority_score"] = round(
                item["uncertainty_score"] * 0.6 + item["density_score"] * 0.4,
                5,
            )
            output.append(item)
        return sorted(output, key=lambda item: (-item["priority_score"], item["surface_form"]))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def _fuzzy_candidate_rows(limit: int, include_context: bool = True) -> list[dict[str, Any]]:
    """Collect review-only fuzzy suggestions from the deterministic matcher."""
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, abstract FROM articles "
            "WHERE abstract IS NOT NULL AND abstract <> '' "
            "ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        candidates: dict[tuple[str, str], dict[str, Any]] = {}
        for article in cursor.fetchall():
            raw_text = str(article.get("abstract") or "")
            suggestions = ner_with_fuzzy(raw_text).get("suggestions", [])
            for suggestion in suggestions:
                if suggestion.get("matched_by") != "fuzzy_candidate":
                    continue
                concepts = suggestion.get("candidate_concepts") or []
                if len(concepts) != 1:
                    continue
                concept = concepts[0]
                concept_id = str(concept.get("concept_id") or "").strip()
                code = str(concept.get("concept_code") or "").strip()
                surface = str(suggestion.get("text") or "").strip()
                surface_key = normalize_match_text(surface)
                if not surface_key or not concept_id:
                    continue
                key = (surface_key, concept_id)
                item = candidates.setdefault(key, {
                    "surface_form": surface,
                    "concept_id": concept_id,
                    "canonical_term": concept.get("canonical_term") or surface,
                    "concept_code": code,
                    "concept_type": concept.get("entity_type") or "Bệnh Lý",
                    "alias_type": "fuzzy_candidate",
                    "discovery_source": "corpus_fuzzy_review",
                    "occurrence_count": 0,
                    "document_frequency": 0,
                    "evidence": [],
                    "_evidence_keys": set(),
                    "confidence_score": float(suggestion.get("confidence") or 0.0),
                })
                item["occurrence_count"] += 1
                item["document_frequency"] += 1
                evidence_key = (int(article["id"]), int(suggestion.get("start") or 0))
                if evidence_key not in item["_evidence_keys"] and len(item["evidence"]) < 3:
                    item["_evidence_keys"].add(evidence_key)
                    evidence = {
                        "article_id": int(article["id"]),
                        "start": int(suggestion.get("start") or 0),
                        "end": int(suggestion.get("end") or 0),
                        "text": surface,
                    }
                    if include_context:
                        start = max(0, evidence["start"] - 120)
                        end = min(len(raw_text), evidence["end"] + 120)
                        evidence["context"] = raw_text[start:end]
                    item["evidence"].append(evidence)
        output = []
        for item in candidates.values():
            item.pop("_evidence_keys", None)
            item["match_method"] = "fuzzy_candidate"
            item["uncertainty_score"] = round(1.0 - item["confidence_score"], 5)
            item["density_score"] = round(min(1.0, item["document_frequency"] / 5), 5)
            item["priority_score"] = round(
                item["uncertainty_score"] * 0.6 + item["density_score"] * 0.4,
                5,
            )
            item["mi_score"] = 0.0
            item["boundary_score"] = 1.0
            item["context_consistency"] = 0.5
            output.append(item)
        return sorted(output, key=lambda item: (-item["priority_score"], item["surface_form"]))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_login_attempts_lock = Lock()
_LOGIN_WINDOW_SECONDS = int(os.getenv("AUTH_LOGIN_WINDOW_SECONDS", "900"))
_LOGIN_MAX_ATTEMPTS = int(os.getenv("AUTH_LOGIN_MAX_ATTEMPTS", "5"))


def _login_rate_key(request: Request, email: str) -> str:
    # Do not trust a client-supplied X-Forwarded-For value. If the deployment
    # is behind a proxy, configure trusted proxy middleware instead.
    host = request.client.host if request.client else "unknown"
    return f"{host}|{str(email or '').strip().casefold()}"


def _check_login_rate_limit(key: str) -> None:
    now = time.monotonic()
    with _login_attempts_lock:
        attempts = _login_attempts[key]
        while attempts and now - attempts[0] > _LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=429,
                detail="Đăng nhập thất bại quá nhiều lần. Vui lòng thử lại sau.",
            )


def _record_login_failure(key: str) -> None:
    with _login_attempts_lock:
        _login_attempts[key].append(time.monotonic())


def _clear_login_failures(key: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(key, None)

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
    if len(serialized_labels) > MAX_AI_LABEL_PAYLOAD_CHARS:
        raise HTTPException(status_code=413, detail="Kết quả AI vượt quá kích thước cho phép.")
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
def login_endpoint(req: LoginRequest, request: Request):
    rate_key = _login_rate_key(request, req.email)
    _check_login_rate_limit(rate_key)
    try:
        user, token, expires_at = authenticate(_db_config, req.email, req.password)
        _clear_login_failures(rate_key)
        return {"user": user, "token": token, "expiresAt": expires_at}
    except ValueError as exc:
        _record_login_failure(rate_key)
        raise HTTPException(status_code=422, detail=str(exc))
    except HTTPException:
        _record_login_failure(rate_key)
        raise


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
    suggestions = preproc_log.get("suggestions", [])
    note = "" if concepts else (
        "Có candidate không chắc chắn cần Reviewer kiểm tra."
        if suggestions else "Đoạn văn mô tả cơ chế sinh lý, không chứa chẩn đoán bệnh lý cụ thể"
    )
    return {
        "highlighted_html":  highlighted,
        "matched_concepts":  concepts,
        "suggestions":       suggestions,
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
    suggestions = preproc_log.get("suggestions", [])
    note = "" if entities else (
        "Có candidate không chắc chắn cần Reviewer kiểm tra."
        if suggestions else "Đoạn văn mô tả cơ chế sinh lý, không chứa chẩn đoán bệnh lý cụ thể"
    )
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
                "concept_id":   e.get("concept_id", e.get("icd_code", "")),
                "concept_id_source": e.get("concept_id_source", ""),
                "icd_label_vn": e.get("icd_label_vn", ""),
                "matched_by":   e.get("matched_by", "exact"),
                "confidence":   e.get("confidence", 1.0),
            }
            for e in entities
        ],
        "note":              note or None,
        "matched_concepts":  concepts,
        "suggestions":       suggestions,
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
        logger.exception("AI NER request failed")
        raise HTTPException(500, "Không thể hoàn tất gán nhãn AI lúc này.") from e


@router.post("/api/xlmr-ner")
def xlmr_ner_endpoint(req: XLMRNERRequest, _: dict[str, Any] = Depends(_require_admin)):
    """Run the locally fine-tuned XLM-R token-classification checkpoint."""
    try:
        return xlmr_ner.predict(req.text)
    except XLMRInferenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("XLM-R NER request failed")
        raise HTTPException(status_code=500, detail="Không thể hoàn tất XLM-R NER lúc này.") from exc

@router.get("/api/admin/labeling-options")
def labeling_options(_: dict[str, Any] = Depends(_require_admin)):
    connection = cursor = None
    try:
        connection = _get_conn(); cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT a.id, a.title, a.authors, a.publication_year,
                   COUNT(DISTINCT CASE WHEN ls.status = 'OPEN' THEN lsa.session_id END) AS assigned_sessions
            FROM articles a
            LEFT JOIN labeling_session_assignments lsa ON lsa.article_id = a.id
            LEFT JOIN labeling_sessions ls ON ls.id = lsa.session_id
            GROUP BY a.id, a.title, a.authors, a.publication_year
            ORDER BY a.id DESC LIMIT 1000
            """
        )
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
    name = req.name.strip()
    description = req.description.strip()
    article_ids = sorted({int(value) for value in req.article_ids if int(value) > 0})
    expert_ids = sorted({int(value) for value in req.expert_ids if int(value) > 0})
    if not name or not article_ids or len(expert_ids) != 2:
        raise HTTPException(422, "Cần tên phiên, ít nhất một tài liệu và đúng hai Expert.")
    if int(req.reviewer_id) in expert_ids:
        raise HTTPException(422, "Reviewer không được đồng thời là Expert của cùng phiên.")
    if len(name) > 255 or len(description) > 4000:
        raise HTTPException(422, "Tên hoặc mô tả phiên vượt quá độ dài cho phép.")
    connection = cursor = None
    try:
        connection = _get_conn(); cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, full_name AS name, email FROM users WHERE id IN (%s,%s) AND role='EXPERT' AND is_active=1", tuple(expert_ids))
        experts = cursor.fetchall()
        if len(experts) != 2:
            raise HTTPException(422, "Một hoặc nhiều Expert không hợp lệ hoặc đã bị khóa.")
        cursor.execute("SELECT id FROM users WHERE id=%s AND role='REVIEWER' AND is_active=1", (req.reviewer_id,))
        if not cursor.fetchone():
            raise HTTPException(422, "Reviewer không hợp lệ hoặc đã bị khóa.")

        placeholders = ",".join(["%s"] * len(article_ids))
        # Lock the article rows while checking and creating assignments. This
        # closes the race where two Admin requests both observe an article as
        # unassigned and then create two OPEN sessions for it.
        cursor.execute(f"SELECT id FROM articles WHERE id IN ({placeholders}) FOR UPDATE", tuple(article_ids))
        existing_article_ids = {int(row["id"]) for row in cursor.fetchall()}
        missing = sorted(set(article_ids) - existing_article_ids)
        if missing:
            raise HTTPException(422, f"Tài liệu không tồn tại: {missing[:10]}")
        cursor.execute(
            f"""
            SELECT DISTINCT lsa.article_id
            FROM labeling_session_assignments lsa
            JOIN labeling_sessions ls ON ls.id = lsa.session_id
            WHERE ls.status = 'OPEN' AND lsa.article_id IN ({placeholders})
            """,
            tuple(article_ids),
        )
        already_open = sorted(int(row["article_id"]) for row in cursor.fetchall())
        if already_open:
            raise HTTPException(
                409,
                f"Các tài liệu đã thuộc một phiên đang mở: {already_open[:10]}",
            )

        cursor.execute(
            "INSERT INTO labeling_sessions (name, description, reviewer_id, created_by) VALUES (%s,%s,%s,%s)",
            (name, description, req.reviewer_id, user["id"]),
        )
        session_id = cursor.lastrowid
        rows = [(session_id, article_id, expert_id) for article_id in article_ids for expert_id in expert_ids]
        cursor.executemany("INSERT INTO labeling_session_assignments (session_id, article_id, expert_id) VALUES (%s,%s,%s)", rows)
        connection.commit()
        if req.send_email:
            try:
                emailed = _send_assignment_email(experts, name, len(article_ids))
                email_status = "sent" if emailed else "not_configured"
            except Exception as email_error:
                email_status = f"failed: {email_error}"
        else:
            email_status = "disabled"
        return {
            "sessionId": int(session_id),
            "emailStatus": email_status,
            "assignedExperts": len(experts),
            "assignedDocuments": len(article_ids),
        }
    except HTTPException:
        if connection: connection.rollback()
        raise
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/admin/labeling-sessions")
def list_labeling_sessions(
    page: int = 1,
    page_size: int = 30,
    status: str = "",
    _: dict[str, Any] = Depends(_require_admin),
):
    normalized_status = status.strip().upper()
    if normalized_status and normalized_status not in {"OPEN", "CLOSED", "CANCELLED"}:
        raise HTTPException(422, "Trạng thái phiên không hợp lệ.")
    connection = cursor = None
    try:
        connection = _get_conn(); cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT ls.id, ls.name, ls.description, ls.status, ls.created_at, ls.updated_at,
                   reviewer.full_name AS reviewer_name,
                   creator.full_name AS created_by_name,
                   COUNT(DISTINCT lsa.article_id) AS document_count,
                   COUNT(DISTINCT lsa.expert_id) AS expert_count,
                   COUNT(DISTINCT CONCAT(r.document_id, ':', r.expert_id)) AS submitted_reviews,
                   COUNT(DISTINCT CASE WHEN ra.resolution_status = 'RESOLVED' THEN ra.document_id END) AS resolved_documents
            FROM labeling_sessions ls
            JOIN users reviewer ON reviewer.id = ls.reviewer_id
            JOIN users creator ON creator.id = ls.created_by
            LEFT JOIN labeling_session_assignments lsa ON lsa.session_id = ls.id
            LEFT JOIN expert_reviews r ON r.session_id = ls.id
            LEFT JOIN reviewer_adjudications ra ON ra.session_id = ls.id
            WHERE (%s = '' OR ls.status = %s)
            GROUP BY ls.id, ls.name, ls.description, ls.status, ls.created_at, ls.updated_at,
                     reviewer.full_name, creator.full_name
            ORDER BY ls.id DESC
            """,
            (normalized_status, normalized_status),
        )
        rows = cursor.fetchall()
        for row in rows:
            row["expectedReviews"] = int(row["document_count"] or 0) * 2
        return _paginate(rows, page, page_size)
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.patch("/api/admin/labeling-sessions/{session_id}/status")
def update_labeling_session_status(
    session_id: int,
    req: LabelingSessionStatusRequest,
    _: dict[str, Any] = Depends(_require_admin),
):
    status = req.status.strip().upper()
    if status not in {"OPEN", "CLOSED", "CANCELLED"}:
        raise HTTPException(422, "Trạng thái phiên không hợp lệ.")
    connection = cursor = None
    try:
        connection = _get_conn(); cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, status FROM labeling_sessions WHERE id = %s", (session_id,))
        current = cursor.fetchone()
        if not current:
            raise HTTPException(404, "Không tìm thấy phiên gán nhãn.")
        if current["status"] == "CANCELLED" and status != "CANCELLED":
            raise HTTPException(409, "Không thể mở lại phiên đã hủy.")
        cursor.execute("UPDATE labeling_sessions SET status = %s WHERE id = %s", (status, session_id))
        connection.commit()
        return {"message": "Đã cập nhật trạng thái phiên.", "sessionId": session_id, "status": status}
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
        logger.exception("AI label request failed")
        raise HTTPException(500, "Không thể hoàn tất gán nhãn AI lúc này.") from e


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
        logger.exception("Crawl log query failed")
        raise HTTPException(500, "Không thể tải nhật ký crawl lúc này.") from e
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
        logger.exception("Data verification failed")
        raise HTTPException(500, "Không thể kiểm chứng dữ liệu lúc này.") from e
    finally:
        if cursor: cursor.close()
        if conn:   conn.close()

import tempfile
from core.pdf_pipeline import ExtractorPipeline, PipelineError

@router.post("/api/extract-pdf")
async def extract_pdf_endpoint(file: UploadFile = File(...), relative_path: str = Form(""), _: dict[str, Any] = Depends(_require_admin)):
    """Extract title/authors/abstract and article sections from one PDF."""
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Chỉ hỗ trợ file PDF (.pdf)")
    if len(relative_path) > 1024:
        raise HTTPException(422, "Đường dẫn tương đối quá dài.")

    temp_dir = tempfile.mkdtemp()
    
    # Lấy tên file an toàn (tránh lỗi khi filename chứa đường dẫn thư mục con)
    safe_filename = os.path.basename(filename)[:255]
    if not safe_filename:
        safe_filename = "temp.pdf"
        
    temp_path = os.path.join(temp_dir, safe_filename)
    
    try:
        with open(temp_path, "wb") as buffer:
            total_bytes = 0
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_PDF_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="PDF vượt quá giới hạn 50 MB.")
                buffer.write(chunk)
            
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
        logger.exception("PDF extraction failed")
        raise HTTPException(500, "Không thể xử lý PDF lúc này.") from e
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        try:
            os.rmdir(temp_dir)
        except OSError:
            # Preserve the original pipeline/validation error if a dependency
            # left an auxiliary file in the temporary directory.
            pass

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
                rows.append((
                    req.article_id,
                    name,
                    ctype,
                    code,
                    c.get("concept_id") or code,
                    c.get("matched_by") or "exact",
                    c.get("confidence"),
                    dictionary_metadata.get("dictionary_version"),
                    c.get("start"),
                    c.get("end"),
                ))

        if rows:
            cursor.executemany(
                "INSERT INTO extracted_concepts "
                "(article_id, concept_name, concept_type, concept_code, concept_id, "
                "match_method, confidence_score, dictionary_version, concept_start, concept_end) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
    """Trả về các mục từ hai nguồn ICD-10 và YHCT đã xác thực."""
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        entries = []
        for source_key in ("icd10", "yhct"):
            source = manifest["files"][source_key]
            payload = json.loads((DICT_DIR / source["path"]).read_text(encoding="utf-8"))
            for entry in payload.get("entries", []):
                entries.append({
                    "term": entry.get("canonical_term") or entry.get("yhct_term") or "",
                    "canonical_term": entry.get("canonical_term") or entry.get("yhct_term") or "",
                    "type": entry.get("category") or "Đông Y / YHCT",
                    "code": entry.get("code") or "",
                    "active_for_ner": entry.get("active_for_ner", True),
                    "ambiguous": entry.get("ambiguous", False),
                    "source": source_key,
                })
        return entries
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


@router.post("/api/dictionary/alias-candidates/approve-suggestion")
def approve_dictionary_suggestion(
    req: AliasSuggestionApprovalRequest,
    user: dict[str, Any] = Depends(_require_admin),
):
    """Promote an explicit reviewer choice into the approved runtime alias set."""
    allowed_methods = {"fuzzy_candidate", "ambiguous", "grammar_variant", "accentless"}
    if req.match_method not in allowed_methods:
        raise HTTPException(422, "Phương pháp match của suggestion không hợp lệ.")
    concept = _dictionary_concept_for_identity(req.concept_id, req.concept_code)
    if not concept:
        raise HTTPException(422, "Concept ID không tồn tại trong dictionary hiện tại.")
    key = normalize_match_text(req.surface_form)
    if not key:
        raise HTTPException(422, "Surface form không hợp lệ sau khi chuẩn hóa.")
    requested_identity = {
        str(value or "").strip().casefold()
        for value in (req.concept_id, req.concept_code)
        if str(value or "").strip()
    }
    conflicting_values = [
        value
        for value in term_candidates.get(key, [])
        if not requested_identity.intersection({
            str(value.get("concept_id") or "").strip().casefold(),
            str(value.get("code") or "").strip().casefold(),
        })
    ]
    if conflicting_values:
        raise HTTPException(409, "Surface form đang xung đột với một concept khác.")
    if key in term_dict:
        return {
            "approved": True,
            "already_present": True,
            "concept_id": concept.get("concept_id") or req.concept_id,
            "dictionary_version": dictionary_metadata.get("dictionary_version"),
        }

    evidence = []
    if req.article_id is not None:
        evidence.append({
            "article_id": req.article_id,
            "start": req.start,
            "end": req.end,
            "text": req.surface_form,
            "context": req.context,
        })
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor()
        confidence = req.confidence_score
        cursor.execute(
            """
            INSERT INTO dictionary_alias_candidates
                (surface_form, concept_id, canonical_term, concept_code,
                 concept_type, alias_type, discovery_source, evidence_json,
                 match_method, confidence_score, uncertainty_score,
                 occurrence_count, document_frequency, context_json,
                 dictionary_version, status, reviewed_by, reviewed_at)
            VALUES (%s, %s, %s, %s, %s, 'approved_alias',
                    'manual_suggestion', %s, %s, %s, %s, 1, %s, %s,
                    %s, 'APPROVED', %s, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                concept_id = VALUES(concept_id),
                canonical_term = VALUES(canonical_term),
                concept_type = VALUES(concept_type),
                alias_type = 'approved_alias',
                discovery_source = 'manual_suggestion',
                evidence_json = VALUES(evidence_json),
                match_method = VALUES(match_method),
                confidence_score = VALUES(confidence_score),
                uncertainty_score = VALUES(uncertainty_score),
                occurrence_count = VALUES(occurrence_count),
                document_frequency = VALUES(document_frequency),
                context_json = VALUES(context_json),
                dictionary_version = VALUES(dictionary_version),
                status = 'APPROVED',
                reviewed_by = VALUES(reviewed_by),
                reviewed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                req.surface_form.strip(),
                concept.get("concept_id") or req.concept_id,
                req.canonical_term.strip(),
                concept.get("code") or req.concept_code,
                req.concept_type.strip() or concept.get("cat") or "Bệnh Lý",
                json.dumps(evidence, ensure_ascii=False),
                req.match_method,
                confidence,
                round(1 - confidence, 5) if confidence is not None else None,
                1 if req.article_id is not None else 0,
                json.dumps({"source": "manual_suggestion", "article_id": req.article_id}, ensure_ascii=False),
                dictionary_metadata.get("dictionary_version"),
                int(user["id"]),
            ),
        )
        connection.commit()
        _refresh_runtime_aliases()
        reset_ner_engine()
        return {
            "approved": True,
            "already_present": False,
            "surface_form": req.surface_form.strip(),
            "concept_id": concept.get("concept_id") or req.concept_id,
            "dictionary_version": dictionary_metadata.get("dictionary_version"),
        }
    except mysql.connector.Error as exc:
        if connection:
            connection.rollback()
        raise HTTPException(503, "Chưa có bảng alias candidate. Hãy chạy migration trong mysql.txt.") from exc
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@router.post("/api/dictionary/alias-candidates/discover")
def discover_dictionary_aliases(
    req: AliasDiscoveryRequest,
    _: dict[str, Any] = Depends(_require_admin),
):
    """Create pending alias candidates from conservative corpus variants."""
    connection = cursor = None
    try:
        candidates = _candidate_variant_rows(req.limit, include_context=req.include_context)
        if req.include_fuzzy_candidates:
            candidates.extend(_fuzzy_candidate_rows(req.limit, include_context=req.include_context))
        connection = _get_conn()
        cursor = connection.cursor()
        inserted = 0
        for item in candidates:
            cursor.execute(
                """
                INSERT INTO dictionary_alias_candidates
                    (surface_form, concept_id, canonical_term, concept_code,
                     concept_type, alias_type, discovery_source, evidence_json,
                     match_method, confidence_score, uncertainty_score,
                     occurrence_count, document_frequency, context_json,
                     dictionary_version, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING_REVIEW')
                ON DUPLICATE KEY UPDATE
                    evidence_json = VALUES(evidence_json),
                    confidence_score = VALUES(confidence_score),
                    uncertainty_score = VALUES(uncertainty_score),
                    occurrence_count = VALUES(occurrence_count),
                    document_frequency = VALUES(document_frequency),
                    context_json = VALUES(context_json),
                    dictionary_version = VALUES(dictionary_version),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    item["surface_form"], item["concept_id"], item["canonical_term"],
                    item["concept_code"], item["concept_type"], item["alias_type"],
                    item["discovery_source"], json.dumps(item["evidence"], ensure_ascii=False),
                    item.get("match_method") or item["alias_type"], item["confidence_score"], item["uncertainty_score"],
                    item["occurrence_count"], item["document_frequency"],
                    json.dumps({
                        "mi_score": item["mi_score"],
                        "boundary_score": item["boundary_score"],
                        "context_consistency": item["context_consistency"],
                        "density_score": item["density_score"],
                        "priority_score": item["priority_score"],
                    }, ensure_ascii=False),
                    dictionary_metadata.get("dictionary_version"),
                ),
            )
            inserted += cursor.rowcount > 0
        connection.commit()
        return {"discovered": len(candidates), "inserted_or_updated": inserted}
    except mysql.connector.Error as exc:
        logger.exception("Alias candidate discovery failed")
        raise HTTPException(503, "Chưa có bảng alias candidate. Hãy chạy migration trong mysql.txt.") from exc
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@router.get("/api/dictionary/alias-candidates")
def list_dictionary_aliases(
    status: str = "PENDING_REVIEW",
    match_method: str | None = None,
    concept_code: str | None = None,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    _: dict[str, Any] = Depends(_require_admin),
):
    allowed = {"PENDING_REVIEW", "APPROVED", "REJECTED"}
    normalized = status.strip().upper()
    if normalized not in allowed:
        raise HTTPException(422, "Trạng thái alias không hợp lệ.")
    if min_confidence is not None and not 0 <= min_confidence <= 1:
        raise HTTPException(422, "min_confidence phải nằm trong khoảng 0 đến 1.")
    if max_confidence is not None and not 0 <= max_confidence <= 1:
        raise HTTPException(422, "max_confidence phải nằm trong khoảng 0 đến 1.")
    if min_confidence is not None and max_confidence is not None and min_confidence > max_confidence:
        raise HTTPException(422, "min_confidence không được lớn hơn max_confidence.")
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        conditions = ["status = %s"]
        params: list[Any] = [normalized]
        if match_method and match_method.strip():
            conditions.append("match_method = %s")
            params.append(match_method.strip())
        if concept_code and concept_code.strip():
            conditions.append("concept_code = %s")
            params.append(concept_code.strip())
        if min_confidence is not None:
            conditions.append("confidence_score >= %s")
            params.append(min_confidence)
        if max_confidence is not None:
            conditions.append("confidence_score <= %s")
            params.append(max_confidence)
        cursor.execute(
            f"""
            SELECT id, surface_form, concept_id, canonical_term, concept_code,
                   concept_type, alias_type, discovery_source, evidence_json,
                   match_method, confidence_score, uncertainty_score,
                   occurrence_count, document_frequency, context_json,
                   dictionary_version,
                   status, reviewed_by, reviewed_at, created_at, updated_at
            FROM dictionary_alias_candidates
            WHERE {' AND '.join(conditions)}
            ORDER BY updated_at DESC, id DESC
            LIMIT 500
            """,
            tuple(params),
        )
        rows = cursor.fetchall()
        for row in rows:
            try:
                row["evidence"] = json.loads(row.pop("evidence_json") or "[]")
            except (TypeError, ValueError):
                row["evidence"] = []
            try:
                row["metrics"] = json.loads(row.pop("context_json") or "{}")
            except (TypeError, ValueError):
                row["metrics"] = {}
        return {"items": rows, "total": len(rows), "status": normalized}
    except mysql.connector.Error as exc:
        raise HTTPException(503, "Chưa có bảng alias candidate. Hãy chạy migration trong mysql.txt.") from exc
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@router.post("/api/dictionary/alias-candidates/{candidate_id}/approve")
def approve_dictionary_alias(candidate_id: int, user: dict[str, Any] = Depends(_require_admin)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM dictionary_alias_candidates WHERE id = %s FOR UPDATE",
            (candidate_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "Không tìm thấy alias candidate.")
        if row["status"] != "PENDING_REVIEW":
            raise HTTPException(409, "Alias candidate đã được xử lý.")
        concept = _dictionary_concept_for_identity(row.get("concept_id"), row.get("concept_code"))
        if not concept:
            raise HTTPException(422, "Concept ID không tồn tại trong dictionary hiện tại.")
        candidate_version = str(row.get("dictionary_version") or "").strip()
        current_version = str(dictionary_metadata.get("dictionary_version") or "").strip()
        if candidate_version and current_version and candidate_version != current_version:
            raise HTTPException(409, "Candidate được tạo từ phiên bản dictionary cũ.")
        key = normalize_match_text(row["surface_form"])
        existing_values = term_candidates.get(key, [])
        requested_identity = {
            str(value or "").strip().casefold()
            for value in (row.get("concept_id"), row.get("concept_code"))
            if str(value or "").strip()
        }
        conflicting_values = [
            value
            for value in existing_values
            if not requested_identity.intersection({
                str(value.get("concept_id") or "").strip().casefold(),
                str(value.get("code") or "").strip().casefold(),
            })
        ]
        if conflicting_values:
            raise HTTPException(409, "Alias đang xung đột với một concept khác.")
        cursor.execute(
            """
            UPDATE dictionary_alias_candidates
            SET status = 'APPROVED', reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (int(user["id"]), candidate_id),
        )
        connection.commit()
        _refresh_runtime_aliases()
        reset_ner_engine()
        return {"approved": True, "candidate_id": candidate_id, "dictionary_version": dictionary_metadata.get("dictionary_version")}
    except HTTPException:
        if connection:
            connection.rollback()
        raise
    except mysql.connector.Error as exc:
        if connection:
            connection.rollback()
        raise HTTPException(503, "Chưa có bảng alias candidate. Hãy chạy migration trong mysql.txt.") from exc
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@router.post("/api/dictionary/alias-candidates/{candidate_id}/reject")
def reject_dictionary_alias(candidate_id: int, user: dict[str, Any] = Depends(_require_admin)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE dictionary_alias_candidates
            SET status = 'REJECTED', reviewed_by = %s, reviewed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'PENDING_REVIEW'
            """,
            (int(user["id"]), candidate_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(404, "Alias candidate không tồn tại hoặc đã được xử lý.")
        connection.commit()
        return {"rejected": True, "candidate_id": candidate_id}
    except HTTPException:
        if connection:
            connection.rollback()
        raise
    except mysql.connector.Error as exc:
        if connection:
            connection.rollback()
        raise HTTPException(503, "Chưa có bảng alias candidate. Hãy chạy migration trong mysql.txt.") from exc
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@router.post("/api/save-to-dictionary")
def save_to_dictionary_endpoint(req: SaveDictionaryRequest, _: dict[str, Any] = Depends(_require_admin)):
    """Disabled: NER chỉ sử dụng hai từ điển được xác thực."""
    raise HTTPException(
        status_code=410,
        detail="Hệ thống chỉ sử dụng từ điển ICD-10 và YHCT; không cho thêm thuật ngữ tùy chỉnh.",
    )


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
        SELECT concept_name, concept_type, concept_code, concept_id,
               match_method, confidence_score, dictionary_version
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
            "concept_id": row.get("concept_id") or row["concept_code"],
            "matched_by": row.get("match_method") or "exact",
            "confidence": row.get("confidence_score"),
            "dictionary_version": row.get("dictionary_version"),
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
               concept_id, match_method, confidence_score, dictionary_version,
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


def _assigned_expert_session(cursor, document_id: int, expert_id: int, *, require_open: bool = False) -> dict[str, Any]:
    cursor.execute("SELECT id FROM articles WHERE id = %s", (document_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy văn bản id={document_id}")
    cursor.execute(
        """
        SELECT ls.id, ls.name, ls.status
        FROM labeling_session_assignments lsa
        JOIN labeling_sessions ls ON ls.id = lsa.session_id
        WHERE lsa.article_id = %s AND lsa.expert_id = %s
          AND (%s = 0 OR ls.status = 'OPEN')
        ORDER BY (ls.status = 'OPEN') DESC, ls.id DESC
        LIMIT 1
        """,
        (document_id, expert_id, int(require_open)),
    )
    session = cursor.fetchone()
    if not session:
        raise HTTPException(status_code=403, detail="Văn bản chưa được phân công cho Expert trong phiên phù hợp.")
    return session


def _assigned_reviewer_session(cursor, document_id: int, reviewer_id: int, *, require_open: bool = False) -> dict[str, Any]:
    cursor.execute("SELECT id FROM articles WHERE id = %s", (document_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy văn bản id={document_id}")
    cursor.execute(
        """
        SELECT DISTINCT ls.id, ls.name, ls.status
        FROM labeling_sessions ls
        JOIN labeling_session_assignments lsa ON lsa.session_id = ls.id
        WHERE lsa.article_id = %s AND ls.reviewer_id = %s
          AND (%s = 0 OR ls.status = 'OPEN')
        ORDER BY (ls.status = 'OPEN') DESC, ls.id DESC
        LIMIT 1
        """,
        (document_id, reviewer_id, int(require_open)),
    )
    session = cursor.fetchone()
    if not session:
        raise HTTPException(status_code=403, detail="Văn bản chưa được phân công cho Reviewer trong phiên phù hợp.")
    return session


def _review_history(
    cursor,
    document_id: int,
    expert_id: int | None = None,
    session_id: int | None = None,
) -> list[dict[str, Any]]:
    sql = """
        SELECT r.id, r.session_id, r.review_status, r.label_source, r.original_labels_json,
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
    if session_id is not None:
        sql += " AND r.session_id = %s"
        params.append(session_id)
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


def _case_resolution_status(cursor, document_id: int, session_id: int) -> str:
    cursor.execute(
        """
        SELECT COUNT(*) AS expert_count
        FROM (
            SELECT expert_id
            FROM expert_reviews
            WHERE document_id = %s AND session_id = %s
            GROUP BY expert_id
        ) experts
        """,
        (document_id, session_id),
    )
    if int(cursor.fetchone()["expert_count"] or 0) < 2:
        return "PENDING"
    cursor.execute(
        """
        SELECT resolution_status
        FROM reviewer_adjudications
        WHERE document_id = %s AND session_id = %s
        ORDER BY id DESC LIMIT 1
        """,
        (document_id, session_id),
    )
    adjudication = cursor.fetchone()
    return str(adjudication["resolution_status"]).upper() if adjudication else "CONFLICT"


@router.get("/api/expert/dashboard")
def expert_dashboard(user: dict[str, Any] = Depends(_require_expert)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT COUNT(DISTINCT lsa.article_id) AS value
            FROM labeling_session_assignments lsa
            JOIN labeling_sessions ls ON ls.id = lsa.session_id AND ls.status = 'OPEN'
            JOIN extracted_concepts ec ON ec.article_id = lsa.article_id AND ec.concept_code <> ''
            WHERE lsa.expert_id = %s
            """,
            (user["id"],),
        )
        icd_count = cursor.fetchone()["value"]
        cursor.execute(
            """
            SELECT COUNT(DISTINCT lsa.article_id) AS value
            FROM labeling_session_assignments lsa
            JOIN labeling_sessions ls ON ls.id = lsa.session_id AND ls.status = 'OPEN'
            JOIN ai_document_labels ai ON ai.article_id = lsa.article_id
            WHERE lsa.expert_id = %s
            """,
            (user["id"],),
        )
        ai_count = cursor.fetchone()["value"]
        cursor.execute(
            "SELECT COUNT(DISTINCT document_id, session_id) AS value FROM expert_reviews WHERE expert_id = %s AND session_id IS NOT NULL",
            (user["id"],),
        )
        reviewed = cursor.fetchone()["value"]
        cursor.execute(
            """
            SELECT COUNT(DISTINCT lsa.article_id) AS value
            FROM labeling_session_assignments lsa
            JOIN labeling_sessions ls ON ls.id = lsa.session_id AND ls.status = 'OPEN'
            LEFT JOIN expert_reviews r
              ON r.session_id = lsa.session_id AND r.document_id = lsa.article_id AND r.expert_id = lsa.expert_id
            WHERE lsa.expert_id = %s AND r.id IS NULL
            """,
            (user["id"],),
        )
        pending = cursor.fetchone()["value"]
        cursor.execute(
            """
            SELECT COUNT(DISTINCT lsa.session_id) AS value
            FROM labeling_session_assignments lsa
            JOIN labeling_sessions ls ON ls.id = lsa.session_id AND ls.status = 'OPEN'
            WHERE lsa.expert_id = %s
            """,
            (user["id"],),
        )
        active_sessions = cursor.fetchone()["value"]
        return {
            "icd10LabeledDocuments": icd_count,
            "aiLabeledDocuments": ai_count,
            "reviewed": reviewed,
            "pendingReview": pending,
            "activeSessions": active_sessions,
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
                   ls.id AS session_id, ls.name AS session_name,
                   GROUP_CONCAT(DISTINCT ec.concept_code ORDER BY ec.concept_code SEPARATOR ', ') AS icd10_codes,
                   GROUP_CONCAT(DISTINCT ec.concept_name ORDER BY ec.concept_name SEPARATOR ' | ') AS icd10_labels,
                   latest.review_status AS latest_review_status
            FROM articles a
            JOIN labeling_session_assignments lsa ON lsa.article_id = a.id AND lsa.expert_id = %s
            JOIN labeling_sessions ls ON ls.id = lsa.session_id AND ls.status = 'OPEN'
            JOIN extracted_concepts ec ON ec.article_id = a.id AND ec.concept_code <> ''
            LEFT JOIN expert_reviews latest ON latest.id = (
                SELECT MAX(r.id) FROM expert_reviews r
                WHERE r.document_id = a.id AND r.expert_id = %s AND r.session_id = ls.id
            )
            WHERE (%s = '' OR a.title LIKE %s OR a.authors LIKE %s)
              AND (%s = '' OR ec.concept_code LIKE %s)
            GROUP BY a.id, a.title, a.authors, a.publication_year, ls.id, ls.name, latest.review_status, latest.id
            ORDER BY a.id DESC
        """
        params = (user["id"], user["id"], q, f"%{q}%", f"%{q}%", icd, f"%{icd}%")
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
                   ls.id AS session_id, ls.name AS session_name,
                   ai.primary_icd10_code, ai.primary_icd10_label, ai.confidence, ai.created_at AS ai_labeled_at,
                   latest.review_status AS latest_review_status
            FROM articles a
            JOIN labeling_session_assignments lsa ON lsa.article_id = a.id AND lsa.expert_id = %s
            JOIN labeling_sessions ls ON ls.id = lsa.session_id AND ls.status = 'OPEN'
            JOIN ai_document_labels ai ON ai.id = (
                SELECT MAX(ai2.id) FROM ai_document_labels ai2 WHERE ai2.article_id = a.id
            )
            LEFT JOIN expert_reviews latest ON latest.id = (
                SELECT MAX(r.id) FROM expert_reviews r
                WHERE r.document_id = a.id AND r.expert_id = %s AND r.session_id = ls.id
            )
            WHERE (%s = '' OR a.title LIKE %s OR a.authors LIKE %s)
            ORDER BY a.id DESC
            """,
            (user["id"], user["id"], q, f"%{q}%", f"%{q}%"),
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
            SELECT a.id, a.title, r.session_id, ls.name AS session_name,
                   r.review_status, r.suggested_icd10_code, r.suggested_icd10_label,
                   r.comment, r.created_at AS reviewed_at
            FROM articles a JOIN expert_reviews r ON r.id = (
                SELECT MAX(r2.id) FROM expert_reviews r2
                WHERE r2.document_id = a.id AND r2.expert_id = %s
            )
            JOIN labeling_sessions ls ON ls.id = r.session_id
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
        session = _assigned_expert_session(cursor, document_id, int(user["id"]))
        cursor.execute(
            "SELECT id, title, authors, abstract, publication_year, source_url FROM articles WHERE id = %s",
            (document_id,),
        )
        article = cursor.fetchone()
        article["currentLabels"] = _document_labels(cursor, document_id)
        article["aiLabel"] = _latest_ai_label(cursor, document_id)
        article["aiLabels"] = _all_ai_labels(cursor, document_id)
        article["session"] = session
        article["reviewHistory"] = _review_history(cursor, document_id, int(user["id"]), int(session["id"]))
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
        session = _assigned_expert_session(cursor, document_id, int(user["id"]))
        return {"items": _review_history(cursor, document_id, int(user["id"]), int(session["id"]))}
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
        session = _assigned_expert_session(cursor, document_id, int(user["id"]), require_open=True)
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
            (document_id, session_id, expert_id, review_status, label_source, original_labels_json,
             annotations_json, suggested_icd10_code, suggested_icd10_label, comment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                document_id, session["id"], user["id"], status, label_source,
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
def reviewer_dashboard(user: dict[str, Any] = Depends(_require_reviewer)):
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT COUNT(DISTINCT ls.id) AS active_sessions,
                   COUNT(DISTINCT lsa.article_id) AS assigned_documents,
                   COUNT(DISTINCT lsa.expert_id) AS assigned_experts
            FROM labeling_sessions ls
            LEFT JOIN labeling_session_assignments lsa ON lsa.session_id = ls.id
            WHERE ls.reviewer_id = %s AND ls.status = 'OPEN'
            """,
            (user["id"],),
        )
        scope = cursor.fetchone()
        cursor.execute(
            """
            SELECT COUNT(DISTINCT CONCAT(r.document_id, ':', r.expert_id)) AS submitted_reviews
            FROM expert_reviews r
            JOIN labeling_sessions ls ON ls.id = r.session_id
            WHERE ls.reviewer_id = %s
            """,
            (user["id"],),
        )
        submitted_reviews = cursor.fetchone()["submitted_reviews"]
        cursor.execute(
            """
            SELECT COUNT(DISTINCT CASE WHEN ra.resolution_status = 'RESOLVED' THEN ra.document_id END) AS resolved,
                   COUNT(DISTINCT CASE WHEN ra.resolution_status <> 'RESOLVED' THEN ra.document_id END) AS conflicts
            FROM reviewer_adjudications ra
            JOIN labeling_sessions ls ON ls.id = ra.session_id
            WHERE ls.reviewer_id = %s
            """,
            (user["id"],),
        )
        decisions = cursor.fetchone()
        expected_reviews = int(scope["assigned_documents"] or 0) * 2
        return {
            "totalReviews": submitted_reviews,
            "activeExperts": scope["assigned_experts"],
            "reviewedDocuments": scope["assigned_documents"],
            "activeSessions": scope["active_sessions"],
            "expectedReviews": expected_reviews,
            "pendingReviews": max(0, expected_reviews - int(submitted_reviews or 0)),
            "resolvedDocuments": decisions["resolved"],
            "conflictDocuments": decisions["conflicts"],
        }
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.get("/api/reviewer/reviews")
def reviewer_reviews(
    q: str = "", review_status: str = "", page: int = 1, page_size: int = 50,
    user: dict[str, Any] = Depends(_require_reviewer),
):
    normalized_status = review_status.strip().upper()
    if normalized_status and normalized_status not in {"PENDING", "CONFLICT", "RESOLVED"}:
        raise HTTPException(422, "Trạng thái case không hợp lệ.")
    connection = cursor = None
    try:
        connection = _get_conn()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT ls.id AS session_id, ls.name AS session_name, ls.status AS session_status,
                   a.id AS document_id, a.title AS document_title,
                   COUNT(DISTINCT latest.expert_id) AS expert_count,
                   GROUP_CONCAT(DISTINCT u.full_name ORDER BY u.full_name SEPARATOR ' · ') AS expert_names,
                   CASE
                     WHEN COUNT(DISTINCT latest.expert_id) < 2 THEN 'PENDING'
                     WHEN MAX(CASE WHEN ra.resolution_status = 'RESOLVED' THEN 1 ELSE 0 END) = 1 THEN 'RESOLVED'
                     ELSE 'CONFLICT'
                   END AS case_status,
                   MAX(latest.created_at) AS updated_at
            FROM labeling_sessions ls
            JOIN labeling_session_assignments lsa ON lsa.session_id = ls.id
            JOIN articles a ON a.id = lsa.article_id
            LEFT JOIN expert_reviews latest ON latest.id = (
                SELECT MAX(r2.id) FROM expert_reviews r2
                WHERE r2.session_id = ls.id AND r2.document_id = a.id AND r2.expert_id = lsa.expert_id
            )
            LEFT JOIN users u ON u.id = latest.expert_id
            LEFT JOIN reviewer_adjudications ra ON ra.id = (
                SELECT MAX(ra2.id) FROM reviewer_adjudications ra2
                WHERE ra2.session_id = ls.id AND ra2.document_id = a.id
            )
            WHERE ls.reviewer_id = %s
              AND (%s = '' OR a.title LIKE %s OR ls.name LIKE %s OR u.full_name LIKE %s)
            GROUP BY ls.id, ls.name, ls.status, a.id, a.title
            ORDER BY ls.id DESC, a.id DESC
            """,
            (user["id"], q, f"%{q}%", f"%{q}%", f"%{q}%"),
        )
        rows = cursor.fetchall()
        if normalized_status:
            rows = [row for row in rows if row["case_status"] == normalized_status]
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
        session = _assigned_reviewer_session(cursor, document_id, int(user["id"]))
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
        article["session"] = session
        article["reviewHistory"] = _review_history(cursor, document_id, session_id=int(session["id"]))
        article["resolutionStatus"] = _case_resolution_status(cursor, document_id, int(session["id"]))
        cursor.execute(
            """
            SELECT id, decision_payload, note, created_at
                   , final_labels_json, resolution_status
            FROM reviewer_adjudications
            WHERE document_id = %s AND reviewer_id = %s AND session_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (document_id, user["id"], session["id"]),
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
        session = _assigned_reviewer_session(cursor, document_id, int(user["id"]), require_open=True)
        cursor.execute(
            """
            SELECT r.id, r.expert_id
            FROM expert_reviews r
            JOIN labeling_session_assignments lsa
              ON lsa.session_id = r.session_id AND lsa.article_id = r.document_id AND lsa.expert_id = r.expert_id
            WHERE r.session_id = %s AND r.document_id = %s
              AND r.id = (
                SELECT MAX(r2.id) FROM expert_reviews r2
                WHERE r2.session_id = r.session_id AND r2.document_id = r.document_id AND r2.expert_id = r.expert_id
              )
            ORDER BY r.expert_id
            """,
            (session["id"], document_id),
        )
        expert_reviews = cursor.fetchall()
        if len(expert_reviews) != 2 or len({row["expert_id"] for row in expert_reviews}) != 2:
            raise HTTPException(409, "Cần đủ review mới nhất của hai Expert khác nhau trước khi phân xử.")
        cursor.execute("SELECT abstract FROM articles WHERE id = %s", (document_id,))
        article_text = (cursor.fetchone() or {}).get("abstract") or ""
        final_labels = _normalize_annotations(req.final_labels, article_text)
        for item in final_labels:
            if item["code"] and item["code"].casefold() not in _icd_code_catalog():
                raise HTTPException(status_code=422, detail=f"Mã nhãn không tồn tại trong controlled vocabulary: {item['code']}")
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
                (document_id, session_id, reviewer_id, expert_review_a_id, expert_review_b_id,
                 decision_payload, final_labels_json, resolution_status, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                document_id,
                session["id"],
                user["id"],
                expert_reviews[0]["id"],
                expert_reviews[1]["id"],
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
        session = _assigned_reviewer_session(cursor, document_id, int(user["id"]))
        cursor.execute(
            """
            SELECT id, decision_payload, final_labels_json, resolution_status, note, created_at
            FROM reviewer_adjudications
            WHERE document_id = %s AND reviewer_id = %s AND session_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (document_id, user["id"], session["id"]),
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
    """Create a Reviewer account from the Admin workspace."""
    if req.password != req.confirm_password:
        raise HTTPException(status_code=422, detail="Xác nhận mật khẩu không khớp.")
    try:
        user = register_reviewer(_db_config, req.full_name, req.email, req.password)
        return {"message": "Đã tạo tài khoản Reviewer thành công.", "user": user}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.patch("/api/admin/users/{user_id}/status")
def update_user_status(
    user_id: int,
    req: UserStatusRequest,
    admin: dict[str, Any] = Depends(_require_admin),
):
    if int(admin["id"]) == user_id and not req.is_active:
        raise HTTPException(422, "Không thể tự khóa tài khoản đang đăng nhập.")
    connection = cursor = None
    try:
        connection = _get_conn(); cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, role, is_active FROM users WHERE id = %s", (user_id,))
        target = cursor.fetchone()
        if not target:
            raise HTTPException(404, "Không tìm thấy tài khoản.")
        if str(target["role"]).upper() == ROLE_ADMIN:
            raise HTTPException(403, "Không quản lý tài khoản Admin từ màn hình này.")
        cursor.execute("UPDATE users SET is_active = %s WHERE id = %s", (int(req.is_active), user_id))
        if not req.is_active:
            cursor.execute(
                "UPDATE user_sessions SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
        connection.commit()
        return {"message": "Đã cập nhật trạng thái tài khoản.", "userId": user_id, "isActive": req.is_active}
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


@router.post("/api/admin/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    req: ResetPasswordRequest,
    _: dict[str, Any] = Depends(_require_admin),
):
    if req.password != req.confirm_password:
        raise HTTPException(422, "Xác nhận mật khẩu không khớp.")
    try:
        password_hash = hash_password(req.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    connection = cursor = None
    try:
        connection = _get_conn(); cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, role FROM users WHERE id = %s", (user_id,))
        target = cursor.fetchone()
        if not target:
            raise HTTPException(404, "Không tìm thấy tài khoản.")
        if str(target["role"]).upper() == ROLE_ADMIN:
            raise HTTPException(403, "Không đặt lại mật khẩu Admin từ màn hình này.")
        cursor.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
        cursor.execute(
            "UPDATE user_sessions SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
        connection.commit()
        return {"message": "Đã đặt lại mật khẩu và thu hồi các phiên đăng nhập.", "userId": user_id}
    finally:
        if cursor: cursor.close()
        if connection: connection.close()


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
def get_status(_: dict[str, Any] = Depends(_require_admin)):
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
    with _scrape_start_lock:
        if scrape_status["running"]:
            raise HTTPException(409, "Đang có tác vụ chạy, vui lòng đợi!")
        # Set the guard before starting the thread; otherwise two requests
        # arriving in the same scheduling window can both pass the check.
        scrape_status["running"] = True
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
