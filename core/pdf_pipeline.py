import hashlib
import logging
import os
import re
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
            import pdfplumber
            full_text = ""
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        full_text += text + "\n"
            metadata.extracted_text = full_text

            self._split_and_save_sections(full_text, Path(file_path).stem, metadata)

            metadata.steps_completed.append("extract_text")
            step.complete(success=True)
        except Exception as e:
            step.complete(success=False, error=str(e))
            raise PipelineError(f"Extraction failed: {e}", step="extract_text")
        finally:
            metadata.processing_steps.append(step)

    def _split_and_save_sections(self, text: str, base_name: str, metadata: ExtractedMetadata) -> None:
        out_dir = Path("Văn_Bản_Y_Tế_TXT")
        out_dir.mkdir(exist_ok=True)
        
        pattern = re.compile(
            r'^(TÓM TẮT|ABSTRACT|GIỚI THIỆU|ĐẶT VẤN ĐỀ|ĐỐI TƯỢNG VÀ PHƯƠNG PHÁP|PHƯƠNG PHÁP|ĐỐI TƯỢNG|KẾT QUẢ|BÀN LUẬN|KẾT LUẬN|TÀI LIỆU THAM KHẢO)',
            re.MULTILINE | re.IGNORECASE
        )
        
        matches = list(pattern.finditer(text))
        sections = {}
        
        if not matches:
            sections["khac"] = text
        else:
            if matches[0].start() > 0:
                sections["khac"] = text[:matches[0].start()].strip()
            
            for i, match in enumerate(matches):
                heading = match.group(1).upper()
                start_idx = match.end()
                end_idx = matches[i+1].start() if i + 1 < len(matches) else len(text)
                content = text[start_idx:end_idx].strip()
                
                shorthand = "khac"
                if "TÓM TẮT" in heading or "ABSTRACT" in heading:
                    shorthand = "tt"
                elif "GIỚI THIỆU" in heading or "ĐẶT VẤN ĐỀ" in heading:
                    shorthand = "gt"
                elif "PHƯƠNG PHÁP" in heading or "ĐỐI TƯỢNG" in heading:
                    shorthand = "pp"
                elif "KẾT QUẢ" in heading:
                    shorthand = "kq"
                elif "BÀN LUẬN" in heading:
                    shorthand = "bl"
                elif "KẾT LUẬN" in heading:
                    shorthand = "kl"
                elif "TÀI LIỆU" in heading:
                    shorthand = "tl"
                
                if shorthand in sections:
                    sections[shorthand] += "\n\n" + content
                else:
                    sections[shorthand] = content
        
        for shorthand, content in sections.items():
            if content:
                file_name = f"{base_name}_{shorthand}.txt"
                out_path = out_dir / file_name
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(content)
                metadata.extracted_files.append(str(out_path))

    @staticmethod
    def _sha256(file_path: str) -> str:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
