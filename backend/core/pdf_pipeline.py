import hashlib
import json
import logging
import os
import re
import unicodedata
from pathlib import Path

from config.constants import PDF_MAGIC_BYTES, MAX_FILE_SIZE_BYTES
from models.metadata import ExtractedMetadata, ProcessingStep

logger = logging.getLogger(__name__)

PDF_OUTPUT_ROOT = Path(os.getenv("PDF_EXTRACT_OUTPUT_DIR", "Kho_Ngu_Lieu_Txt/pdf_extracted"))

class PipelineError(Exception):
    def __init__(self, message: str, step: str = ""):
        self.step = step
        super().__init__(message)

class ExtractorPipeline:
    """
    Pipeline xử lý PDF — trích xuất metadata từ file PDF và chia section.
    """

    def __init__(self):
        logger.info("ExtractorPipeline initialized")

    def run(self, file_path: str, source: str = "upload") -> ExtractedMetadata:
        """
        Chạy toàn bộ pipeline trên một file PDF.
        """
        metadata = ExtractedMetadata(source=source, file_path=file_path)

        # Step 1: Pre-check
        self._step_precheck(file_path, metadata)

        # Step 2: Extract Text and Split Sections
        self._step_extract_text(file_path, metadata)

        return metadata

    def _step_precheck(self, file_path: str, metadata: ExtractedMetadata) -> None:
        """
        Pre-check file PDF:
        1. File tồn tại?
        2. File size ≤ giới hạn?
        3. Magic bytes = %PDF?
        4. Tính SHA-256 hash.
        """
        step = ProcessingStep(step_name="precheck")
        step.start()

        try:
            path = Path(file_path)

            if not path.exists():
                raise PipelineError(f"File not found: {file_path}", step="precheck")

            file_size = path.stat().st_size
            if file_size > MAX_FILE_SIZE_BYTES:
                size_mb = file_size / (1024 * 1024)
                max_mb = MAX_FILE_SIZE_BYTES / (1024 * 1024)
                raise PipelineError(f"File too large: {size_mb:.1f}MB (max {max_mb:.0f}MB)", step="precheck")

            with open(file_path, "rb") as f:
                magic = f.read(4)
            if magic != PDF_MAGIC_BYTES:
                raise PipelineError(f"Not a valid PDF file (magic bytes: {magic!r})", step="precheck")

            metadata.file_hash_sha256 = self._sha256(file_path)
            metadata.steps_completed.append("precheck")

            step.complete(success=True)
            logger.info(f"Pre-check passed: {path.name} ({file_size / 1024:.1f}KB, hash={metadata.file_hash_sha256[:12]}...)")

        except PipelineError:
            step.complete(success=False, error=str(step))
            raise
        except Exception as e:
            step.complete(success=False, error=str(e))
            raise PipelineError(f"Unexpected error in precheck: {e}", step="precheck")
        finally:
            metadata.processing_steps.append(step)

    def _step_extract_text(self, file_path: str, metadata: ExtractedMetadata) -> None:
        step = ProcessingStep(step_name="extract_text")
        step.start()
        try:
            from pdf_extractor import extract_from_pdf_path

            result = extract_from_pdf_path(file_path)
            if result.get("error"):
                raise PipelineError(result["error"], step="extract_text")

            metadata.extracted_text = result["full_text"]
            metadata.title = result.get("title", "")
            metadata.authors = result.get("authors", "")
            metadata.abstract = result.get("abstract", "")
            metadata.page_count = int(result.get("page_count") or 0)
            metadata.headings = result["headings"]
            metadata.sections = result["sections"]
            metadata.validation_report = result["validation"]

            self._save_extraction(result, Path(file_path).stem, metadata)

            metadata.steps_completed.append("extract_text")
            step.complete(success=True)
        except Exception as e:
            step.complete(success=False, error=str(e))
            raise PipelineError(f"Extraction failed: {e}", step="extract_text")
        finally:
            metadata.processing_steps.append(step)

    @staticmethod
    def _safe_directory_name(value: str) -> str:
        normalized = unicodedata.normalize("NFC", str(value or "")).strip()
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", normalized)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
        return (cleaned or "pdf_article")[:120]

    def _save_extraction(self, result: dict, base_name: str, metadata: ExtractedMetadata) -> None:
        """Persist article metadata and section TXT files in one traceable folder."""
        # Keep this method self-contained so any caller that already has an
        # extractor result cannot accidentally write empty metadata files.
        metadata.title = str(result.get("title") or metadata.title or "")
        metadata.authors = str(result.get("authors") or metadata.authors or "")
        metadata.abstract = str(result.get("abstract") or metadata.abstract or "")
        metadata.page_count = int(result.get("page_count") or metadata.page_count or 0)
        metadata.validation_report = result.get("validation") or metadata.validation_report or {}
        article_dir = PDF_OUTPUT_ROOT / self._safe_directory_name(base_name)
        article_dir.mkdir(parents=True, exist_ok=True)
        metadata.output_directory = str(article_dir)

        # Remove only previously generated output for this exact article.
        for old_file in article_dir.iterdir():
            if old_file.is_file() and old_file.suffix.lower() in {".txt", ".json"}:
                old_file.unlink()

        metadata_payload = {
            "source": metadata.source,
            "source_file": Path(metadata.file_path).name,
            "sha256": metadata.file_hash_sha256,
            "pageCount": metadata.page_count,
            "title": metadata.title,
            "authors": metadata.authors,
            "abstract": metadata.abstract,
            "validation": metadata.validation_report,
        }
        metadata_path = article_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        metadata.metadata_file = str(metadata_path)

        for filename, content, section_name, label in (
            ("title.txt", metadata.title, "Tiêu đề", "title"),
            ("authors.txt", metadata.authors, "Tác giả", "authors"),
            ("abstract.txt", metadata.abstract, "Tóm tắt", "abstract"),
        ):
            out_path = article_dir / filename
            out_path.write_text(content.strip(), encoding="utf-8")
            metadata.extracted_files.append({
                "file_path": str(out_path), "section_name": section_name,
                "heading": section_name, "label": label,
                "content_preview": content.strip()[:300],
            })

        self._save_sections(result["sections"], article_dir, metadata)

    def _save_sections(
        self,
        sections: list,
        out_dir: Path,
        metadata: ExtractedMetadata,
    ) -> None:
        """Lưu các section đã được PyMuPDF phân tách thành từng file .txt."""
        # Map label -> tên hiển thị
        LABEL_DISPLAY = {
            "abstract":           "TÓM TẮT",
            "introduction":       "GIỚI THIỆU / ĐẶT VẤN ĐỀ",
            "methods":            "PHƯƠNG PHÁP NGHIÊN CỨU",
            "results":            "KẾT QUẢ",
            "results_discussion": "KẾT QUẢ VÀ BÀN LUẬN",
            "discussion":         "BÀN LUẬN",
            "conclusion":         "KẾT LUẬN",
            "references":         "TÀI LIỆU THAM KHẢO",
            "keywords":           "TỪ KHÓA",
            "acknowledgment":     "LỜI CẢM ƠN",
            "data_availability":  "DATA AVAILABILITY",
            "funding":            "FUNDING",
            "conflict_of_interest": "CONFLICT OF INTEREST",
            "author_contributions": "AUTHOR CONTRIBUTIONS",
            "supplementary_data": "SUPPLEMENTARY DATA",
            "graphical_abstract": "GRAPHICAL ABSTRACT",
            "section":            "NỘI DUNG KHÁC",
        }

        # Lưu file và cập nhật metadata.extracted_files. Không ghi đè khi một bài
        # có nhiều đề mục cùng label (ví dụ nhiều subsection chưa chuẩn hóa).
        label_counts = {}
        for sec in sections:
            content = sec.get("content", "").strip()
            if not content:
                continue

            label     = sec.get("label", "section")
            heading   = sec.get("heading", label)
            sec_name  = LABEL_DISPLAY.get(label, heading)

            # ``abstract.txt`` is already the canonical metadata output
            # written above. Do not duplicate it in the section list.
            if label == "abstract":
                continue

            label_counts[label] = label_counts.get(label, 0) + 1
            occurrence = label_counts[label]
            suffix = "" if occurrence == 1 else f"_{occurrence}"
            file_name = f"{label}{suffix}.txt"
            out_path  = out_dir / file_name
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content)

            preview = content[:300].strip()
            if len(content) > 300:
                preview += "..."

            metadata.extracted_files.append({
                "file_path":       str(out_path),
                "section_name":    sec_name,
                "heading":         heading,
                "label":           label,
                "content_preview": preview,
            })

        # --- Thêm log nhận diện section ---
        expected_labels = ["abstract", "introduction", "methods", "results", "discussion", "conclusion", "references"]
        found_labels = set(sec.get("label") for sec in sections if sec.get("label"))
        found_expected = [L for L in expected_labels if L in found_labels]
        missing_expected = [L for L in expected_labels if L not in found_labels]
        
        log_msg = f"[{out_dir.name}]: đã tìm thấy {len(found_expected)}/{len(expected_labels)} section"
        if missing_expected:
            log_msg += f" — thiếu {', '.join(missing_expected)}"
        else:
            log_msg += " — ĐỦ CÁC MỤC!"

        logger.info(log_msg)

    @staticmethod
    def _sha256(file_path: str) -> str:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
