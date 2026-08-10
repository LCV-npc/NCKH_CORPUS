import os
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz

from core.pdf_pipeline import ExtractorPipeline
from models.metadata import ExtractedMetadata
from pdf_extractor import (
    _clean_text,
    _get_label_for_heading,
    _line_text_from_chars,
    _order_page_blocks,
    _validate_sections,
    extract_from_pdf_bytes,
)


def _sample_pdf() -> bytes:
    document = fitz.open()
    for page_number in range(1, 4):
        page = document.new_page(width=600, height=800)
        page.insert_text((40, 18), f"Medical Journal 2026 page {page_number}", fontsize=8)
        page.insert_text((40, 785), f"Downloaded from example.org on 9 August 2026", fontsize=8)
        page.insert_text((10, 400), "VERTICAL MARGIN", fontsize=8, rotate=90)

        if page_number == 1:
            page.insert_text((40, 55), "A generic medical article", fontname="hebo", fontsize=18)
            page.insert_text((40, 90), "Abstract", fontname="hebo", fontsize=12)
            page.insert_textbox(
                fitz.Rect(40, 105, 560, 150),
                "This abstract belongs only to the abstract section and contains no image text.",
                fontname="helv", fontsize=10,
            )
            page.insert_text((40, 185), "Introduction", fontname="hebo", fontsize=12)
            page.insert_textbox(
                fitz.Rect(40, 200, 280, 280),
                "Introduction content stays in the left column. It continues before the next heading.",
                fontsize=10,
            )
            page.insert_text((40, 305), "The Monarch application", fontname="hebo", fontsize=10)
            page.insert_textbox(
                fitz.Rect(40, 320, 280, 390), "Application section content in the left column.", fontsize=10
            )
            page.insert_text((320, 185), "Analysis tools power by Monarch", fontname="hebo", fontsize=10)
            page.insert_textbox(
                fitz.Rect(320, 200, 560, 280), "Analysis tools content in the right column.", fontsize=10
            )
        elif page_number == 2:
            page.insert_text((40, 70), "Community engagement", fontname="hebo", fontsize=10)
            page.insert_textbox(fitz.Rect(40, 85, 280, 180), "Community section content.", fontsize=10)
            page.insert_text((320, 70), "Discussion", fontname="hebo", fontsize=10)
            page.insert_textbox(fitz.Rect(320, 85, 560, 180), "Discussion section content.", fontsize=10)
            page.insert_text((320, 210), "Figure 1. This caption must not be extracted.", fontsize=9)

            # Bảng có đường kẻ để page.find_tables() nhận diện và loại cả chữ trong ô.
            table = fitz.Rect(40, 230, 280, 320)
            page.draw_rect(table)
            page.draw_line((160, 230), (160, 320))
            page.draw_line((40, 275), (280, 275))
            page.insert_text((50, 252), "Summary", fontname="hebo", fontsize=9)
            page.insert_text((170, 252), "Value", fontname="hebo", fontsize=9)
            page.insert_text((50, 297), "Forbidden table cell", fontsize=9)
            page.insert_text((170, 297), "123", fontsize=9)
        else:
            y = 70
            page.insert_text((40, y), "Data availability", fontname="hebo", fontsize=10)
            page.insert_text((40, y + 16), "Monarch Data:", fontname="hebo", fontsize=10)
            page.insert_text((40, y + 32), "data.monarchinitiative.org/monarch-kg-dev", fontsize=10)
            page.insert_text((40, y + 48), "Zenodo", fontname="hebo", fontsize=10)
            page.insert_text((88, y + 48), "Deposit:", fontname="heit", fontsize=10)
            page.insert_text((140, y + 48), "DOI: 10.5281/zenodo.8350685", fontsize=10)
            page.insert_text((40, y + 64), "Italic explanatory text remains normal text.", fontname="heit", fontsize=10)
            y += 95

            items = [
                ("Acknowledgements", "Acknowledgement section content."),
                ("Funding", "Funding section content."),
            ]
            for heading, content in items:
                page.insert_text((40, y), heading, fontname="hebo", fontsize=10)
                page.insert_text((40, y + 16), content, fontsize=10)
                y += 55
            page.insert_text((40, y), "Conflict of interest statement.", fontname="hebo", fontsize=10)
            page.insert_text((205, y), "The authors declare no conflict.", fontsize=10)
            page.insert_text((40, y + 55), "References", fontname="hebo", fontsize=10)
            page.insert_text((40, y + 72), "1. First reference text.", fontsize=10)

    payload = document.tobytes()
    document.close()
    return payload


class PdfExtractorTests(unittest.TestCase):
    def test_pymupdf_extracts_expected_sections_and_removes_margins(self):
        result = extract_from_pdf_bytes(_sample_pdf())

        self.assertIsNone(result["error"])
        labels = [section["label"] for section in result["sections"]]
        for expected in (
            "abstract", "introduction", "the_monarch_application",
            "analysis_tools_powered_by_monarch", "community_engagement",
            "discussion", "data_availability", "acknowledgment", "funding",
            "conflict_of_interest", "references",
        ):
            self.assertIn(expected, labels)
        self.assertNotIn("Downloaded from", result["full_text"])
        self.assertNotIn("VERTICAL MARGIN", result["full_text"])
        self.assertNotIn("Medical Journal", result["full_text"])
        self.assertIn("This caption must not be extracted", result["full_text"])
        self.assertNotIn("Forbidden table cell", result["full_text"])
        self.assertNotIn("figure", [section["label"] for section in result["sections"]])
        self.assertEqual(1, [section["label"] for section in result["sections"]].count("abstract"))
        self.assertTrue(result["validation"]["ok"])

    def test_abstract_stops_before_introduction(self):
        result = extract_from_pdf_bytes(_sample_pdf())
        abstract = next(section for section in result["sections"] if section["label"] == "abstract")

        self.assertIn("belongs only to the abstract", abstract["content"])
        self.assertNotIn("Introduction", abstract["content"])
        self.assertNotIn("Application section", abstract["content"])

    def test_two_column_blocks_are_read_left_then_right(self):
        left_top = {"bbox": (40, 100, 280, 140), "name": "left-top"}
        left_bottom = {"bbox": (40, 300, 280, 340), "name": "left-bottom"}
        right_top = {"bbox": (320, 100, 560, 140), "name": "right-top"}

        ordered = _order_page_blocks([right_top, left_bottom, left_top], 600)

        self.assertEqual([block["name"] for block in ordered], ["left-top", "left-bottom", "right-top"])

    def test_inline_heading_keeps_following_content(self):
        result = extract_from_pdf_bytes(_sample_pdf())
        conflict = next(
            section for section in result["sections"] if section["label"] == "conflict_of_interest"
        )
        self.assertIn("authors declare no conflict", conflict["content"])

    def test_data_availability_keeps_list_labels_and_italic_text_in_one_section(self):
        result = extract_from_pdf_bytes(_sample_pdf())
        data = next(section for section in result["sections"] if section["label"] == "data_availability")
        labels = [section["label"] for section in result["sections"]]

        self.assertIn("Monarch Data", data["content"])
        self.assertIn("Zenodo", data["content"])
        self.assertIn("Deposit", data["content"])
        self.assertIn("10.5281/zenodo.8350685", data["content"])
        self.assertIn("Italic explanatory text remains normal text", data["content"])
        self.assertNotIn("zenodo", labels)
        self.assertNotIn("deposit", labels)
        self.assertNotIn("doi", labels)

    def test_fragmented_known_heading_is_normalized_for_matching(self):
        self.assertEqual(
            _get_label_for_heading("The Monar c h application"),
            "the_monarch_application",
        )

    def test_article_specific_labels_are_stable(self):
        self.assertEqual(_get_label_for_heading("The Monarch application"), "the_monarch_application")
        self.assertEqual(
            _get_label_for_heading("Analysis tools power by Monarch"),
            "analysis_tools_powered_by_monarch",
        )
        self.assertEqual(_get_label_for_heading("Community engagement"), "community_engagement")

    def test_validation_rejects_footer_content(self):
        report = _validate_sections(
            "Abstract\nValid text.",
            [{"heading": "Abstract", "label": "abstract", "content": "Downloaded from site"}],
        )
        self.assertFalse(report["ok"])
        self.assertIn("abstract:contains_boilerplate", report["issues"])

    def test_spacing_normalization(self):
        self.assertEqual(
            _clean_text("profile-matching . github.com / monarch / app"),
            "profile-matching. github.com/monarch/app",
        )

    def test_fake_font_space_is_removed_but_real_space_is_kept(self):
        def char(value, x0, x1):
            return {"c": value, "bbox": (x0, 0, x1, 8)}

        spans = [{
            "size": 8,
            "chars": [
                char("v", 0, 4), char(" ", 4, 7.5), char("a", 4.0, 8),
                char("r", 8, 11), char(" ", 11, 14.5), char("t", 12.9, 16),
            ],
        }]
        self.assertEqual(_line_text_from_chars(spans), "var t")

    def test_pipeline_removes_stale_files_and_saves_unique_labels(self):
        sections = [
            {"heading": "First", "label": "custom", "content": "First content"},
            {"heading": "Second", "label": "custom", "content": "Second content"},
        ]
        metadata = ExtractedMetadata(source="test", file_path="article.pdf")
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                os.chdir(temp_dir)
                output = Path("Văn_Bản_Y_Tế_TXT")
                output.mkdir()
                stale = output / "article_old.txt"
                stale.write_text("stale", encoding="utf-8")

                ExtractorPipeline()._save_sections(sections, "article", metadata)

                self.assertFalse(stale.exists())
                self.assertEqual((output / "article_custom.txt").read_text(encoding="utf-8"), "First content")
                self.assertEqual((output / "article_custom_2.txt").read_text(encoding="utf-8"), "Second content")
            finally:
                os.chdir(original_cwd)

    def test_pipeline_runs_end_to_end_with_pymupdf(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "sample.pdf"
            pdf_path.write_bytes(_sample_pdf())
            try:
                os.chdir(temp_dir)
                metadata = ExtractorPipeline().run(str(pdf_path))

                labels = [section["label"] for section in metadata.sections]
                self.assertIn("abstract", labels)
                self.assertIn("references", labels)
                self.assertTrue(metadata.validation_report["ok"])
                abstract_path = Path("Văn_Bản_Y_Tế_TXT/sample_abstract.txt")
                self.assertTrue(abstract_path.exists())
                self.assertNotIn("Introduction", abstract_path.read_text(encoding="utf-8"))
                data_path = Path("Văn_Bản_Y_Tế_TXT/sample_data_availability.txt")
                self.assertTrue(data_path.exists())
                self.assertIn("Zenodo Deposit", data_path.read_text(encoding="utf-8"))
                self.assertFalse(Path("Văn_Bản_Y_Tế_TXT/sample_zenodo.txt").exists())
                self.assertFalse(Path("Văn_Bản_Y_Tế_TXT/sample_figure.txt").exists())
                self.assertFalse(Path("Văn_Bản_Y_Tế_TXT/sample_table.txt").exists())
            finally:
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
