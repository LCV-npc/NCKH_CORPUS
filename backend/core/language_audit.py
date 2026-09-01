"""Persistence and filesystem audit helpers for corpus language decisions."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import mysql.connector

from config.language_filter import VietnameseCorpusSettings
from core.language_validation import LANGUAGE_VALIDATION_VERSION, AdmissionDecision, stable_file_hash


def ensure_language_audit_schema(db_config: dict) -> None:
    """Create the standalone audit table without changing the legacy articles table."""
    connection = cursor = None
    try:
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS corpus_language_audit (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                article_id INT UNSIGNED NULL,
                source_url VARCHAR(512) NOT NULL,
                source_domain VARCHAR(255) NOT NULL DEFAULT '',
                pdf_url VARCHAR(1024) NULL,
                article_title VARCHAR(500) NULL,
                decision_status VARCHAR(40) NOT NULL,
                rejection_reason VARCHAR(160) NOT NULL DEFAULT '',
                metadata_language VARCHAR(20) NOT NULL DEFAULT 'unknown',
                metadata_confidence DECIMAL(6,4) NULL,
                pdf_language VARCHAR(20) NULL,
                pdf_confidence DECIMAL(6,4) NULL,
                vietnamese_ratio DECIMAL(6,4) NULL,
                english_ratio DECIMAL(6,4) NULL,
                assessed_characters INT UNSIGNED NULL,
                file_path VARCHAR(1024) NULL,
                file_hash_sha256 CHAR(64) NULL,
                validation_version VARCHAR(20) NOT NULL DEFAULT '1',
                evidence_json LONGTEXT NOT NULL,
                first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uq_corpus_language_audit_source (source_url),
                KEY idx_corpus_language_audit_status (decision_status),
                KEY idx_corpus_language_audit_article (article_id),
                KEY idx_corpus_language_audit_hash (file_hash_sha256)
            ) ENGINE=InnoDB DEFAULT CHARACTER SET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        # Existing installations created before validation versions existed
        # receive the new column without requiring a manual migration.
        try:
            cursor.execute(
                "ALTER TABLE corpus_language_audit "
                "ADD COLUMN validation_version VARCHAR(20) NOT NULL DEFAULT '1'"
            )
        except mysql.connector.Error as error:
            if error.errno != 1060:  # duplicate column after the first run
                raise
        connection.commit()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


class LanguageAuditRepository:
    def __init__(self, db_config: dict):
        self.db_config = db_config

    def previous_decision(self, source_url: str) -> dict[str, str] | None:
        connection = cursor = None
        try:
            connection = mysql.connector.connect(**self.db_config)
            cursor = connection.cursor()
            cursor.execute(
                "SELECT decision_status, validation_version "
                "FROM corpus_language_audit WHERE source_url=%s",
                (source_url,),
            )
            row = cursor.fetchone()
            return {"status": row[0], "validation_version": row[1] or "1"} if row else None
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    def record(
        self,
        *,
        source_url: str,
        title: str,
        pdf_url: str | None,
        decision: AdmissionDecision,
        file_path: str | None = None,
        article_id: int | None = None,
    ) -> None:
        pdf = decision.pdf
        file_hash = stable_file_hash(file_path) if file_path and os.path.isfile(file_path) else None
        domain = urlparse(source_url).netloc.lower().removeprefix("www.")
        payload = json.dumps(decision.as_dict(), ensure_ascii=False, separators=(",", ":"))
        connection = cursor = None
        try:
            connection = mysql.connector.connect(**self.db_config)
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO corpus_language_audit
                (article_id, source_url, source_domain, pdf_url, article_title,
                 decision_status, rejection_reason, metadata_language, metadata_confidence,
                 pdf_language, pdf_confidence, vietnamese_ratio, english_ratio,
                 assessed_characters, file_path, file_hash_sha256, validation_version, evidence_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    article_id=COALESCE(VALUES(article_id), article_id),
                    source_domain=VALUES(source_domain), pdf_url=VALUES(pdf_url),
                    article_title=VALUES(article_title), decision_status=VALUES(decision_status),
                    rejection_reason=VALUES(rejection_reason), metadata_language=VALUES(metadata_language),
                    metadata_confidence=VALUES(metadata_confidence), pdf_language=VALUES(pdf_language),
                    pdf_confidence=VALUES(pdf_confidence), vietnamese_ratio=VALUES(vietnamese_ratio),
                    english_ratio=VALUES(english_ratio), assessed_characters=VALUES(assessed_characters),
                    file_path=VALUES(file_path), file_hash_sha256=VALUES(file_hash_sha256),
                    validation_version=VALUES(validation_version),
                    evidence_json=VALUES(evidence_json)
                """,
                (
                    article_id, source_url, domain, pdf_url, title[:500], decision.status, decision.reason,
                    decision.metadata.language, decision.metadata.confidence,
                    pdf.language if pdf else None, pdf.confidence if pdf else None,
                    pdf.vietnamese_ratio if pdf else None, pdf.english_ratio if pdf else None,
                    pdf.assessed_characters if pdf else None, file_path, file_hash,
                    LANGUAGE_VALIDATION_VERSION, payload,
                ),
            )
            connection.commit()
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()


def is_allowed_journal_url(url: str, settings: VietnameseCorpusSettings | None = None) -> bool:
    settings = settings or VietnameseCorpusSettings()
    host = urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    return host in settings.allowed_domains


def quarantine_pdf(
    source_path: str | Path,
    status: str,
    relative_parts: tuple[str, ...] = (),
    settings: VietnameseCorpusSettings | None = None,
) -> Path:
    """Move a rejected candidate to a reasoned quarantine path, never delete it."""
    settings = settings or VietnameseCorpusSettings()
    source = Path(source_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    bucket = status.casefold().replace("rejected_", "").replace(" ", "_")
    destination_dir = (settings.quarantine_dir / bucket).resolve()
    for part in relative_parts:
        if part not in {"", ".", ".."}:
            destination_dir /= Path(part).name
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        destination = destination_dir / f"{source.stem}_{stable_file_hash(str(source))[:12]}{source.suffix}"
    shutil.move(str(source), str(destination))
    return destination
