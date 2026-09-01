"""Opt-in MySQL integration test for auth, RBAC and persistent review history.

Run with RUN_AUTH_INTEGRATION=1.  It creates uniquely named temporary users,
a temporary ICD-10 concept and one review, then removes all of them in teardown.
"""

import os
import uuid
import unittest


@unittest.skipUnless(os.getenv("RUN_AUTH_INTEGRATION") == "1", "set RUN_AUTH_INTEGRATION=1 to run against local MySQL")
class AuthReviewIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from main import db_config
        from core.auth import create_or_update_admin, ensure_auth_review_schema, register_expert

        ensure_auth_review_schema(db_config)
        cls.db_config = db_config
        cls.suffix = uuid.uuid4().hex[:12]
        cls.expert_email = f"review-test-expert-{cls.suffix}@example.invalid"
        cls.admin_email = f"review-test-admin-{cls.suffix}@example.invalid"
        cls.expert = register_expert(db_config, "Review Test Expert", cls.expert_email, "CorpusReview!2026")
        cls.admin = create_or_update_admin(db_config, "Review Test Admin", cls.admin_email, "CorpusReview!2026")
        import mysql.connector
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id FROM articles ORDER BY id LIMIT 1")
        article = cursor.fetchone()
        if not article:
            raise unittest.SkipTest("No corpus article available for review persistence test")
        cls.article_id = int(article["id"])
        cursor.close()
        connection.close()

    @classmethod
    def tearDownClass(cls):
        import mysql.connector
        connection = mysql.connector.connect(**cls.db_config)
        cursor = connection.cursor()
        cursor.execute("DELETE FROM expert_reviews WHERE expert_id IN (SELECT id FROM users WHERE email IN (%s, %s))", (cls.expert_email, cls.admin_email))
        cursor.execute("DELETE FROM ai_document_labels WHERE generated_by IN (SELECT id FROM users WHERE email IN (%s, %s))", (cls.expert_email, cls.admin_email))
        cursor.execute("DELETE FROM extracted_concepts WHERE concept_name = %s", (f"temporary review code {cls.suffix}",))
        cursor.execute("DELETE FROM users WHERE email IN (%s, %s)", (cls.expert_email, cls.admin_email))
        connection.commit()
        cursor.close()
        connection.close()

    def test_login_roles_document_restriction_and_review_persistence(self):
        from fastapi import HTTPException
        import mysql.connector
        from api.routes import (
            ReviewCreateRequest,
            SaveAiLabelRequest,
            _icd_code_catalog,
            _require_admin,
            _require_expert,
            expert_document_detail,
            save_ai_label_endpoint,
            save_expert_review,
        )
        from core.auth import authenticate, get_session_user

        expert, expert_token, _ = authenticate(self.db_config, self.expert_email, "CorpusReview!2026")
        admin, _, _ = authenticate(self.db_config, self.admin_email, "CorpusReview!2026")
        self.assertEqual(get_session_user(self.db_config, f"Bearer {expert_token}")["role"], "expert")
        with self.assertRaises(HTTPException) as forbidden:
            _require_admin(expert)
        self.assertEqual(forbidden.exception.status_code, 403)
        self.assertEqual(_require_admin(admin)["role"], "admin")
        self.assertEqual(_require_expert(expert)["role"], "expert")

        # Create an eligible ICD-10-labelled document only for this test and
        # use a validated real code from the project dictionary.
        code = next(key for key, value in _icd_code_catalog().items() if value)
        connection = mysql.connector.connect(**self.db_config)
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO extracted_concepts (article_id, concept_name, concept_type, concept_code) VALUES (%s, %s, %s, %s)",
            (self.article_id, f"temporary review code {self.suffix}", "DISEASE", code),
        )
        connection.commit()
        cursor.close()
        connection.close()

        detail = expert_document_detail(self.article_id, expert)
        self.assertEqual(detail["id"], self.article_id)
        created = save_expert_review(
            self.article_id,
            ReviewCreateRequest(reviewStatus="CORRECT", comment="Temporary integration review."),
            expert,
        )
        self.assertIn("reviewId", created)
        refreshed = expert_document_detail(self.article_id, expert)
        self.assertTrue(any(item["comment"] == "Temporary integration review." for item in refreshed["reviewHistory"]))

        ai_labels = {"Bệnh lý": [{"term": "temporary AI label", "code": code, "label_vn": "temporary AI label"}]}
        saved = save_ai_label_endpoint(SaveAiLabelRequest(articleId=self.article_id, labels=ai_labels), admin)
        duplicate = save_ai_label_endpoint(SaveAiLabelRequest(articleId=self.article_id, labels=ai_labels), admin)
        self.assertTrue(saved["saved"])
        self.assertTrue(duplicate["duplicate"])


if __name__ == "__main__":
    unittest.main()
