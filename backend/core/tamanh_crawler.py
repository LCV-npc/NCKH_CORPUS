"""Tâm Anh public medical Q&A crawler.

The crawler deliberately performs an HTML-to-text extraction only.  It neither
uses an LLM nor changes the medical meaning of the published answers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Callable, Iterable, Optional, Protocol
from urllib.parse import urljoin, urlparse, urlunparse
import unicodedata
import uuid

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config.crawler import TamanhCrawlerSettings
from core.tamanh_metadata_repository import TamanhQaMetadataRepository


LOGGER = logging.getLogger(__name__)
SOURCE_NAME = "Tâm Anh Hospital"
_PHONE_OR_CONTACT = re.compile(
    r"(?:hotline|liên hệ|đặt lịch|điện thoại|email|\b0\d{8,}\b|\b\d{3}[ .-]\d{3}[ .-]\d{3,}\b)",
    re.IGNORECASE,
)


class MedicalCrawler(Protocol):
    def crawl(self, request: "TamanhCrawlRequest", status: "TamanhJobStatus") -> None:
        """Run a crawler request and continuously update its status."""


@dataclass(frozen=True)
class TamanhCrawlRequest:
    source_url: str = "https://tamanhhospital.vn/"
    start_year: Optional[int] = None
    end_year: Optional[int] = None


@dataclass
class TamanhQaRecord:
    category: str
    question_title: Optional[str]
    patient_question: Optional[str]
    doctor_name: Optional[str]
    doctor_answer: Optional[str]
    source_url: str
    category_url: str
    published_year: Optional[int] = None


@dataclass
class TamanhJobStatus:
    job_id: str
    status: str = "QUEUED"
    categories_found: int = 0
    categories_processed: int = 0
    pages_processed: int = 0
    questions_found: int = 0
    answers_found: int = 0
    files_created: int = 0
    duplicates_skipped: int = 0
    errors: int = 0
    year_filtered: int = 0
    current_category: Optional[str] = None
    current_url: Optional[str] = None
    message: Optional[str] = None
    output_dir: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    log_messages: list[str] = field(default_factory=list)
    stop_requested: bool = False

    def add_log(self, message: str) -> None:
        stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self.log_messages.append(stamped)
        del self.log_messages[:-300]
        LOGGER.info(message)

    def public(self) -> dict:
        payload = asdict(self)
        payload.pop("stop_requested", None)
        return payload


@dataclass(frozen=True)
class TamanhCategory:
    name: str
    url: str


def normalize_text(value: str) -> str:
    """Decode/flatten HTML text without altering case, accents, or punctuation."""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value)
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    normalized = normalized.replace("đ", "d").replace("Đ", "D").lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_") or "khong_phan_loai"


def qa_fingerprint(question: str, answer: str) -> str:
    canonical = f"{normalize_text(question).casefold()}\n{normalize_text(answer).casefold()}"
    return sha256(canonical.encode("utf-8")).hexdigest()


class TamanhHtmlParser:
    """Site-specific parsing, kept separate from fetching and file persistence."""

    @staticmethod
    def canonical_url(href: str, base_url: str) -> Optional[str]:
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return None
        parsed = urlparse(urljoin(base_url, href))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/" and not path.endswith("/"):
            path += "/"
        return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", "", ""))

    @staticmethod
    def same_domain(url: str, base_url: str) -> bool:
        host = urlparse(url).hostname or ""
        base_host = urlparse(base_url).hostname or ""
        return host.removeprefix("www.").casefold() == base_host.removeprefix("www.").casefold()

    def discover_tu_van_url(self, html: str, base_url: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")
        candidates: list[tuple[int, str]] = []
        for anchor in soup.select("a[href]"):
            url = self.canonical_url(anchor.get("href", ""), base_url)
            if not url or not self.same_domain(url, base_url):
                continue
            if urlparse(url).path.rstrip("/") != "/tu-van":
                continue
            text = normalize_text(anchor.get_text(" ", strip=True)).casefold()
            score = 2 if "tư vấn" in text or "tu van" in text else 1
            candidates.append((score, url))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]

    def discover_categories(self, html: str, root_url: str) -> list[TamanhCategory]:
        soup = BeautifulSoup(html, "html.parser")
        root_path = urlparse(root_url).path.rstrip("/")
        primary: list[TamanhCategory] = []
        fallback: list[TamanhCategory] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            url = self.canonical_url(anchor.get("href", ""), root_url)
            if not url or not self.same_domain(url, root_url):
                continue
            segments = [part for part in urlparse(url).path.split("/") if part]
            if len(segments) != 2 or segments[0] != root_path.strip("/") or segments[1] == "page":
                continue
            name = normalize_text(anchor.get_text(" ", strip=True))
            if not name or url == root_url or url in seen:
                continue
            category = TamanhCategory(name=name, url=url)
            # Current site marks the category navigation links semantically; a
            # path-only fallback remains available if that class is renamed.
            if "title_catetuvan" in anchor.get("class", []):
                primary.append(category)
                seen.add(url)
            elif self._is_category_link_context(anchor):
                fallback.append(category)
                seen.add(url)
        return primary or fallback

    @staticmethod
    def _is_category_link_context(anchor: Tag) -> bool:
        context = " ".join(
            normalize_text(parent.get_text(" ", strip=True)).casefold()
            for parent in list(anchor.parents)[:4]
            if isinstance(parent, Tag)
        )
        return "chuyên mục tư vấn" in context

    def detail_links(self, html: str, page_url: str, category_urls: set[str]) -> set[str]:
        soup = BeautifulSoup(html, "html.parser")
        root_path = "/tu-van/"
        result: set[str] = set()
        for anchor in soup.select("a[href]"):
            url = self.canonical_url(anchor.get("href", ""), page_url)
            if not url or not self.same_domain(url, page_url) or url in category_urls:
                continue
            path = urlparse(url).path
            segments = [part for part in path.split("/") if part]
            # Detail Q&A posts are direct children of /tu-van/.  This excludes
            # pagination, forms, external pages, and category navigation.
            if path.startswith(root_path) and len(segments) == 2 and segments[1] != "page":
                result.add(url)
        return result

    def pagination_links(self, html: str, page_url: str, category_url: str) -> set[str]:
        soup = BeautifulSoup(html, "html.parser")
        category_path = urlparse(category_url).path.rstrip("/")
        expression = re.compile(re.escape(category_path) + r"/page/\d+/?$")
        result: set[str] = set()
        for anchor in soup.select("a[href]"):
            url = self.canonical_url(anchor.get("href", ""), page_url)
            if url and self.same_domain(url, category_url) and expression.fullmatch(urlparse(url).path):
                result.add(url)
        return result

    def parse_qa(self, html: str, source_url: str, category: TamanhCategory) -> Optional[TamanhQaRecord]:
        soup = BeautifulSoup(html, "html.parser")
        question_box = soup.select_one(".box_tuvan")
        answer_box = soup.select_one(".tuvan_detail")
        if not question_box or not answer_box:
            return None

        title_tag = soup.select_one(".box_detail .title h1, .box_detail h1")
        title = normalize_text(title_tag.get_text(" ", strip=True)) if title_tag else None

        question_parts = []
        for child in question_box.find_all(["div", "p"], recursive=False):
            classes = set(child.get("class", []))
            if "cl_brand" in classes:
                continue  # public patient name is intentionally not collected
            text = normalize_text(child.get_text(" ", strip=True))
            if text:
                question_parts.append(text)
        question = normalize_text(" ".join(question_parts))
        if not question:
            return None

        doctor_tag = answer_box.select_one(".font_helB")
        doctor_name = normalize_text(doctor_tag.get_text(" ", strip=True)) if doctor_tag else None
        answer_parts = []
        for paragraph in answer_box.find_all("p", recursive=False):
            text = normalize_text(paragraph.get_text(" ", strip=True))
            if not text or _PHONE_OR_CONTACT.search(text):
                continue
            if text.casefold().startswith("cảm ơn bạn đã gửi câu hỏi"):
                continue
            answer_parts.append(text)
        # Paragraph boundaries are meaningful presentation in a doctor answer;
        # whitespace inside each paragraph has already been normalized above.
        answer = "\n\n".join(answer_parts).strip()
        if not answer:
            return None

        published_year = None
        published = soup.find("meta", attrs={"property": "article:published_time"})
        if published and published.get("content"):
            match = re.match(r"(\d{4})", published["content"])
            if match:
                published_year = int(match.group(1))

        return TamanhQaRecord(
            category=category.name,
            question_title=title,
            patient_question=question,
            doctor_name=doctor_name or None,
            doctor_answer=answer,
            source_url=source_url,
            category_url=category.url,
            published_year=published_year,
        )


class TamanhFileExporter:
    """Writes paired files and atomically maintains source metadata."""

    def __init__(self, output_dir: Path, metadata_repository: Optional[TamanhQaMetadataRepository] = None):
        self.output_dir = output_dir
        self.metadata_repository = metadata_repository
        self.tu_van_dir = output_dir / "tu_van"
        self.metadata_path = output_dir / "metadata.json"
        self._metadata = self._load_metadata()
        self._fingerprints = {item["fingerprint"] for item in self._metadata if item.get("fingerprint")}
        if self.metadata_repository:
            self._fingerprints.update(self.metadata_repository.fingerprints())
        self._next_numbers: dict[str, int] = {}
        for item in self._metadata:
            match = re.search(r"_(\d{6})$", str(item.get("id", "")))
            if match:
                slug = str(item["id"]).rsplit("_", 1)[0]
                self._next_numbers[slug] = max(self._next_numbers.get(slug, 0), int(match.group(1)))

    def _load_metadata(self) -> list[dict]:
        if not self.metadata_path.is_file():
            return []
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Cannot read existing Tâm Anh metadata; preserving no prior fingerprints.")
            return []

    def is_duplicate(self, record: TamanhQaRecord) -> bool:
        return qa_fingerprint(record.patient_question or "", record.doctor_answer or "") in self._fingerprints

    def export(self, record: TamanhQaRecord) -> dict:
        category_slug = slugify(record.category)
        next_number = self._next_numbers.get(category_slug, 0) + 1
        self._next_numbers[category_slug] = next_number
        identifier = f"{category_slug}_{next_number:06d}"
        category_dir = self.tu_van_dir / category_slug
        category_dir.mkdir(parents=True, exist_ok=True)
        question_name = f"{identifier}_BN.txt"
        answer_name = f"{identifier}_BS.txt"
        question_path = category_dir / question_name
        answer_path = category_dir / answer_name
        question_path.write_text(record.patient_question or "", encoding="utf-8")
        answer_text = record.doctor_answer or ""
        if record.doctor_name:
            answer_text = f"Bác sĩ: {record.doctor_name}\n\n{answer_text}"
        answer_path.write_text(answer_text, encoding="utf-8")
        fingerprint = qa_fingerprint(record.patient_question or "", record.doctor_answer or "")
        metadata = {
            "id": identifier,
            "source": SOURCE_NAME,
            "category": record.category,
            "questionFile": (Path("tu_van") / category_slug / question_name).as_posix(),
            "answerFile": (Path("tu_van") / category_slug / answer_name).as_posix(),
            "sourceUrl": record.source_url,
            "categoryUrl": record.category_url,
            "doctorName": record.doctor_name,
            "questionTitle": record.question_title,
            "publishedYear": record.published_year,
            "crawledAt": datetime.now(timezone.utc).isoformat(),
            "fingerprint": fingerprint,
        }
        try:
            if self.metadata_repository:
                self.metadata_repository.upsert(metadata)
        except Exception:
            question_path.unlink(missing_ok=True)
            answer_path.unlink(missing_ok=True)
            raise
        self._metadata.append(metadata)
        self._fingerprints.add(fingerprint)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.metadata_path)
        return metadata


class TamanhMedicalCrawler:
    def __init__(self, settings: Optional[TamanhCrawlerSettings] = None, db_config: Optional[dict] = None):
        self.settings = settings or TamanhCrawlerSettings()
        self._db_config = dict(db_config or {})
        self.metadata_repository: Optional[TamanhQaMetadataRepository] = None
        self.parser = TamanhHtmlParser()
        self._session = requests.Session()
        retry = Retry(
            total=self.settings.max_retries,
            backoff_factor=0.75,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retry))
        self._session.headers.update({
            "User-Agent": "MedicalCorpusResearchBot/1.0 (+medical-corpus; respectful-rate-limit)",
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.7",
        })
        self._last_request_at = 0.0
        self._disallowed_paths: list[str] = []

    def crawl(self, request: TamanhCrawlRequest, status: TamanhJobStatus) -> None:
        base_url = self._validate_source_url(request.source_url)
        status.status = "RUNNING"
        status.started_at = datetime.now(timezone.utc).isoformat()
        status.output_dir = str(self.settings.output_dir / "tu_van")
        status.add_log("Starting Tâm Anh crawler")
        status.add_log(f"Root URL: {base_url}")
        try:
            # Metadata is mandatory for this corpus.  Existing JSON metadata is
            # imported first so moving the storage directory loses no provenance.
            self.metadata_repository = TamanhQaMetadataRepository(self._db_config)
            self.metadata_repository.ensure_table()
            self.metadata_repository.sync_metadata_file(self.settings.output_dir / "metadata.json")
            self._load_robots(base_url, status)
            home_html = self._get_html(base_url, status)
            tu_van_url = self.parser.discover_tu_van_url(home_html, base_url)
            if not tu_van_url:
                raise ValueError("Không tìm thấy khu vực Tư vấn trên website.")
            root_html = self._get_html(tu_van_url, status)
            categories = self.parser.discover_categories(root_html, tu_van_url)
            if not categories:
                raise ValueError("Không tìm thấy chuyên khoa trong khu vực Tư vấn trên website.")
            status.categories_found = len(categories)
            status.add_log(f"Found /tu-van/: {tu_van_url}")
            status.add_log(f"Found {len(categories)} categories")
            self._crawl_categories(categories, request, status)
            status.status = "STOPPED" if status.stop_requested else "COMPLETED"
            status.message = "Đã dừng theo yêu cầu." if status.stop_requested else "Hoàn tất thu thập Tâm Anh."
        except Exception as exc:  # errors are exposed as status rather than silently lost in the thread
            status.errors += 1
            status.status = "ERROR"
            status.message = str(exc)
            status.add_log(f"ERROR: {exc}")
        finally:
            status.completed_at = datetime.now(timezone.utc).isoformat()
            self._session.close()

    def _validate_source_url(self, source_url: str) -> str:
        candidate = (source_url or self.settings.base_url).strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL nguồn không hợp lệ. Vui lòng nhập URL đầy đủ https://...")
        configured_host = (urlparse(self.settings.base_url).hostname or "").removeprefix("www.").casefold()
        candidate_host = (parsed.hostname or "").removeprefix("www.").casefold()
        if candidate_host != configured_host:
            raise ValueError("Phiên bản hiện tại chỉ hỗ trợ domain tamanhhospital.vn.")
        return self.parser.canonical_url(candidate, candidate) or self.settings.base_url

    def _load_robots(self, base_url: str, status: TamanhJobStatus) -> None:
        robots_url = urljoin(base_url, "/robots.txt")
        response = self._request(robots_url, status, check_robots=False)
        if response.status_code == 404:
            return
        if response.status_code != 200:
            raise RuntimeError(f"Không thể kiểm tra robots.txt (HTTP {response.status_code}).")
        applies = False
        for raw_line in response.text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            if key.casefold() == "user-agent":
                applies = value == "*"
            elif applies and key.casefold() == "disallow" and value:
                self._disallowed_paths.append(value)

    def _allowed_by_robots(self, url: str) -> bool:
        path = urlparse(url).path
        return not any(path.startswith(disallowed) for disallowed in self._disallowed_paths)

    def _request(self, url: str, status: TamanhJobStatus, check_robots: bool = True) -> requests.Response:
        if check_robots and not self._allowed_by_robots(url):
            raise RuntimeError(f"URL bị robots.txt chặn: {url}")
        delay_seconds = max(0, self.settings.request_delay_ms) / 1000
        remaining = delay_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)
        response = self._session.get(url, timeout=max(1, self.settings.timeout_ms) / 1000)
        self._last_request_at = time.monotonic()
        if response.status_code in {403, 429}:
            raise RuntimeError(f"Website từ chối hoặc giới hạn truy cập (HTTP {response.status_code}): {url}")
        if response.status_code == 404:
            raise RuntimeError(f"Không tìm thấy trang (HTTP 404): {url}")
        if response.status_code != 200:
            raise RuntimeError(f"Không thể tải trang (HTTP {response.status_code}): {url}")
        return response

    def _get_html(self, url: str, status: TamanhJobStatus) -> str:
        status.current_url = url
        return self._request(url, status).text

    def _crawl_categories(self, categories: Iterable[TamanhCategory], request: TamanhCrawlRequest, status: TamanhJobStatus) -> None:
        category_list = list(categories)
        category_urls = {category.url for category in category_list}
        if not self.metadata_repository:
            raise RuntimeError("Metadata repository Tâm Anh chưa được khởi tạo.")
        exporter = TamanhFileExporter(self.settings.output_dir, self.metadata_repository)
        seen_detail_urls: set[str] = set()
        for category in category_list:
            if status.stop_requested:
                return
            status.current_category = category.name
            status.add_log(f"Processing category: {category.name}")
            pages_to_visit = [category.url]
            visited_pages: set[str] = set()
            while pages_to_visit and not status.stop_requested:
                page_url = pages_to_visit.pop(0)
                if page_url in visited_pages:
                    continue
                visited_pages.add(page_url)
                try:
                    html = self._get_html(page_url, status)
                    status.pages_processed += 1
                    status.add_log(f"Processing page: {page_url}")
                    new_details = self.parser.detail_links(html, page_url, category_urls) - seen_detail_urls
                    seen_detail_urls.update(new_details)
                    for detail_url in sorted(new_details):
                        if status.stop_requested:
                            return
                        self._crawl_detail(detail_url, category, request, exporter, status)
                    for pagination_url in self.parser.pagination_links(html, page_url, category.url):
                        if pagination_url not in visited_pages:
                            pages_to_visit.append(pagination_url)
                except Exception as exc:
                    status.errors += 1
                    status.add_log(f"ERROR Failed URL: {page_url} ({exc})")
            status.categories_processed += 1

    def _crawl_detail(self, detail_url: str, category: TamanhCategory, request: TamanhCrawlRequest, exporter: TamanhFileExporter, status: TamanhJobStatus) -> None:
        try:
            html = self._get_html(detail_url, status)
            record = self.parser.parse_qa(html, detail_url, category)
            if not record:
                status.errors += 1
                status.add_log(f"No valid Q&A structure found: {detail_url}")
                return
            if record.published_year is not None and (
                (request.start_year is not None and record.published_year < request.start_year)
                or (request.end_year is not None and record.published_year > request.end_year)
            ):
                status.year_filtered += 1
                return
            status.questions_found += 1
            status.answers_found += 1
            if exporter.is_duplicate(record):
                status.duplicates_skipped += 1
                status.add_log(f"Duplicate skipped: {detail_url}")
                return
            metadata = exporter.export(record)
            status.files_created += 2
            status.add_log(f"Created: {metadata['questionFile']}")
            status.add_log(f"Created: {metadata['answerFile']}")
        except Exception as exc:
            status.errors += 1
            status.add_log(f"ERROR Failed URL: {detail_url} ({exc})")


class TamanhCrawlerJobManager:
    """In-process background jobs; isolated so this can later move to a queue."""

    def __init__(self, crawler_factory: Optional[Callable[[], MedicalCrawler]] = None):
        self._crawler_factory = crawler_factory
        self._db_config: dict = {}
        self._jobs: dict[str, TamanhJobStatus] = {}
        self._lock = threading.Lock()

    def configure_db(self, db_config: dict) -> None:
        self._db_config = dict(db_config)

    def start(self, request: TamanhCrawlRequest) -> TamanhJobStatus:
        with self._lock:
            if any(job.status in {"QUEUED", "RUNNING"} for job in self._jobs.values()):
                raise RuntimeError("Đang có một Tâm Anh crawler chạy.")
            status = TamanhJobStatus(job_id=str(uuid.uuid4()))
            self._jobs[status.job_id] = status
        thread = threading.Thread(target=self._run, args=(request, status), daemon=True)
        thread.start()
        return status

    def _run(self, request: TamanhCrawlRequest, status: TamanhJobStatus) -> None:
        crawler = self._crawler_factory() if self._crawler_factory else TamanhMedicalCrawler(db_config=self._db_config)
        crawler.crawl(request, status)

    def get(self, job_id: str) -> Optional[TamanhJobStatus]:
        with self._lock:
            return self._jobs.get(job_id)

    def stop(self, job_id: str) -> Optional[TamanhJobStatus]:
        status = self.get(job_id)
        if status:
            status.stop_requested = True
            status.add_log("Đã nhận yêu cầu dừng crawler.")
        return status
