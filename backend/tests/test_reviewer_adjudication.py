import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api.routes import ReviewerAdjudicationRequest, save_reviewer_adjudication


class _FakeCursor:
    def __init__(self, expert_rows):
        self._expert_rows = expert_rows
        self._result = None
        self.lastrowid = 73
        self.executions = []
        self.closed = False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executions.append((normalized, params))
        if normalized.startswith("SELECT id FROM articles"):
            self._result = {"id": 9}
        elif normalized.startswith("SELECT id, expert_id FROM expert_reviews"):
            self._result = list(self._expert_rows)
        elif normalized.startswith("INSERT INTO reviewer_adjudications"):
            self._result = None

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, expert_rows):
        self.fake_cursor = _FakeCursor(expert_rows)
        self.committed = False
        self.closed = False

    def cursor(self, dictionary=False):
        return self.fake_cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class ReviewerAdjudicationTests(unittest.TestCase):
    def _request(self):
        return ReviewerAdjudicationRequest(
            expertReviewAId=101,
            expertReviewBId=202,
            decisions=[
                {
                    "id": "entity-0",
                    "decision": "expert_a",
                    "expertA": {"text": "Nhãn A", "code": "A00"},
                    "expertB": {"text": "Nhãn B", "code": "B00"},
                }
            ],
            note="Đã đối chiếu",
        )

    def test_saves_two_distinct_expert_reviews_with_upsert(self):
        connection = _FakeConnection(
            [{"id": 101, "expert_id": 11}, {"id": 202, "expert_id": 22}]
        )
        with patch("api.routes._get_conn", return_value=connection):
            result = save_reviewer_adjudication(
                9,
                self._request(),
                {"id": 33, "role": "reviewer"},
            )

        self.assertEqual(result["message"], "Đã lưu thành công.")
        self.assertTrue(result["saved"])
        self.assertTrue(connection.committed)
        insert_sql, insert_params = connection.fake_cursor.executions[-1]
        self.assertIn("ON DUPLICATE KEY UPDATE", insert_sql)
        self.assertEqual(insert_params[:4], (9, 33, 101, 202))
        self.assertIn('"expertA"', insert_params[4])

    def test_rejects_reviews_from_the_same_expert(self):
        connection = _FakeConnection(
            [{"id": 101, "expert_id": 11}, {"id": 202, "expert_id": 11}]
        )
        with patch("api.routes._get_conn", return_value=connection):
            with self.assertRaises(HTTPException) as raised:
                save_reviewer_adjudication(
                    9,
                    self._request(),
                    {"id": 33, "role": "reviewer"},
                )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertFalse(connection.committed)


if __name__ == "__main__":
    unittest.main()
