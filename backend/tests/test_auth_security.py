"""Unit tests for password validation and role constants.

Database workflows are covered separately by the opt-in integration test so
this suite remains safe to run without a local MySQL service.
"""

import unittest

from core.auth import PASSWORD_MIN_LENGTH, ROLE_ADMIN, ROLE_EXPERT, hash_password, normalize_email, verify_password


class AuthenticationSecurityTests(unittest.TestCase):
    def test_password_is_one_way_and_verifiable(self):
        password = "CorpusReview!2026"
        stored = hash_password(password)
        self.assertNotEqual(stored, password)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_password(password, stored))
        self.assertFalse(verify_password("wrong-password", stored))

    def test_password_minimum_length_is_enforced(self):
        with self.assertRaises(ValueError):
            hash_password("x" * (PASSWORD_MIN_LENGTH - 1))

    def test_email_is_canonicalized_and_invalid_email_rejected(self):
        self.assertEqual(normalize_email("  Expert@Example.COM "), "expert@example.com")
        with self.assertRaises(ValueError):
            normalize_email("not-an-email")

    def test_only_supported_roles_are_defined(self):
        self.assertEqual({ROLE_ADMIN, ROLE_EXPERT}, {"ADMIN", "EXPERT"})


if __name__ == "__main__":
    unittest.main()
