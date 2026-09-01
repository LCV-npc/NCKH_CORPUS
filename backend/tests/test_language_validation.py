import unittest

from config.language_filter import VietnameseCorpusSettings
from core.language_audit import is_allowed_journal_url
from core.language_validation import (
    assess_language,
    assess_metadata,
    decide_admission,
    select_pdf_text_for_language,
)


VIETNAMESE = (
    "Nghiên cứu này đánh giá kết quả điều trị cho người bệnh tại bệnh viện. "
    "Các phương pháp được áp dụng trong thời gian theo dõi và kết quả cho thấy hiệu quả tốt. "
)
ENGLISH = (
    "This clinical study evaluated treatment outcomes in patients at the hospital. "
    "The methods and results showed that the intervention was effective for medical care. "
)


class LanguageValidationTests(unittest.TestCase):
    def setUp(self):
        self.settings = VietnameseCorpusSettings(
            metadata_min_chars=40,
            pdf_min_chars=100,
            chunk_chars=180,
            min_vietnamese_ratio=0.72,
            english_reject_ratio=0.65,
        )

    def test_vietnamese_body_is_accepted(self):
        body = VIETNAMESE * 12
        decision = decide_admission(assess_metadata("Tiêu đề", VIETNAMESE, settings=self.settings), body, self.settings)
        self.assertTrue(decision.accepted)
        self.assertEqual("vi", decision.pdf.language)

    def test_english_body_is_rejected(self):
        body = ENGLISH * 12
        decision = decide_admission(assess_metadata("English article", ENGLISH, settings=self.settings), body, self.settings)
        self.assertEqual("REJECTED_ENGLISH", decision.status)
        self.assertEqual("en", decision.pdf.language if decision.pdf else "en")

    def test_bilingual_text_is_not_accepted_as_vietnamese(self):
        body = (VIETNAMESE * 5) + (ENGLISH * 5)
        decision = decide_admission(assess_metadata("Tiêu đề", VIETNAMESE, settings=self.settings), body, self.settings)
        self.assertIn(decision.status, {"REJECTED_ENGLISH", "REJECTED_MIXED"})
        self.assertFalse(decision.accepted)

    def test_empty_or_too_short_body_is_rejected(self):
        decision = decide_admission(assess_metadata("Tiêu đề", VIETNAMESE, settings=self.settings), "", self.settings)
        self.assertEqual("REJECTED_NO_TEXT", decision.status)

    def test_unknown_input_is_never_silently_vietnamese(self):
        assessment = assess_language("1234 @@@ ---", self.settings)
        self.assertEqual("unknown", assessment.language)

    def test_html_language_only_supplements_unknown_metadata(self):
        assessment = assess_metadata("", "", "en", self.settings)
        self.assertEqual("en", assessment.language)
        self.assertEqual("HTML_LANGUAGE_EN", assessment.reason)

    def test_incomplete_section_body_uses_full_pdf_for_language_evidence(self):
        english_translation = ENGLISH * 3
        vietnamese_article = VIETNAMESE * 15
        selected = select_pdf_text_for_language(
            english_translation, vietnamese_article + english_translation
        )
        decision = decide_admission(
            assess_metadata("Tiêu đề", "", settings=self.settings), selected, self.settings
        )
        self.assertTrue(decision.accepted)
        self.assertEqual("vi", decision.pdf.language)

    def test_only_configured_journal_domains_are_allowed(self):
        self.assertTrue(is_allowed_journal_url("https://tapchinghiencuuyhoc.vn", self.settings))
        self.assertFalse(is_allowed_journal_url("https://example.org", self.settings))


if __name__ == "__main__":
    unittest.main()
