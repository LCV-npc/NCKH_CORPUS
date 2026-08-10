import hashlib
import logging
from pathlib import Path

from config.constants import PDF_MAGIC_BYTES, MAX_FILE_SIZE_BYTES
from models.metadata import ExtractedMetadata, ProcessingStep

logger = logging.getLogger(__name__)

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
            metadata.headings = result["headings"]
            metadata.sections = result["sections"]
            metadata.validation_report = result["validation"]

            self._save_sections(result["sections"], Path(file_path).stem, metadata)

            metadata.steps_completed.append("extract_text")
            step.complete(success=True)
        except Exception as e:
            step.complete(success=False, error=str(e))
            raise PipelineError(f"Extraction failed: {e}", step="extract_text")
        finally:
            metadata.processing_steps.append(step)

    def _save_sections(
        self,
        sections: list,
        base_name: str,
        metadata: ExtractedMetadata,
    ) -> None:
        """Lưu các section đã được PyMuPDF phân tách thành từng file .txt."""
        out_dir = Path("Văn_Bản_Y_Tế_TXT")
        out_dir.mkdir(exist_ok=True)

        # Loại kết quả cũ của đúng bài này để lần chạy mới không lẫn các section
        # đã được tạo bởi thuật toán/đề mục trước đó.
        output_prefix = f"{base_name}_"
        for old_file in out_dir.iterdir():
            if (
                old_file.is_file()
                and old_file.suffix.lower() == ".txt"
                and old_file.name.startswith(output_prefix)
            ):
                old_file.unlink()

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

            label_counts[label] = label_counts.get(label, 0) + 1
            occurrence = label_counts[label]
            suffix = "" if occurrence == 1 else f"_{occurrence}"
            file_name = f"{base_name}_{label}{suffix}.txt"
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
        
        log_msg = f"[{base_name}]: đã tìm thấy {len(found_expected)}/{len(expected_labels)} section"
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
