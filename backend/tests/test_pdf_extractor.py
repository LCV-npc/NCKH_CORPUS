import json
import os
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz

from core.pdf_pipeline import ExtractorPipeline, PipelineError
from models.metadata import ExtractedMetadata
from pdf_extractor import (
    _build_hierarchical_sections,
    _clean_text,
    _extract_title_and_authors,
    _filter_article_headings,
    _get_label_for_heading,
    _known_heading_prefix,
    _line_text_from_chars,
    _order_page_blocks,
    _sanitize_abstract_content,
    _trim_to_article_front,
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
                output = Path("Kho_Ngu_Lieu_Txt/pdf_extracted/article_unhashed")
                output.mkdir(parents=True)
                stale = output / "old.txt"
                stale.write_text("stale", encoding="utf-8")

                ExtractorPipeline()._save_extraction({
                    "title": "Article title", "authors": "Author One",
                    "abstract": "Article abstract", "page_count": 1,
                    "validation": {}, "sections": sections,
                }, "article", metadata)

                self.assertFalse(stale.exists())
                self.assertEqual((output / "sections/01_custom.txt").read_text(encoding="utf-8"), "First content")
                self.assertEqual((output / "sections/02_custom_2.txt").read_text(encoding="utf-8"), "Second content")
                self.assertEqual((output / "title.txt").read_text(encoding="utf-8"), "Article title")
                self.assertEqual((output / "authors.txt").read_text(encoding="utf-8"), "Author One")
                self.assertEqual((output / "abstract.txt").read_text(encoding="utf-8"), "Article abstract")
                self.assertEqual(json.loads((output / "metadata.json").read_text(encoding="utf-8"))["title"], "Article title")
                structured = json.loads((output / "structured_article.json").read_text(encoding="utf-8"))
                self.assertEqual("2.0", structured["schemaVersion"])
                self.assertEqual(2, len(structured["sections"]))
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
                article_dir = Path(metadata.output_directory)
                abstract_path = article_dir / "abstract.txt"
                self.assertTrue(abstract_path.exists())
                self.assertNotIn("Introduction", abstract_path.read_text(encoding="utf-8"))
                data_path = next(Path(item["file_path"]) for item in metadata.extracted_files if item["label"] == "data_availability")
                self.assertTrue(data_path.exists())
                self.assertIn("Zenodo Deposit", data_path.read_text(encoding="utf-8"))
                self.assertTrue((article_dir / "metadata.json").exists())
                self.assertTrue((article_dir / "structured_article.json").exists())
                self.assertTrue((article_dir / "title.txt").exists())
                self.assertTrue((article_dir / "authors.txt").exists())
                self.assertFalse((article_dir / "sections/zenodo.txt").exists())
                self.assertFalse((article_dir / "sections/figure.txt").exists())
                self.assertFalse((article_dir / "sections/table.txt").exists())
            finally:
                os.chdir(original_cwd)

    def test_pipeline_skips_pdf_with_existing_sha256(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "same-content.pdf"
            pdf_path.write_bytes(_sample_pdf())
            try:
                os.chdir(temp_dir)
                first = ExtractorPipeline().run(str(pdf_path))
                second = ExtractorPipeline().run(str(pdf_path))

                self.assertFalse(first.is_duplicate)
                self.assertTrue(second.is_duplicate)
                self.assertEqual(first.file_hash_sha256, second.file_hash_sha256)
                self.assertEqual(first.output_directory, second.output_directory)
                self.assertEqual([], second.extracted_files)
                self.assertEqual(
                    1,
                    len(list(Path("Kho_Ngu_Lieu_Txt/pdf_extracted").rglob("metadata.json"))),
                )
            finally:
                os.chdir(original_cwd)

    def test_vietnamese_gate_does_not_persist_english_pdf_output(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "english.pdf"
            pdf_path.write_bytes(_sample_pdf())
            try:
                os.chdir(temp_dir)
                with self.assertRaises(PipelineError):
                    ExtractorPipeline().run(str(pdf_path), require_vietnamese=True)
                self.assertFalse(Path("Kho_Ngu_Lieu_Txt/pdf_extracted/english").exists())
            finally:
                os.chdir(original_cwd)

    def test_hierarchy_keeps_direct_content_separate_from_child_content(self):
        text = "II. ĐỐI TƯỢNG VÀ PHƯƠNG PHÁP\nMở đầu phương pháp.\n1. Đối tượng nghiên cứu\nNgười bệnh.\nTiêu chuẩn lựa chọn\nĐủ điều kiện."
        methods = "II. ĐỐI TƯỢNG VÀ PHƯƠNG PHÁP"
        population = "1. Đối tượng nghiên cứu"
        inclusion = "Tiêu chuẩn lựa chọn"
        headings = [
            {"heading": methods, "canonical_type": "METHODS", "label": "methods", "page": 1, "position": text.index(methods), "end_position": text.index(methods) + len(methods), "numbering_depth": 1, "heading_score": 0.9, "features": {}},
            {"heading": population, "canonical_type": "STUDY_POPULATION", "label": "study_population", "page": 1, "position": text.index(population), "end_position": text.index(population) + len(population), "numbering_depth": 1, "heading_score": 0.9, "features": {}},
            {"heading": inclusion, "canonical_type": "INCLUSION_CRITERIA", "label": "inclusion_criteria", "page": 1, "position": text.index(inclusion), "end_position": text.index(inclusion) + len(inclusion), "numbering_depth": None, "heading_score": 0.85, "features": {}},
        ]
        sections = _build_hierarchical_sections(text, headings)
        self.assertEqual("METHODS", sections[0]["canonical_type"])
        self.assertEqual("S001", sections[1]["parent_id"])
        self.assertEqual("S002", sections[2]["parent_id"])
        self.assertNotIn("Người bệnh", sections[0]["direct_content"])
        self.assertIn("Người bệnh", sections[0]["aggregate_content"])


    def test_structured_abstract_keeps_inline_labels_as_abstract_content(self):
        text = (
            "ABSTRACT\nObjective: Evaluate the intervention.\n"
            "Methods: We examined 47 patients.\n"
            "Results: The intervention was effective.\n"
            "Key words: intervention."
        )

        def heading(value, label, canonical_type):
            start = text.index(value)
            return {
                "heading": value, "label": label, "canonical_type": canonical_type,
                "page": 1, "position": start, "end_position": start + len(value),
                "numbering_depth": None, "heading_score": 0.9, "features": {},
            }

        headings = [
            heading("ABSTRACT", "abstract", "ABSTRACT"),
            heading("Objective", "objectives", "OBJECTIVES"),
            heading("Methods", "methods", "METHODS"),
            heading("Results", "results", "RESULTS"),
            heading("Key words", "keywords", "KEYWORDS"),
        ]
        filtered = _filter_article_headings(headings)
        self.assertEqual(["abstract", "keywords"], [item["label"] for item in filtered])

        abstract = _build_hierarchical_sections(text, filtered)[0]
        self.assertIn("Objective: Evaluate the intervention.", abstract["direct_content"])
        self.assertIn("Methods: We examined 47 patients.", abstract["direct_content"])
        self.assertIn("Results: The intervention was effective.", abstract["direct_content"])

    def test_abstract_is_not_discarded_when_title_starts_with_results_or_methods(self):
        headings = [
            {"heading": "RESULTS OF A STUDY", "label": "results", "page": 1, "position": 0},
            {"heading": "ABSTRACT", "label": "abstract", "page": 1, "position": 30},
            {"heading": "Keywords", "label": "keywords", "page": 1, "position": 100},
        ]
        labels = [item["label"] for item in _filter_article_headings(headings)]
        self.assertIn("abstract", labels)

    def test_footnote_marked_vietnamese_abstract_and_embedded_article_front(self):
        self.assertEqual("abstract", _known_heading_prefix("T\u00d3M T\u1eaeT8")[1])

        specs = [
            ("REFERENCES", 12.0, 90.0, 104.0),
            ("THE REQUESTED ARTICLE TITLE", 14.0, 260.0, 276.0),
            ("CONTINUES ON THIS LINE", 14.0, 278.0, 294.0),
            ("Author One, Author Two", 12.0, 302.0, 316.0),
            ("T\u00d3M T\u1eaeT8", 12.0, 330.0, 344.0),
            ("N\u1ed9i dung t\u00f3m t\u1eaft.", 9.0, 350.0, 361.0),
        ]
        full_text = "\n".join(item[0] for item in specs)
        lines, position = [], 0
        for text, size, y0, y1 in specs:
            lines.append({
                "text": text, "size": size, "page": 1,
                "bbox": (40.0, y0, 560.0, y1),
                "position": position, "end_position": position + len(text),
            })
            position += len(text) + 1

        trimmed_text, trimmed_lines = _trim_to_article_front(full_text, lines)
        self.assertTrue(trimmed_text.startswith("THE REQUESTED ARTICLE TITLE"))
        self.assertEqual("THE REQUESTED ARTICLE TITLE", trimmed_lines[0]["text"])
        self.assertEqual(0, trimmed_lines[0]["position"])

    def test_abstract_removes_author_and_contact_artifacts(self):
        content = (
            "Mục tiêu: Đánh giá hiệu quả điều trị.\n\n"
            "Nguyễn Văn A là tác giả liên hệ. Email: nguyenvana@example.org\n\n"
            "Chịu trách nhiệm chính: Nguyễn Văn A. Điện thoại: 0900000000"
        )
        cleaned = _sanitize_abstract_content(content, "Nguyễn Văn A, Trần Thị B")
        self.assertIn("Mục tiêu: Đánh giá hiệu quả điều trị.", cleaned)
        self.assertNotIn("Nguyễn Văn A", cleaned)
        self.assertNotIn("example.org", cleaned)
        self.assertNotIn("Điện thoại", cleaned)

    def test_abstract_keeps_text_after_interleaved_contact_footer(self):
        content = (
            "Mục tiêu: Đánh giá can thiệp. *Đại học Y Chịu trách nhiệm chính: Nguyễn Văn A "
            "Email: nguyenvana@example.org Ngày nhận bài: 16/3/2021 26 "
            "Các mẫu được đo bằng quy trình chuẩn. Kết quả: cải thiện rõ rệt."
        )
        cleaned = _sanitize_abstract_content(content, "Nguyễn Văn A")
        self.assertNotIn("nguyenvana@example.org", cleaned)
        self.assertNotIn("Nguyễn Văn A", cleaned)
        self.assertNotIn("Ngày nhận bài", cleaned)
        self.assertIn("Các mẫu được đo bằng quy trình chuẩn.", cleaned)
        self.assertIn("Kết quả: cải thiện rõ rệt.", cleaned)

    def test_section_display_heading_omits_numeric_prefix_but_keeps_original(self):
        text = "2.5. Kỹ thuật thu thập số liệu\nNội dung."
        sections = _build_hierarchical_sections(text, [{
            "heading": "2.5. Kỹ thuật thu thập số liệu", "label": "data_collection",
            "canonical_type": "DATA_COLLECTION", "page": 1, "position": 0,
            "end_position": len("2.5. Kỹ thuật thu thập số liệu"), "numbering_depth": 2,
            "heading_score": 0.9, "features": {},
        }])
        self.assertEqual("Kỹ thuật thu thập số liệu", sections[0]["heading"])
        self.assertEqual("2.5. Kỹ thuật thu thập số liệu", sections[0]["original_heading"])

    def test_title_extraction_joins_wrapped_title_lines(self):
        def line(text, size, y0, y1, position):
            return {
                "text": text, "size": size, "page": 1, "position": position,
                "bbox": (40.0, y0, 560.0, y1),
            }

        lines = [
            line("Journal header", 9.0, 20.0, 30.0, 0),
            line("CLINICAL CHARACTERISTICS OF GINGIVAL", 19.3, 70.0, 93.0, 20),
            line("ENLARGEMENT IN A GROUP OF VIETNAMESE PEOPLE", 19.3, 92.5, 115.5, 60),
            line("Nguyen Thi Hong Minh, Do Thi Thu Huong", 13.5, 145.0, 159.0, 110),
        ]
        title, authors = _extract_title_and_authors(lines, [{"position": 200}])
        self.assertEqual(
            "CLINICAL CHARACTERISTICS OF GINGIVAL ENLARGEMENT IN A GROUP OF VIETNAMESE PEOPLE",
            title,
        )
        self.assertEqual("Nguyen Thi Hong Minh, Do Thi Thu Huong", authors)

    def test_author_list_is_never_promoted_to_title_when_pdf_title_is_missing(self):
        def line(text, size, y0, y1, position):
            return {
                "text": text, "size": size, "page": 1, "position": position,
                "bbox": (40.0, y0, 560.0, y1),
            }

        author_line = "Vũ Hải Linh1, Nguyễn Văn Chủ2, Bùi Thị Bích Phương2"
        lines = [
            line(author_line, 12.0, 567.0, 581.0, 0),
            line("TÓM TẮT6", 12.0, 590.0, 602.0, 80),
        ]
        title, authors = _extract_title_and_authors(
            lines,
            [{"heading": "TÓM TẮT6", "canonical_type": "abstract", "page": 1, "position": 80}],
            "2022/ĐÁNH GIÁ KẾT QUẢ ĐIỀU TRỊ TỔN THƯƠNG CỔ TỬ CUNG_dd19416d4935.pdf",
        )
        self.assertEqual("ĐÁNH GIÁ KẾT QUẢ ĐIỀU TRỊ TỔN THƯƠNG CỔ TỬ CUNG", title)
        self.assertEqual(author_line, authors)
        self.assertNotEqual(title, authors)


if __name__ == "__main__":
    unittest.main()
