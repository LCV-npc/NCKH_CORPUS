import hashlib
import json
import logging
import re
import shutil
import unicodedata
from pathlib import Path

from config.constants import PDF_MAGIC_BYTES, MAX_FILE_SIZE_BYTES
from config.env import backend_path_from_env
from config.llm import PDFLLMSettings, PDF_TEXT_NORMALIZATION_VERSION
from core.article_exporter import LLMArticleExporter
from core.language_validation import assess_metadata, decide_admission, select_pdf_text_for_language
from core.section_ontology import canonical_label, classify_section
from models.metadata import ExtractedMetadata, ProcessingStep

logger = logging.getLogger(__name__)

PDF_OUTPUT_ROOT = backend_path_from_env("PDF_EXTRACT_OUTPUT_DIR", "Kho_Ngu_Lieu_Txt/pdf_extracted")
PDF_HASH_INDEX_FILE = ".processed_pdf_hashes.json"

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

    def run(
        self,
        file_path: str,
        source: str = "upload",
        require_vietnamese: bool = False,
        use_llm: bool = False,
    ) -> ExtractedMetadata:
        """
        Chạy toàn bộ pipeline trên một file PDF.
        """
        metadata = ExtractedMetadata(source=source, file_path=file_path)

        # Step 1: Pre-check
        self._step_precheck(file_path, metadata)

        existing = self._find_processed_pdf(metadata.file_hash_sha256)
        if existing and (not use_llm or self._llm_cache_matches(existing.get("payload") or {})):
            self._populate_duplicate_metadata(metadata, existing)
            logger.info("Skipped duplicate PDF: %s (hash=%s...)", Path(file_path).name, metadata.file_hash_sha256[:12])
            return metadata

        # Step 2: Extract Text and Split Sections
        self._step_extract_text(
            file_path,
            metadata,
            require_vietnamese=require_vietnamese,
            use_llm=use_llm,
        )

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

    def _step_extract_text(
        self,
        file_path: str,
        metadata: ExtractedMetadata,
        require_vietnamese: bool = False,
        use_llm: bool = False,
    ) -> None:
        step = ProcessingStep(step_name="extract_text")
        step.start()
        try:
            from pdf_extractor import extract_from_pdf_path

            # ``source`` retains the original browser/crawler filename even
            # when the upload is staged under a temporary server filename.
            # It is only used as a provenance fallback when a PDF has no
            # machine-readable title text; it never replaces a detected title.
            result = extract_from_pdf_path(file_path, source_hint=metadata.source or file_path)
            if result.get("error"):
                raise PipelineError(result["error"], step="extract_text")

            metadata.extracted_text = result["full_text"]
            metadata.title = result.get("title", "")
            metadata.authors = result.get("authors", "")
            metadata.abstract = result.get("abstract", "")
            metadata.page_count = int(result.get("page_count") or 0)
            metadata.headings = result["headings"]
            metadata.document_blocks = result.get("blocks", [])
            metadata.sections = result["sections"]
            metadata.validation_report = result["validation"]

            if require_vietnamese:
                decision = decide_admission(
                    assess_metadata(metadata.title, metadata.abstract),
                    select_pdf_text_for_language(result.get("body"), result.get("full_text")),
                )
                metadata.language_decision = decision.as_dict()
                if not decision.accepted:
                    raise PipelineError(
                        f"PDF không được nhận vào corpus tiếng Việt: {decision.reason}",
                        step="language_validation",
                    )

            if use_llm:
                from core.llm_pdf_extractor import LLMPDFExtractor

                logger.info("[LLM] Starting scientific structure extraction")
                article = LLMPDFExtractor().extract(
                    result,
                    source_pdf=metadata.source or Path(file_path).name,
                    source_hash=metadata.file_hash_sha256,
                )
                self._apply_llm_article(result, article)
                metadata.title = result.get("title", "")
                metadata.authors = result.get("authors", "")
                metadata.abstract = result.get("abstract", "")
                metadata.keywords = article.keywords
                metadata.affiliations = article.affiliations
                metadata.sections = result["sections"]
                metadata.validation_report = result["validation"]
                metadata.extraction = article.extraction

            # Persist only after a requested language gate has accepted it.
            self._save_extraction(result, Path(file_path).stem, metadata)

            metadata.steps_completed.append("extract_text")
            step.complete(success=True)
        except Exception as e:
            step.complete(success=False, error=str(e))
            raise PipelineError(f"Extraction failed: {e}", step="extract_text")
        finally:
            metadata.processing_steps.append(step)

    @staticmethod
    def _apply_llm_article(result: dict, article) -> None:
        """Map validated LLM structure onto the existing exporter contract."""
        converted_sections = []
        for section in article.sections:
            canonical_type, confidence, source = classify_section(section.full_heading)
            label = (
                canonical_label(canonical_type)
                if canonical_type not in {"OTHER", "UNKNOWN"}
                else "section"
            )
            first_page = min(section.source_pages) if section.source_pages else 1
            last_page = max(section.source_pages) if section.source_pages else first_page
            converted_sections.append({
                "section_id": f"S{section.order:03d}",
                "order": section.order,
                "level": section.level,
                "parent_id": section.parent,
                "children": [],
                "heading": section.title,
                "original_heading": section.full_heading,
                "label": label,
                "canonical_type": canonical_type,
                "classification_confidence": confidence,
                "classification_source": f"llm+{source}",
                "page": first_page,
                "page_start": first_page,
                "page_end": last_page,
                "direct_content": section.content,
                "content": section.content,
                "aggregate_content": section.content,
                "source_pages": section.source_pages,
                "source_blocks": section.source_blocks,
            })

        result["title"] = article.title or result.get("title") or ""
        result["authors"] = ", ".join(article.authors)
        result["abstract"] = article.abstract or result.get("abstract") or ""
        result["sections"] = converted_sections
        result["body"] = " ".join(section.content for section in article.sections if section.content)
        result["validation"] = {
            "ok": True,
            "method": "llm_hybrid",
            "section_count": len(converted_sections),
            "issues": [],
            "content_source": "pdf_blocks",
        }
        result["article"] = article
        result["extraction"] = article.extraction

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
        metadata.extraction = result.get("extraction") or metadata.extraction or {}
        hash_prefix = metadata.file_hash_sha256[:12] or "unhashed"
        article_id = f"{self._safe_directory_name(base_name)}_{hash_prefix}"
        article_dir = PDF_OUTPUT_ROOT / article_id
        article_dir.mkdir(parents=True, exist_ok=True)
        metadata.output_directory = str(article_dir)

        # Remove only previously generated output for this exact article.
        for old_file in article_dir.iterdir():
            if old_file.is_file() and old_file.suffix.lower() in {".txt", ".json"}:
                old_file.unlink()
        sections_dir = article_dir / "sections"
        if sections_dir.exists():
            shutil.rmtree(sections_dir)

        structured_payload = {
            "schemaVersion": "2.0",
            "articleId": article_id,
            "source": metadata.source,
            "source_file": Path(metadata.file_path).name,
            "sha256": metadata.file_hash_sha256,
            "pageCount": metadata.page_count,
            "title": metadata.title,
            "authors": metadata.authors,
            "abstract": metadata.abstract,
            "keywords": metadata.keywords,
            "affiliations": metadata.affiliations,
            "documentBlocks": result.get("blocks", metadata.document_blocks),
            "headings": result.get("headings", metadata.headings),
            "sections": result.get("sections", metadata.sections),
            "validation": metadata.validation_report,
            "languageDecision": metadata.language_decision,
            "extraction": metadata.extraction,
        }
        structured_path = article_dir / "structured_article.json"
        structured_path.write_text(json.dumps(structured_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        metadata.structured_document_file = str(structured_path)
        metadata_payload = {
            "articleId": article_id,
            "structuredDocument": structured_path.name,
            **{key: structured_payload[key] for key in (
                "source", "source_file", "sha256", "pageCount", "title", "authors", "abstract",
                "keywords", "affiliations",
                "validation", "languageDecision",
                "extraction",
            )},
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

        article = result.get("article")
        if article is not None:
            metadata.extracted_files.extend(LLMArticleExporter().export(article, article_dir))

        self._save_sections(result["sections"], sections_dir, metadata)
        self._register_processed_pdf(metadata, article_dir)

    @staticmethod
    def _llm_cache_matches(payload: dict) -> bool:
        extraction = payload.get("extraction") or {}
        settings = PDFLLMSettings.from_env()
        return (
            extraction.get("method") == "llm_hybrid"
            and extraction.get("model") == settings.model
            and extraction.get("prompt_version") == settings.prompt_version
            and extraction.get("text_normalization") == PDF_TEXT_NORMALIZATION_VERSION
        )

    @staticmethod
    def _hash_index_path() -> Path:
        return PDF_OUTPUT_ROOT / PDF_HASH_INDEX_FILE

    def _load_processed_pdf_index(self) -> dict[str, dict]:
        """Load a SHA-256 index; build it once from existing metadata if needed."""
        index_path = self._hash_index_path()
        try:
            if index_path.exists():
                raw = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except (OSError, ValueError, json.JSONDecodeError):
            logger.warning("Cannot read PDF hash index; rebuilding it.")

        index: dict[str, dict] = {}
        if PDF_OUTPUT_ROOT.exists():
            for metadata_path in PDF_OUTPUT_ROOT.rglob("metadata.json"):
                try:
                    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                    file_hash = str(payload.get("sha256") or "").lower()
                    if len(file_hash) == 64:
                        index[file_hash] = {
                            "metadata_file": str(metadata_path),
                            "output_directory": str(metadata_path.parent),
                            "structured_document_file": str(metadata_path.parent / payload.get("structuredDocument", "structured_article.json")),
                        }
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        self._write_processed_pdf_index(index)
        return index

    def _write_processed_pdf_index(self, index: dict[str, dict]) -> None:
        PDF_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        index_path = self._hash_index_path()
        temporary_path = index_path.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(index_path)

    def _find_processed_pdf(self, file_hash: str) -> dict | None:
        entry = self._load_processed_pdf_index().get(file_hash.lower())
        if not entry:
            return None
        metadata_path = Path(str(entry.get("metadata_file") or ""))
        if not metadata_path.exists():
            return None
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return {**entry, "payload": payload}

    def _populate_duplicate_metadata(self, metadata: ExtractedMetadata, existing: dict) -> None:
        payload = existing.get("payload", {})
        metadata.is_duplicate = True
        metadata.duplicate_of = {
            "hash": metadata.file_hash_sha256,
            "metadataFile": str(existing.get("metadata_file") or ""),
            "outputDirectory": str(existing.get("output_directory") or ""),
        }
        metadata.title = str(payload.get("title") or "")
        metadata.authors = str(payload.get("authors") or "")
        metadata.abstract = str(payload.get("abstract") or "")
        metadata.page_count = int(payload.get("pageCount") or 0)
        metadata.validation_report = payload.get("validation") or {}
        metadata.language_decision = payload.get("languageDecision") or {}
        metadata.extraction = payload.get("extraction") or {}
        metadata.keywords = list(payload.get("keywords") or [])
        metadata.affiliations = list(payload.get("affiliations") or [])
        metadata.output_directory = str(existing.get("output_directory") or "")
        metadata.metadata_file = str(existing.get("metadata_file") or "")
        metadata.structured_document_file = str(existing.get("structured_document_file") or "")
        metadata.steps_completed.append("duplicate_check")

    def _register_processed_pdf(self, metadata: ExtractedMetadata, article_dir: Path) -> None:
        if not metadata.file_hash_sha256:
            return
        index = self._load_processed_pdf_index()
        index[metadata.file_hash_sha256.lower()] = {
            "metadata_file": metadata.metadata_file,
            "output_directory": str(article_dir),
            "structured_document_file": metadata.structured_document_file,
        }
        self._write_processed_pdf_index(index)

    def _save_sections(
        self,
        sections: list,
        out_dir: Path,
        metadata: ExtractedMetadata,
    ) -> None:
        """Lưu các section đã được PyMuPDF phân tách thành từng file .txt."""
        out_dir.mkdir(parents=True, exist_ok=True)
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
            content = sec.get("direct_content", sec.get("content", "")).strip()
            if not content:
                continue

            label     = sec.get("label", "section")
            canonical = str(sec.get("canonical_type", label)).casefold()
            heading   = sec.get("heading", label)
            sec_name  = LABEL_DISPLAY.get(label, heading)
            label_counts[canonical] = label_counts.get(canonical, 0) + 1
            occurrence = label_counts[canonical]
            suffix = "" if occurrence == 1 else f"_{occurrence}"
            stem = f"{int(sec.get('order', occurrence)):02d}_{canonical}{suffix}"
            text_path = out_dir / f"{stem}.txt"
            json_path = out_dir / f"{stem}.json"
            json_path.write_text(json.dumps(sec, ensure_ascii=False, indent=2), encoding="utf-8")
            # Abstract already has a canonical root-level abstract.txt, while
            # its section JSON still retains hierarchy and provenance.
            if label != "abstract":
                text_path.write_text(content, encoding="utf-8")

            preview = content[:300].strip()
            if len(content) > 300:
                preview += "..."

            metadata.extracted_files.append({
                "file_path":       str(text_path) if label != "abstract" else str(json_path),
                "json_file_path":  str(json_path),
                "section_name":    sec_name,
                "heading":         heading,
                "label":           label,
                "canonical_type":  sec.get("canonical_type", "OTHER"),
                "level":           sec.get("level", 1),
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
