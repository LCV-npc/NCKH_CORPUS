"""Regression tests for Vietnamese and English scientific-section headings."""

import unittest

from core.section_ontology import classify_section, normalize_heading


class SectionOntologyTests(unittest.TestCase):
    def test_vietnamese_headings_map_to_canonical_types(self):
        cases = {
            "ĐẶT VẤN ĐỀ": "INTRODUCTION",
            "ĐỐI TƯỢNG VÀ PHƯƠNG PHÁP": "METHODS",
            "Tiêu chuẩn lựa chọn": "INCLUSION_CRITERIA",
            "KẾT LUẬN": "CONCLUSION",
            "Tài liệu tham khảo": "REFERENCES",
        }

        for heading, expected_type in cases.items():
            with self.subTest(heading=heading):
                actual_type, confidence, source = classify_section(heading)
                self.assertEqual(expected_type, actual_type)
                self.assertGreaterEqual(confidence, 0.91)
                self.assertEqual("ontology_exact", source)

    def test_normalization_removes_vietnamese_diacritics_without_losing_words(self):
        self.assertEqual(
            "doi tuong va phuong phap",
            normalize_heading("II. Đối tượng và phương pháp"),
        )


if __name__ == "__main__":
    unittest.main()
