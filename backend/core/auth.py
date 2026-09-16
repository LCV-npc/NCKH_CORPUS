"""Authentication primitives and idempotent MySQL schema for corpus review.

The project uses direct MySQL access rather than an ORM.  This module keeps
password hashing, opaque sessions and schema setup in one place so API routes
never need to handle a plaintext password or a raw session token directly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

import mysql.connector
from fastapi import HTTPException

from config.env import load_backend_env


load_backend_env()

ROLE_ADMIN = "ADMIN"
ROLE_EXPERT = "EXPERT"
ROLE_REVIEWER = "REVIEWER"
PASSWORD_MIN_LENGTH = 10
PBKDF2_ITERATIONS = 600_000
SESSION_HOURS = int(os.getenv("AUTH_SESSION_HOURS", "12"))
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def ensure_auth_review_schema(db_config: dict[str, Any]) -> None:
    """Create review/auth tables without changing existing corpus tables."""
    connection = cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                full_name VARCHAR(200) NOT NULL,
                email VARCHAR(254) NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(16) NOT NULL DEFAULT 'EXPERT',
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_users_email (email),
                KEY idx_users_role (role),
                CONSTRAINT chk_users_role CHECK (role IN ('ADMIN', 'EXPERT', 'REVIEWER'))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        # Migrate databases created before the Reviewer role was introduced.
        try:
            cursor.execute("ALTER TABLE users DROP CHECK chk_users_role")
        except mysql.connector.Error:
            pass
        try:
            cursor.execute(
                "ALTER TABLE users ADD CONSTRAINT chk_users_role "
                "CHECK (role IN ('ADMIN', 'EXPERT', 'REVIEWER'))"
            )
        except mysql.connector.Error:
            pass
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sessions (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                user_id BIGINT UNSIGNED NOT NULL,
                token_hash CHAR(64) NOT NULL,
                expires_at DATETIME NOT NULL,
                last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                revoked_at DATETIME NULL DEFAULT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_user_sessions_token_hash (token_hash),
                KEY idx_user_sessions_user (user_id),
                KEY idx_user_sessions_expiry (expires_at),
                CONSTRAINT fk_user_sessions_user FOREIGN KEY (user_id)
                    REFERENCES users(id) ON UPDATE CASCADE ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_document_labels (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                article_id INT UNSIGNED NOT NULL,
                model_name VARCHAR(120) NOT NULL,
                label_payload LONGTEXT NOT NULL,
                primary_icd10_code VARCHAR(100) NULL,
                primary_icd10_label VARCHAR(500) NULL,
                confidence DECIMAL(5,4) NULL,
                generated_by BIGINT UNSIGNED NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_ai_labels_article (article_id, id),
                KEY idx_ai_labels_primary_code (primary_icd10_code),
                CONSTRAINT fk_ai_labels_article FOREIGN KEY (article_id)
                    REFERENCES articles(id) ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT fk_ai_labels_user FOREIGN KEY (generated_by)
                    REFERENCES users(id) ON UPDATE CASCADE ON DELETE SET NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS expert_reviews (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                document_id INT UNSIGNED NOT NULL,
                expert_id BIGINT UNSIGNED NOT NULL,
                review_status VARCHAR(32) NOT NULL,
                label_source VARCHAR(10) NOT NULL DEFAULT 'icd10',
                original_labels_json LONGTEXT NOT NULL,
                suggested_icd10_code VARCHAR(100) NULL,
                suggested_icd10_label VARCHAR(500) NULL,
                comment TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_reviews_document (document_id, id),
                KEY idx_reviews_expert (expert_id, id),
                KEY idx_reviews_status (review_status),
                CONSTRAINT fk_reviews_article FOREIGN KEY (document_id)
                    REFERENCES articles(id) ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT fk_reviews_expert FOREIGN KEY (expert_id)
                    REFERENCES users(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                CONSTRAINT chk_reviews_status CHECK (
                    review_status IN ('CORRECT', 'INCORRECT', 'NEEDS_REVISION')
                )
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        # CREATE TABLE IF NOT EXISTS does not update an older table. Keep the
        # runtime migration here so deployments created before label_source was
        # introduced can still save expert reviews.
        cursor.execute("SHOW COLUMNS FROM expert_reviews LIKE 'label_source'")
        if cursor.fetchone() is None:
            cursor.execute(
                "ALTER TABLE expert_reviews "
                "ADD COLUMN label_source VARCHAR(10) NOT NULL DEFAULT 'icd10' "
                "COMMENT 'icd10 = NER dictionary, ai = AI label' "
                "AFTER review_status"
            )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reviewer_adjudications (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                document_id INT UNSIGNED NOT NULL,
                reviewer_id BIGINT UNSIGNED NOT NULL,
                expert_review_a_id BIGINT UNSIGNED NOT NULL,
                expert_review_b_id BIGINT UNSIGNED NOT NULL,
                decision_payload LONGTEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_adjudications_review_pair
                    (document_id, reviewer_id, expert_review_a_id, expert_review_b_id),
                KEY idx_adjudications_document (document_id, id),
                KEY idx_adjudications_reviewer (reviewer_id, id),
                KEY idx_adjudications_review_a (expert_review_a_id),
                KEY idx_adjudications_review_b (expert_review_b_id),
                CONSTRAINT fk_adjudications_article FOREIGN KEY (document_id)
                    REFERENCES articles(id) ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT fk_adjudications_reviewer FOREIGN KEY (reviewer_id)
                    REFERENCES users(id) ON UPDATE CASCADE ON DELETE RESTRICT,
                CONSTRAINT fk_adjudications_review_a FOREIGN KEY (expert_review_a_id)
                    REFERENCES expert_reviews(id) ON UPDATE CASCADE ON DELETE CASCADE,
                CONSTRAINT fk_adjudications_review_b FOREIGN KEY (expert_review_b_id)
                    REFERENCES expert_reviews(id) ON UPDATE CASCADE ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )

        # Preserve legacy adjudications while adding explicit references to the
        # two expert-review rows used by every new reviewer decision.
        reviewer_columns = {
            "expert_review_a_id": (
                "ALTER TABLE reviewer_adjudications "
                "ADD COLUMN expert_review_a_id BIGINT UNSIGNED NULL AFTER reviewer_id"
            ),
            "expert_review_b_id": (
                "ALTER TABLE reviewer_adjudications "
                "ADD COLUMN expert_review_b_id BIGINT UNSIGNED NULL AFTER expert_review_a_id"
            ),
            "updated_at": (
                "ALTER TABLE reviewer_adjudications "
                "ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP "
                "ON UPDATE CURRENT_TIMESTAMP AFTER created_at"
            ),
        }
        for column_name, statement in reviewer_columns.items():
            cursor.execute(f"SHOW COLUMNS FROM reviewer_adjudications LIKE '{column_name}'")
            if cursor.fetchone() is None:
                cursor.execute(statement)

        cursor.execute(
            "SHOW INDEX FROM reviewer_adjudications "
            "WHERE Key_name = 'uq_adjudications_review_pair'"
        )
        if not cursor.fetchall():
            cursor.execute(
                "ALTER TABLE reviewer_adjudications "
                "ADD UNIQUE KEY uq_adjudications_review_pair "
                "(document_id, reviewer_id, expert_review_a_id, expert_review_b_id)"
            )

        for constraint_name, column_name in (
            ("fk_adjudications_review_a", "expert_review_a_id"),
            ("fk_adjudications_review_b", "expert_review_b_id"),
        ):
            cursor.execute(
                "SELECT CONSTRAINT_NAME FROM information_schema.REFERENTIAL_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'reviewer_adjudications' "
                "AND CONSTRAINT_NAME = %s",
                (constraint_name,),
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    f"ALTER TABLE reviewer_adjudications ADD CONSTRAINT {constraint_name} "
                    f"FOREIGN KEY ({column_name}) REFERENCES expert_reviews(id) "
                    "ON UPDATE CASCADE ON DELETE CASCADE"
                )
        connection.commit()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def normalize_email(email: str) -> str:
    normalized = str(email or "").strip().casefold()
    if len(normalized) > 254 or not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("Email không hợp lệ.")
    return normalized


def validate_password(password: str) -> str:
    value = str(password or "")
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Mật khẩu cần tối thiểu {PASSWORD_MIN_LENGTH} ký tự.")
    if len(value) > 256:
        raise ValueError("Mật khẩu quá dài.")
    return value


def hash_password(password: str) -> str:
    password_bytes = validate_password(password).encode("utf-8")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iteration_text, salt_text, digest_text = str(stored_hash).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iteration_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def public_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["full_name"],
        "email": row["email"],
        "role": str(row["role"]).lower(),
    }


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register_user(
    db_config: dict[str, Any],
    full_name: str,
    email: str,
    password: str,
    role: str,
) -> dict[str, Any]:
    name = str(full_name or "").strip()
    if not 2 <= len(name) <= 200:
        raise ValueError("Họ và tên phải có từ 2 đến 200 ký tự.")
    normalized_role = str(role or "").strip().upper()
    if normalized_role not in {ROLE_EXPERT, ROLE_REVIEWER}:
        raise ValueError("Vai trò tài khoản không hợp lệ.")
    normalized_email = normalize_email(email)
    password_hash = hash_password(password)
    connection = cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "INSERT INTO users (full_name, email, password_hash, role) VALUES (%s, %s, %s, %s)",
            (name, normalized_email, password_hash, normalized_role),
        )
        user_id = cursor.lastrowid
        connection.commit()
        return {"id": int(user_id), "name": name, "email": normalized_email, "role": normalized_role.lower()}
    except mysql.connector.IntegrityError as exc:
        if getattr(exc, "errno", None) == 1062:
            raise ValueError("Email đã được sử dụng.") from exc
        raise
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def register_expert(db_config: dict[str, Any], full_name: str, email: str, password: str) -> dict[str, Any]:
    return register_user(db_config, full_name, email, password, ROLE_EXPERT)


def register_reviewer(db_config: dict[str, Any], full_name: str, email: str, password: str) -> dict[str, Any]:
    return register_user(db_config, full_name, email, password, ROLE_REVIEWER)


def authenticate(db_config: dict[str, Any], email: str, password: str) -> tuple[dict[str, Any], str, datetime]:
    normalized_email = normalize_email(email)
    connection = cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, full_name, email, password_hash, role, is_active FROM users WHERE email = %s",
            (normalized_email,),
        )
        row = cursor.fetchone()
        if not row or not row["is_active"] or not verify_password(password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Email hoặc mật khẩu không đúng.")

        token = secrets.token_urlsafe(48)
        expires_at = datetime.now() + timedelta(hours=SESSION_HOURS)
        cursor.execute(
            "INSERT INTO user_sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
            (row["id"], _token_hash(token), expires_at),
        )
        connection.commit()
        return public_user(row), token, expires_at
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def get_session_user(db_config: dict[str, Any], authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Cần đăng nhập để truy cập tài nguyên này.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Phiên đăng nhập không hợp lệ.")

    connection = cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT u.id, u.full_name, u.email, u.role
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.revoked_at IS NULL
              AND s.expires_at > NOW() AND u.is_active = 1
            """,
            (_token_hash(token),),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Phiên đăng nhập đã hết hạn hoặc không hợp lệ.")
        cursor.execute("UPDATE user_sessions SET last_seen_at = NOW() WHERE token_hash = %s", (_token_hash(token),))
        connection.commit()
        return public_user(row)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def revoke_session(db_config: dict[str, Any], authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        return
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return
    connection = cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()
        cursor.execute("UPDATE user_sessions SET revoked_at = NOW() WHERE token_hash = %s", (_token_hash(token),))
        connection.commit()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def create_or_update_admin(db_config: dict[str, Any], full_name: str, email: str, password: str) -> dict[str, Any]:
    """Explicit seed helper; registration itself never creates an admin."""
    name = str(full_name or "").strip()
    if not 2 <= len(name) <= 200:
        raise ValueError("Họ và tên phải có từ 2 đến 200 ký tự.")
    normalized_email = normalize_email(email)
    password_hash = hash_password(password)
    connection = cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id FROM users WHERE email = %s", (normalized_email,))
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                "UPDATE users SET full_name=%s, password_hash=%s, role=%s, is_active=1 WHERE id=%s",
                (name, password_hash, ROLE_ADMIN, existing["id"]),
            )
            user_id = existing["id"]
        else:
            cursor.execute(
                "INSERT INTO users (full_name, email, password_hash, role) VALUES (%s, %s, %s, %s)",
                (name, normalized_email, password_hash, ROLE_ADMIN),
            )
            user_id = cursor.lastrowid
        connection.commit()
        return {"id": int(user_id), "name": name, "email": normalized_email, "role": "admin"}
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
