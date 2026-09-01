import json
import asyncio
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import UploadFile

from api import routes
from config.llm import PDFLLMSettings
from core.article_exporter import LLMArticleExporter
from core.llm_client import GeminiStructuredClient, LLMInvalidResponseError
from core.llm_pdf_extractor import LLMPDFExtractor, load_pdf_extraction_prompt
from core.pdf_pipeline import ExtractorPipeline
from models.llm_article import ArticleExtractionResult, ArticleSection
from models.metadata import ExtractedMetadata


def _settings() -> PDFLLMSettings:
    return PDFLLMSettings(
        provider="gemini",
        api_key="unit-test-key",
        model="gemini-test",
        base_url="https://example.invalid/v1beta",
        temperature=0,
        timeout_seconds=10,
        max_retries=0,
        max_input_chars=12000,
        prompt_version="1.0",
    )


class _FakeStructuredClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def generate_structured_output(self, **_kwargs):
        self.calls += 1
        return self.payload


class LLMPDFExtractionTests(unittest.TestCase):
    def test_prompt_loader_reads_versioned_extraction_instruction(self):
        prompt = load_pdf_extraction_prompt()
        self.assertIn("prompt_version: 1.1", prompt)
        self.assertIn("không phải tóm tắt", prompt.casefold())
        self.assertIn("heading_block_id", prompt)
        self.assertIn("không được bỏ heading cha", prompt.casefold())
        self.assertIn("boundary descriptor", prompt.casefold())
        self.assertIn("không trả `content`", prompt.casefold())

    def test_malformed_llm_json_fails_safely(self):
        with self.assertRaises(LLMInvalidResponseError):
            GeminiStructuredClient._decode_response({
                "candidates": [{"content": {"parts": [{"text": "{not-json"}]}}]
            })

    def test_llm_metadata_requires_the_claimed_source_blocks(self):
        blocks = [
            {
                "id": "B0001", "order": 1, "page": 1,
                "text": "TIÊU ĐỀ THẬT", "font_size": 18, "bold": True,
                "bbox": {"x0": 40, "y0": 20, "x1": 500, "y1": 40},
            },
            {
                "id": "B0002", "order": 2, "page": 1,
                "text": "I. ĐẶT VẤN ĐỀ", "font_size": 12, "bold": True,
                "bbox": {"x0": 40, "y0": 60, "x1": 500, "y1": 75},
            },
            {
                "id": "B0003", "order": 3, "page": 1,
                "text": "Nội dung bài báo.", "font_size": 10, "bold": False,
                "bbox": {"x0": 40, "y0": 80, "x1": 500, "y1": 100},
            },
        ]
        payload = {
            "title": "TIÊU ĐỀ THẬT",
            "title_source_blocks": ["B9999"],
            "authors": [], "author_source_blocks": [],
            "affiliations": [], "affiliation_source_blocks": [],
            "abstract": None, "abstract_vi": None, "abstract_en": None,
            "abstract_source_blocks": [],
            "keywords": [], "keyword_source_blocks": [],
            "sections": [{
                "label": "I", "title": "ĐẶT VẤN ĐỀ",
                "full_heading": "I. ĐẶT VẤN ĐỀ", "level": 1,
                "parent": None, "heading_block_id": "B0002",
            }],
        }
        article = LLMPDFExtractor(
            settings=_settings(), client=_FakeStructuredClient(payload),
        ).extract(
            {"blocks": blocks, "headings": [], "title": "", "authors": "", "abstract": ""},
            source_pdf="paper.pdf",
        )
        self.assertIsNone(article.title)
        self.assertEqual("I. ĐẶT VẤN ĐỀ", article.sections[0].full_heading)

    def test_llm_sections_are_reconstructed_from_blocks_and_exclusions_are_removed(self):
        texts = [
            "TÊN BÀI BÁO", "Nguyễn Văn A, Trần Thị B", "TÓM TẮT",
            "Mục tiêu: đánh giá nghiên cứu.", "I. ĐẶT VẤN ĐỀ",
            "Nội dung mở đầu\nbị xuống dòng.", "II. PHƯƠNG PHÁP",
            "Nội dung phương pháp", "được chia sang block tiếp theo.", "LỜI CẢM ƠN",
            "Xin cảm ơn đơn vị tài trợ.", "TÀI LIỆU THAM KHẢO", "[1] Tài liệu A.",
        ]
        blocks = [
            {
                "id": f"B{index:04d}", "order": index, "page": 1,
                "text": text, "font_size": 12 if index in {1, 3, 5, 7, 9, 11} else 10,
                "bold": index in {1, 3, 5, 7, 9, 11},
                "bbox": {"x0": 40, "y0": index * 20, "x1": 560, "y1": index * 20 + 12},
            }
            for index, text in enumerate(texts, 1)
        ]
        headings = [
            {"heading": texts[index - 1], "page": 1}
            for index in (3, 5, 7, 10, 12)
        ]
        llm_payload = {
            "title": texts[0], "title_source_blocks": ["B0001"],
            "authors": ["Nguyễn Văn A", "Trần Thị B"],
            "author_source_blocks": ["B0002"],
            "affiliations": [], "affiliation_source_blocks": [],
            "abstract": texts[3], "abstract_vi": texts[3], "abstract_en": None,
            "abstract_source_blocks": ["B0004"],
            "keywords": [], "keyword_source_blocks": [],
            "sections": [
                {"label": "I", "title": "ĐẶT VẤN ĐỀ", "full_heading": texts[4], "level": 1, "parent": None, "heading_block_id": "B0005"},
                {"label": "II", "title": "PHƯƠNG PHÁP", "full_heading": texts[6], "level": 1, "parent": None, "heading_block_id": "B0007"},
                {"label": None, "title": "LỜI CẢM ƠN", "full_heading": texts[9], "level": 1, "parent": None, "heading_block_id": "B0010"},
                {"label": None, "title": "TÀI LIỆU THAM KHẢO", "full_heading": texts[11], "level": 1, "parent": None, "heading_block_id": "B0012"},
            ],
        }
        fake = _FakeStructuredClient(llm_payload)
        article = LLMPDFExtractor(settings=_settings(), client=fake).extract(
            {
                "blocks": blocks,
                "headings": headings,
                "title": texts[0],
                "authors": texts[1],
                "abstract": texts[3],
            },
            source_pdf="paper.pdf",
            source_hash="abc",
        )

        self.assertEqual(1, fake.calls)
        self.assertEqual(["I. ĐẶT VẤN ĐỀ", "II. PHƯƠNG PHÁP"], [s.full_heading for s in article.sections])
        self.assertEqual("Nội dung mở đầu bị xuống dòng.", article.sections[0].content)
        self.assertEqual(
            "Nội dung phương pháp được chia sang block tiếp theo.",
            article.sections[1].content,
        )
        self.assertNotIn("\n", article.sections[0].content)
        self.assertNotIn("\n", article.sections[1].content)
        combined = " ".join(section.content for section in article.sections)
        self.assertNotIn("cảm ơn", combined.casefold())
        self.assertNotIn("Tài liệu A", combined)

    def test_filename_sanitizer_is_windows_safe(self):
        self.assertEqual("A_B_C", ExtractorPipeline._safe_directory_name('A/B:C?'))

    def test_local_high_confidence_major_heading_recovers_missing_llm_parent(self):
        texts = [
            "I. ĐẶT VẤN ĐỀ", "Mở đầu.",
            "II. ĐỐI TƯỢNG VÀ PHƯƠNG PHÁP", "1. Đối tượng", "Người tham gia.",
            "III. KẾT QUẢ", "Kết quả nghiên cứu.",
        ]
        blocks = [
            {
                "id": f"B{index:04d}", "order": index, "page": 1,
                "text": value,
                "font_size": 12 if index in {1, 3, 6} else 10,
                "bold": index in {1, 3, 4, 6},
                "bbox": {"x0": 40, "y0": index * 20, "x1": 560, "y1": index * 20 + 12},
            }
            for index, value in enumerate(texts, 1)
        ]
        headings = [
            {
                "heading": "đặt vấn đề", "page": 1,
                "canonical_type": "INTRODUCTION", "classification_confidence": 0.99,
                "heading_score": 0.78,
            },
            {
                "heading": "đối tượng và phương pháp", "page": 1,
                "canonical_type": "METHODS", "classification_confidence": 0.99,
                "heading_score": 0.78,
            },
            {
                "heading": "kết quả", "page": 1,
                "canonical_type": "RESULTS", "classification_confidence": 0.99,
                "heading_score": 0.78,
            },
        ]
        payload = {
            "title": None, "title_source_blocks": [],
            "authors": [], "author_source_blocks": [],
            "affiliations": [], "affiliation_source_blocks": [],
            "abstract": None, "abstract_vi": None, "abstract_en": None,
            "abstract_source_blocks": [], "keywords": [], "keyword_source_blocks": [],
            "sections": [
                {"label": "I", "title": "ĐẶT VẤN ĐỀ", "full_heading": texts[0], "level": 1, "parent": None, "heading_block_id": "B0001"},
                {"label": "1", "title": "Đối tượng", "full_heading": texts[3], "level": 2, "parent": "II", "heading_block_id": "B0004"},
            ],
        }
        article = LLMPDFExtractor(
            settings=_settings(), client=_FakeStructuredClient(payload),
        ).extract(
            {"blocks": blocks, "headings": headings, "title": "", "authors": "", "abstract": ""},
            source_pdf="paper.pdf",
        )
        self.assertEqual(
            ["I. ĐẶT VẤN ĐỀ", "II. ĐỐI TƯỢNG VÀ PHƯƠNG PHÁP", "1. Đối tượng", "III. KẾT QUẢ"],
            [section.full_heading for section in article.sections],
        )
        self.assertEqual("II", article.sections[2].parent)

    def test_article_export_writes_utf8_txt_and_json(self):
        article = ArticleExtractionResult(
            source_pdf="bài.pdf",
            title="Bài báo tiếng Việt",
            authors=["Nguyễn Văn A"],
            keywords=["y học", "Việt Nam"],
            sections=[ArticleSection(
                order=1, label="I", title="Đặt vấn đề", full_heading="I. Đặt vấn đề",
                level=1, content="Nội dung nguyên văn.", source_pages=[1], source_blocks=["B0004"],
            )],
            extraction={"method": "llm_hybrid", "prompt_version": "1.0"},
        )
        with tempfile.TemporaryDirectory() as directory:
            created = LLMArticleExporter().export(article, Path(directory))
            payload = json.loads((Path(directory) / "article.json").read_text(encoding="utf-8"))
            self.assertEqual("Bài báo tiếng Việt", payload["title"])
            self.assertEqual("y học; Việt Nam", (Path(directory) / "keywords.txt").read_text(encoding="utf-8"))
            self.assertEqual(2, len(created))

    def test_extract_pdf_api_enables_real_llm_pipeline(self):
        captured = {}

        class FakePipeline:
            def run(self, file_path, **kwargs):
                captured.update(kwargs)
                return ExtractedMetadata(
                    source=kwargs["source"], file_path=file_path,
                    title="Tiêu đề", authors="Tác giả", abstract="Tóm tắt",
                    sections=[{"heading": "I"}], extraction={
                        "method": "llm_hybrid", "provider": "gemini",
                        "model": "gemini-test", "prompt_version": "1.0",
                    },
                )

        with patch.object(routes, "ExtractorPipeline", FakePipeline):
            response = asyncio.run(
                routes.extract_pdf_endpoint(
                    file=UploadFile(filename="paper.pdf", file=BytesIO(b"%PDF-test")),
                    relative_path="folder/paper.pdf",
                    _={"role": "admin"},
                )
            )
        self.assertTrue(captured["use_llm"])
        self.assertTrue(captured["require_vietnamese"])
        self.assertEqual("llm_hybrid", response["extraction"]["method"])


if __name__ == "__main__":
    unittest.main()
