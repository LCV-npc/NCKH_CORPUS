"""MySQL persistence for Tâm Anh Q&A provenance metadata.

Raw patient/doctor text remains in the paired corpus files.  This table stores
only the information needed to trace a corpus item back to its source.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

import mysql.connector


class TamanhQaMetadataRepository:
    TABLE_NAME = "tamanh_qa_metadata"

    def __init__(self, db_config: dict):
        if not db_config:
            raise RuntimeError("Thiếu cấu hình MySQL cho metadata Tâm Anh.")
        self._db_config = db_config

    def _connection(self):
        return mysql.connector.connect(**self._db_config)

    def ensure_table(self) -> None:
        connection = cursor = None
        try:
            connection = self._connection()
            cursor = connection.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tamanh_qa_metadata (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    qa_identifier VARCHAR(255) NOT NULL,
                    source VARCHAR(100) NOT NULL,
                    category VARCHAR(255) NOT NULL,
                    question_file VARCHAR(1024) NOT NULL,
                    answer_file VARCHAR(1024) NOT NULL,
                    source_url VARCHAR(1024) NOT NULL,
                    category_url VARCHAR(1024) NOT NULL,
                    doctor_name VARCHAR(500) NULL,
                    question_title TEXT NULL,
                    published_year SMALLINT UNSIGNED NULL,
                    fingerprint CHAR(64) NOT NULL,
                    crawled_at DATETIME(6) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uq_tamanh_qa_identifier (qa_identifier),
                    UNIQUE KEY uq_tamanh_qa_fingerprint (fingerprint),
                    KEY idx_tamanh_qa_category (category),
                    KEY idx_tamanh_qa_published_year (published_year),
                    KEY idx_tamanh_qa_source_url (source_url(191))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            connection.commit()
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    def fingerprints(self) -> set[str]:
        connection = cursor = None
        try:
            connection = self._connection()
            cursor = connection.cursor()
            cursor.execute("SELECT fingerprint FROM tamanh_qa_metadata")
            return {row[0] for row in cursor.fetchall() if row[0]}
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    def upsert(self, metadata: dict[str, Any]) -> None:
        """Persist one metadata item, updating paths during a controlled migration."""
        connection = cursor = None
        try:
            connection = self._connection()
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO tamanh_qa_metadata
                (qa_identifier, source, category, question_file, answer_file, source_url,
                 category_url, doctor_name, question_title, published_year, fingerprint, crawled_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    qa_identifier=VALUES(qa_identifier), source=VALUES(source), category=VALUES(category),
                    question_file=VALUES(question_file), answer_file=VALUES(answer_file),
                    source_url=VALUES(source_url), category_url=VALUES(category_url),
                    doctor_name=VALUES(doctor_name), question_title=VALUES(question_title),
                    published_year=VALUES(published_year), crawled_at=VALUES(crawled_at)
                """,
                self._values(metadata),
            )
            connection.commit()
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    def sync_metadata_file(self, metadata_path: Path) -> int:
        """Import existing JSON metadata so a storage migration preserves provenance."""
        if not metadata_path.is_file():
            return 0
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return 0
        for metadata in payload:
            if isinstance(metadata, dict) and metadata.get("fingerprint"):
                self.upsert(metadata)
        return len(payload)

    @staticmethod
    def _values(metadata: dict[str, Any]) -> tuple:
        raw_crawled_at = str(metadata.get("crawledAt") or "")
        try:
            crawled_at = datetime.fromisoformat(raw_crawled_at.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            crawled_at = datetime.now()
        return (
            metadata["id"], metadata.get("source") or "Tâm Anh Hospital",
            metadata.get("category") or "", metadata.get("questionFile") or "",
            metadata.get("answerFile") or "", metadata.get("sourceUrl") or "",
            metadata.get("categoryUrl") or "", metadata.get("doctorName"),
            metadata.get("questionTitle"), metadata.get("publishedYear"),
            metadata["fingerprint"], crawled_at,
        )
