import re
import time
import random
import os
import threading
import hashlib
from pathlib import Path
from collections import deque
import requests
import mysql.connector
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from urllib.parse import urlparse, urljoin
from config.constants import MAX_FILE_SIZE_BYTES, PDF_MAGIC_BYTES
from config.language_filter import VietnameseCorpusSettings
from core.language_audit import (
    LanguageAuditRepository,
    ensure_language_audit_schema,
    is_allowed_journal_url,
    quarantine_pdf,
)
from core.language_validation import (
    LANGUAGE_VALIDATION_VERSION,
    AdmissionDecision,
    assess_metadata,
    decide_admission,
    select_pdf_text_for_language,
)
from pdf_extractor import extract_from_pdf_path

# Trạng thái scraping toàn cục — được đọc bởi /api/status
scrape_status: dict = {
    "running": False, "success": 0, "skipped": 0, "duplicates": 0,
    "rejected_english": 0, "rejected_mixed": 0, "rejected_no_text": 0,
    "quarantined": 0, "pages_processed": 0, "total_urls": 0,
    "current_year": None, "current_url": None, "error": None,
    "done": False, "summary": None, "log_messages": [],
}

_MAX_LOGS = 300
_stop_requested = False
_log_id = None

def stop_scraping():
    global _stop_requested
    _stop_requested = True

def _log(msg: str):
    """Ghi log realtime, không thêm lại cùng một thông điệp liên tiếp."""
    messages = scrape_status["log_messages"]
    if messages and messages[-1] == msg:
        return
    messages.append(msg)
    if len(messages) > _MAX_LOGS:
        del messages[:-_MAX_LOGS]
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('utf-8', 'replace').decode('cp1252', 'ignore'))

def clean_filename(fn: str) -> str:
    fn = re.sub(r'[\t\n\r\f\v]+', ' ', fn)
    fn = re.sub(r'[\\/*?"<>|]', "", fn)
    return re.sub(r'\s+', ' ', fn)[:150].strip()

def _make_absolute(href: str, base: str) -> str:
    """FIX #3: Chuyển mọi href (tương đối hoặc tuyệt đối) về URL đầy đủ."""
    return urljoin(base, href)


def _html_language(soup: BeautifulSoup) -> str:
    html = soup.find("html")
    if html and html.get("lang"):
        return str(html.get("lang"))
    for name in ("citation_language", "DC.Language", "language"):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag.get("content"))
    return ""


def _find_pdf_url(soup: BeautifulSoup, base_url: str) -> str | None:
    meta_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
    pdf_url = meta_pdf.get("content") if meta_pdf else None
    if not pdf_url:
        for a_tag in soup.find_all("a", href=True):
            text_lower = a_tag.get_text(strip=True).lower()
            classes = " ".join(a_tag.get("class", []))
            href_val = a_tag["href"]
            if ("pdf" in text_lower or "pdf" in classes.lower()) and (
                "/article/view/" in href_val or "/article/download/" in href_val
            ):
                pdf_url = _make_absolute(href_val, base_url)
                break
    if pdf_url and "/article/view/" in pdf_url:
        pdf_url = pdf_url.replace("/article/view/", "/article/download/")
    return pdf_url


def _candidate_pdf_path(site_folder: str, target_year: str, title: str, source_url: str, settings: VietnameseCorpusSettings) -> Path:
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
    candidate_dir = settings.candidates_dir / site_folder / target_year
    candidate_dir.mkdir(parents=True, exist_ok=True)
    return candidate_dir / f"{title}_{digest}.pdf"


def _download_candidate(session: requests.Session, pdf_url: str, candidate_path: Path) -> str | None:
    """Download a bounded PDF candidate. Returns an error reason, if any."""
    response = session.get(pdf_url, verify=False, timeout=120, stream=True)
    if response.status_code != 200:
        return f"PDF_HTTP_{response.status_code}"
    content_type = response.headers.get("Content-Type", "").lower()
    if "pdf" not in content_type and "octet-stream" not in content_type:
        return "PDF_CONTENT_TYPE_INVALID"
    written = 0
    with open(candidate_path, "wb") as destination:
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            written += len(chunk)
            if written > MAX_FILE_SIZE_BYTES:
                destination.close()
                candidate_path.unlink(missing_ok=True)
                return "PDF_TOO_LARGE"
            destination.write(chunk)
    with open(candidate_path, "rb") as source:
        if source.read(4) != PDF_MAGIC_BYTES:
            return "PDF_MAGIC_INVALID"
    return None


def _accept_candidate(candidate_path: Path) -> Path:
    """Mark a validated candidate as accepted without creating a duplicate.

    ``candidates/<source-domain>/<year>`` is the sole on-disk PDF store for
    successful crawler downloads. Rejected files are moved to quarantine;
    accepted files remain at their original, traceable candidate path.
    """
    return candidate_path


def _record_rejection(status: str) -> None:
    scrape_status["skipped"] += 1
    if status == "REJECTED_ENGLISH":
        scrape_status["rejected_english"] += 1
    elif status == "REJECTED_MIXED":
        scrape_status["rejected_mixed"] += 1
    elif status in {"REJECTED_NO_TEXT", "REJECTED_NO_PDF"}:
        scrape_status["rejected_no_text"] += 1


def _same_host(first_url: str, second_url: str) -> bool:
    return urlparse(first_url).netloc.lower() == urlparse(second_url).netloc.lower()


def _year_from_text(value: str) -> str | None:
    match = re.search(r"(?<!\d)(20\d{2})(?!\d)", value or "")
    return match.group(1) if match else None


def _issue_year(anchor) -> str | None:
    """Get the closest OJS archive year without a brittle CSS selector."""
    direct = _year_from_text(anchor.get_text(" ", strip=True))
    if direct:
        return direct
    parent = anchor.parent
    for _ in range(3):
        if not parent:
            break
        text = parent.get_text(" ", strip=True)
        if len(text) <= 220:
            parent_year = _year_from_text(text)
            if parent_year:
                return parent_year
        parent = parent.parent
    for heading in anchor.find_all_previous(["h1", "h2", "h3", "h4"]):
        candidate = heading.get_text(" ", strip=True)
        if re.fullmatch(r"20\d{2}", candidate):
            return candidate
    return None


def _archive_issue_links(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Find issue links and the displayed year on one OJS archive page."""
    issues: list[dict] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        url = _make_absolute(anchor["href"], page_url)
        if "/issue/view/" not in url or not _same_host(page_url, url) or url in seen:
            continue
        seen.add(url)
        issues.append({
            "url": url,
            "name": clean_filename(anchor.get_text(" ", strip=True)),
            "year": _issue_year(anchor),
        })
    return issues


def _archive_next_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Follow actual OJS pagination links rather than assuming `/1`, `/2`, ..."""
    result: list[str] = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True).casefold()
        rel = " ".join(anchor.get("rel", [])).casefold()
        classes = " ".join(anchor.get("class", [])).casefold()
        if not ("next" in rel or "next" in classes or "sau" in text or "next" in text or text in {"→", "›", ">"}):
            continue
        url = _make_absolute(anchor["href"], page_url)
        if "/issue/archive" in url and _same_host(page_url, url) and url not in result:
            result.append(url)
    return result


def _article_view_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Collect canonical OJS article pages, excluding PDF child URLs."""
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        url = _make_absolute(anchor["href"], page_url).split("#", 1)[0]
        if not _same_host(page_url, url):
            continue
        parsed = urlparse(url)
        if not re.search(r"/article/view/\d+/?$", parsed.path):
            continue
        canonical = parsed._replace(query="").geturl().rstrip("/")
        if canonical not in links:
            links.append(canonical)
    return links

def run_scraping(request, db_config: dict, output_folder: str):
    """
    Thu thập bài báo từ tạp chí OJS.

    Parameters
    ----------
    request       : ScrapeRequest (start_year, end_year, target_url)
    db_config     : dict kết nối MySQL (từ main.py)
    output_folder : đường dẫn thư mục lưu file .txt
    """
    global scrape_status
    scrape_status.clear()
    scrape_status.update({
        "running": True, "success": 0, "skipped": 0, "duplicates": 0,
        "rejected_english": 0, "rejected_mixed": 0, "rejected_no_text": 0,
        "quarantined": 0, "pages_processed": 0, "total_urls": 0,
        "current_year": None, "current_url": None, "error": None,
        "done": False, "summary": None, "log_messages": [],
    })
    global _stop_requested, _log_id
    _stop_requested = False
    _log_id = None

    _log(f"Bắt đầu cào: {request.target_url} | Năm {request.start_year} – {request.end_year}")
    conn = cursor = None
    try:
        # ── FIX #1: Kiểm tra URL hợp lệ trước khi làm bất cứ điều gì ──────
        parsed_check = urlparse(request.target_url)
        if not parsed_check.scheme or not parsed_check.netloc:
            raise ValueError(f"URL không hợp lệ: '{request.target_url}'. Vui lòng nhập đầy đủ https://...")
        settings = VietnameseCorpusSettings()
        if not is_allowed_journal_url(request.target_url, settings):
            allowed = ", ".join(settings.allowed_domains)
            raise ValueError(f"Tên miền không nằm trong danh sách nguồn được phép: {allowed}")

        _log("Đang kết nối tới DB...")
        conn   = mysql.connector.connect(**db_config)
        ensure_language_audit_schema(db_config)
        language_audit = LanguageAuditRepository(db_config)
        _log("Đã kết nối DB thành công.")
        cursor = conn.cursor()

        cursor_dict = conn.cursor(dictionary=True)
        try:
            _log("Đang kiểm tra tiến trình crawl_progress...")
            cursor_dict.execute(
                "SELECT * FROM crawl_progress WHERE target_url=%s AND start_year=%s AND end_year=%s AND status='paused' ORDER BY id DESC LIMIT 1",
                (request.target_url, request.start_year, request.end_year)
            )
            prev_progress = cursor_dict.fetchone()
            if prev_progress:
                scrape_status["success"] = prev_progress["success_count"] or 0
                scrape_status["duplicates"] = prev_progress["duplicate_count"] or 0
                scrape_status["skipped"] = prev_progress["skipped_count"] or 0
                _log_id = prev_progress["id"]
                _log(f"Tiếp tục tiến trình trước đó: đã lưu {scrape_status['success']}, trùng {scrape_status['duplicates']}, bỏ qua {scrape_status['skipped']}")
                cursor_dict.execute("UPDATE crawl_progress SET status='running' WHERE id=%s", (_log_id,))
                conn.commit()
            else:
                cursor_dict.execute(
                    "INSERT INTO crawl_progress (target_url, start_year, end_year, status) VALUES (%s, %s, %s, 'running')",
                    (request.target_url, request.start_year, request.end_year)
                )
                conn.commit()
                _log_id = cursor_dict.lastrowid
                _log(f"Đã tạo bản ghi tiến trình ID={_log_id}")
        except Exception as e:
            _log(f"Không thể kiểm tra tiến trình trong crawl_progress (có thể bảng chưa được tạo): {e}")
        finally:
            cursor_dict.close()

        cursor = conn.cursor()

        # ── FIX #2: Chỉ retry khi lỗi HTTP (5xx/429), KHÔNG retry khi DNS fail ─
        session = requests.Session()
        session.headers.update({
            "User-Agent":     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        retry = Retry(
            total=2,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            raise_on_status=False,
        )
        session.mount("http://",  HTTPAdapter(max_retries=retry))
        session.mount("https://", HTTPAdapter(max_retries=retry))

        base_url    = re.sub(r"/(article|issue)/.*", "", request.target_url).rstrip("/")
        archive_url = f"{base_url}/issue/archive"

        # Tạo tên thư mục từ domain (ví dụ: tapchiyhocvietnam.vn)
        parsed      = urlparse(base_url)
        site_folder = re.sub(r"^www\.", "", parsed.netloc or "unknown")
        site_folder = re.sub(r"[^\w\-\.]", "_", site_folder)
        _log(f"📁 Thư mục lưu trữ: {site_folder}")

        # ── FIX #3: Tìm trang lưu trữ, xử lý URL tương đối ─────────────────
        _log(f"Đang truy cập trang chủ: {base_url} ...")
        try:
            r_home = session.get(base_url, verify=False, timeout=60)
            if r_home.status_code == 200:
                soup_home = BeautifulSoup(r_home.text, "html.parser")
                for a in soup_home.find_all("a", href=True):
                    txt = a.get_text(strip=True).lower()
                    href = a["href"]
                    if any(w in txt for w in ("lưu trữ", "archives", "archive")):
                        archive_url = _make_absolute(href, base_url)
                        _log(f"✅ Tìm thấy trang lưu trữ: {archive_url}")
                        break
            else:
                _log(f"⚠️ Trang chủ trả về HTTP {r_home.status_code}, dùng URL archive mặc định.")
        except requests.exceptions.ConnectionError as ex:
            _log(f"❌ Không thể kết nối đến '{base_url}': {ex}")
            _log("👉 Kiểm tra lại URL hoặc kết nối mạng. Crawler sẽ dừng.")
            return
        except Exception as ex:
            _log(f"⚠️ Lỗi khi tìm trang lưu trữ: {ex}. Dùng URL archive mặc định.")

        _log(f"🗂️ Archive URL sẽ dùng: {archive_url}")

        for yr in range(request.start_year, request.end_year + 1):
            if _stop_requested: break
            target_year = str(yr)
            scrape_status["current_year"] = target_year
            _log(f"\n=== BẮT ĐẦU QUÉT NĂM {target_year} ===")
            issue_links = []

            # Follow the pagination advertised by the archive itself. A page
            # that does not contain the selected year is normal; older years
            # can appear many pages later, so it is never a stop condition.
            pages_to_visit = deque([archive_url])
            visited_archive_pages: set[str] = set()
            while pages_to_visit:
                if _stop_requested:
                    break
                pu = pages_to_visit.popleft()
                if pu in visited_archive_pages:
                    continue
                visited_archive_pages.add(pu)
                _log(f"  Đang fetch archive: {pu}")
                try:
                    r = session.get(pu, verify=False, timeout=60)
                    if r.status_code != 200:
                        _log(f"  ⚠️ Archive trả về HTTP {r.status_code}, bỏ qua trang này.")
                        continue
                    scrape_status["pages_processed"] += 1
                    soup = BeautifulSoup(r.text, "html.parser")
                    discovered = _archive_issue_links(soup, pu)
                    found_on_page = 0
                    for issue in discovered:
                        if issue["year"] == target_year and not any(existing["url"] == issue["url"] for existing in issue_links):
                            issue_links.append({"url": issue["url"], "name": issue["name"]})
                            found_on_page += 1
                    _log(f"  → Archive: tìm thấy {found_on_page} số mới của năm {target_year}")
                    for next_url in _archive_next_links(soup, pu):
                        if next_url not in visited_archive_pages:
                            pages_to_visit.append(next_url)

                except requests.exceptions.ConnectionError as ex:
                    _log(f"  ❌ Lỗi kết nối khi fetch trang {pu}: {ex}")
                    continue
                except Exception as ex:
                    _log(f"  ❌ Lỗi khi fetch page {pu}: {ex}")
                    continue

            if not issue_links:
                _log(f"  (Không tìm thấy số tạp chí nào trong năm {target_year}, bỏ qua)")
                continue

            _log(f"  ✅ Tổng cộng {len(issue_links)} số tạp chí trong năm {target_year}")

            # Duyệt từng số
            for issue_info in issue_links:
                if _stop_requested: break
                iu = issue_info["url"]
                base_issue_name = issue_info["name"]
                scrape_status["current_url"] = iu
                try:
                    _log(f"\n  📰 Đang truy cập số tạp chí: {iu}")
                    resp_issue = session.get(iu, verify=False, timeout=60)
                    if resp_issue.status_code != 200:
                        _log(f"  ⚠️ Số tạp chí trả về HTTP {resp_issue.status_code}, bỏ qua.")
                        continue
                    soup_i = BeautifulSoup(resp_issue.text, "html.parser")

                    issue_name = base_issue_name
                    if not issue_name or len(issue_name) < 4 or issue_name == target_year:
                        h1_tag = soup_i.find("h1")
                        if h1_tag:
                            issue_name = clean_filename(h1_tag.get_text(strip=True))
                        else:
                            issue_name = f"Issue_{target_year}"

                    _log(f"  🔍 Đang quét số tạp chí: {issue_name}...")

                    art_links = _article_view_links(soup_i, iu)
                    scrape_status["total_urls"] += len(art_links)

                    _log(f"  📄 Tìm thấy {len(art_links)} bài báo trong số tạp chí này.")

                    for au in art_links:
                        if _stop_requested: break
                        cursor.execute("SELECT id FROM articles WHERE source_url=%s", (au,))
                        if cursor.fetchone():
                            scrape_status["duplicates"] += 1
                            _log(f"    ⏩ [TRÙNG] {au.split('/')[-1]}")
                            continue
                        previous = language_audit.previous_decision(au)
                        if (
                            previous
                            and previous["status"].startswith("REJECTED")
                            and previous["validation_version"] == LANGUAGE_VALIDATION_VERSION
                        ):
                            scrape_status["duplicates"] += 1
                            _log(f"    ⏩ [ĐÃ KIỂM DUYỆT - {previous['status']}] {au.split('/')[-1]}")
                            continue

                        time.sleep(random.uniform(1.5, 3.0))
                        try:
                            s  = BeautifulSoup(
                                session.get(au, verify=False, timeout=60).text, "html.parser"
                            )
                            tt = (
                                s.find("meta", attrs={"name": "citation_title"})
                                or s.find("meta", attrs={"name": "DC.Title"})
                            )
                            title = (
                                tt["content"].strip() if tt
                                else (s.find("h1").get_text(strip=True) if s.find("h1") else "Không có tiêu đề")
                            )
                            ma = s.find_all("meta", attrs={"name": "citation_author"})
                            authors = (
                                ", ".join(a.get("content", "").strip() for a in ma) if ma
                                else (
                                    s.select_one(".authors,.author") or
                                    type("", (), {"get_text": lambda *a, **k: "Không rõ tác giả"})()
                                ).get_text(strip=True)
                            )

                            # Trích xuất tóm tắt — ưu tiên selector OJS chuẩn trước
                            abstract = ""
                            ab = s.select_one(
                                ".item.abstract, section.abstract, .article-abstract,"
                                " .abstract, .article-details-abstract"
                            )
                            if ab:
                                abstract = ab.get_text(separator=" ").strip()
                            else:
                                # Fallback for OJS themes that do not expose a
                                # standard abstract class.
                                for h in s.find_all(["h2", "h3", "h4", "strong", "b", "span"]):
                                    heading = h.get_text(" ", strip=True).casefold()
                                    if heading in {"tóm tắt", "abstract"}:
                                        p = h.find_next_sibling() or h.find_parent("div") or h.find_parent("section")
                                        if p:
                                            abstract = p.get_text(separator=" ").strip()
                                            break

                            abstract_clean = re.sub(
                                r"^(Tóm tắt|Abstract|TÓM TẮT)[\s:\.\-]*", "",
                                abstract, flags=re.IGNORECASE
                            ).strip()
                            safe_title = clean_filename(title)
                            if len(safe_title) > 50:
                                safe_title = safe_title[:47] + "..."

                            # Metadata is audit evidence only. A bilingual OJS page can
                            # expose an English abstract while its public PDF is Vietnamese,
                            # so final admission always waits for PDF evidence.
                            metadata_assessment = assess_metadata(
                                title, abstract_clean, _html_language(s), settings
                            )

                            pdf_url = _find_pdf_url(s, base_url)
                            if not pdf_url:
                                no_pdf = AdmissionDecision(
                                    "REJECTED_NO_PDF", "PDF_NOT_FOUND", metadata_assessment, None
                                )
                                language_audit.record(
                                    source_url=au, title=title, pdf_url=None, decision=no_pdf
                                )
                                _record_rejection(no_pdf.status)
                                _log(f"    [LOẠI - KHÔNG CÓ PDF] {title[:70]}")
                                continue

                            candidate_path = _candidate_pdf_path(
                                site_folder, target_year, safe_title, au, settings
                            )
                            download_error = _download_candidate(session, pdf_url, candidate_path)
                            if download_error:
                                no_pdf = AdmissionDecision(
                                    "REJECTED_NO_PDF", download_error, metadata_assessment, None
                                )
                                if candidate_path.exists():
                                    quarantine_path = quarantine_pdf(
                                        candidate_path, no_pdf.status, (site_folder, target_year), settings
                                    )
                                    scrape_status["quarantined"] += 1
                                    stored_path = str(quarantine_path)
                                else:
                                    stored_path = None
                                language_audit.record(
                                    source_url=au, title=title, pdf_url=pdf_url, decision=no_pdf,
                                    file_path=stored_path,
                                )
                                _record_rejection(no_pdf.status)
                                _log(f"    [LOẠI - PDF KHÔNG HỢP LỆ] {download_error}: {title[:70]}")
                                continue

                            extracted = extract_from_pdf_path(str(candidate_path))
                            pdf_text = select_pdf_text_for_language(
                                extracted.get("body"), extracted.get("full_text")
                            )
                            decision = decide_admission(metadata_assessment, pdf_text, settings)
                            if extracted.get("error"):
                                decision = AdmissionDecision(
                                    "REJECTED_NO_TEXT", "PDF_EXTRACTION_FAILED", metadata_assessment, None
                                )

                            if not decision.accepted:
                                quarantine_path = quarantine_pdf(
                                    candidate_path, decision.status, (site_folder, target_year), settings
                                )
                                scrape_status["quarantined"] += 1
                                language_audit.record(
                                    source_url=au, title=title, pdf_url=pdf_url, decision=decision,
                                    file_path=str(quarantine_path),
                                )
                                _record_rejection(decision.status)
                                _log(f"    [LOẠI - {decision.reason}] {title[:70]}")
                                continue

                            # Only now is the record admitted to MySQL and the final corpus.
                            if not abstract_clean:
                                abstract_clean = (extracted.get("abstract") or "").strip()
                            final_pdf_path = _accept_candidate(candidate_path)
                            cursor.execute(
                                "INSERT INTO articles "
                                "(title,authors,abstract,publication_year,source_url) "
                                "VALUES(%s,%s,%s,%s,%s)",
                                (title, authors, abstract_clean, yr, au),
                            )
                            conn.commit()
                            aid = cursor.lastrowid
                            language_audit.record(
                                source_url=au, title=title, pdf_url=pdf_url, decision=decision,
                                file_path=str(final_pdf_path), article_id=aid,
                            )

                            issue_dir = os.path.join(output_folder, site_folder, "Vietnamese", target_year, issue_name)
                            os.makedirs(issue_dir, exist_ok=True)
                            fp = os.path.join(
                                issue_dir,
                                f"{datetime.now().strftime('%d%m%Y')}_{aid:04d}_{safe_title}.txt",
                            )
                            with open(fp, "w", encoding="utf-8-sig") as fout:
                                fout.write(
                                    f"TIÊU ĐỀ: {title}\n"
                                    f"TÁC GIẢ: {authors}\n" + "-" * 40 +
                                    f"\nTÓM TẮT:\n{abstract_clean}\n"
                                )
                            scrape_status["success"] += 1
                            _log(f"    💾 Đã nhận vào corpus tiếng Việt: {title[:70]}")
                            total_proc = scrape_status["success"] + scrape_status["duplicates"] + scrape_status["skipped"]
                            _log(f"    Tiến độ: {total_proc} bài (✅{scrape_status['success']} | 🔁{scrape_status['duplicates']} | ⏭{scrape_status['skipped']})")

                        except Exception as e:
                            scrape_status["skipped"] += 1
                            _log(f"    ❌ Lỗi bài báo {au}: {e}")

                except Exception as e:
                    _log(f"  ❌ Lỗi số tạp chí {iu}: {e}")

    except ValueError as e:
        # FIX #1: URL không hợp lệ — hiển thị lỗi rõ ràng
        scrape_status["error"] = str(e)
        _log(f"❌ {e}")
    except Exception as e:
        scrape_status["error"] = str(e)
        _log(f"❌ LỖI NGHIÊM TRỌNG: {e}")
    finally:
        scrape_status["summary"] = {
            k: scrape_status[k] for k in (
                "success", "duplicates", "skipped", "rejected_english",
                "rejected_mixed", "rejected_no_text", "quarantined",
            )
        }
        total_processed = sum(scrape_status[k] for k in ("success", "duplicates", "skipped"))
        scrape_status["summary"]["total_processed"] = total_processed

        # Lưu kết quả vào DB
        if _log_id:
            try:
                log_conn = mysql.connector.connect(**db_config)
                log_cursor = log_conn.cursor()
                final_status = 'paused' if _stop_requested else ('error' if scrape_status["error"] else 'completed')
                log_cursor.execute(
                    "UPDATE crawl_progress SET success_count=%s, duplicate_count=%s, skipped_count=%s, status=%s WHERE id=%s",
                    (scrape_status["success"], scrape_status["duplicates"], scrape_status["skipped"], final_status, _log_id)
                )
                if final_status != 'paused':
                    log_cursor.execute(
                        "INSERT INTO crawl_logs (crawl_date, target_url, total_urls, success_count, duplicate_count, error_count, status, start_year, end_year) "
                        "VALUES (CURDATE(), %s, %s, %s, %s, %s, %s, %s, %s)",
                        (request.target_url, total_processed, scrape_status["success"], scrape_status["duplicates"], scrape_status["skipped"], final_status, request.start_year, request.end_year)
                    )
                log_conn.commit()
                log_cursor.close()
                log_conn.close()
            except Exception as e_log:
                _log(f"Lỗi khi lưu log vào DB: {e_log}")

        if cursor: cursor.close()
        if conn:   conn.close()

        scrape_status["running"] = False
        scrape_status["done"]    = True
        _log(f"\n=== HOÀN THÀNH === ✅ {scrape_status['summary']['success']} lưu | 🔁 {scrape_status['summary']['duplicates']} trùng | ⏭ {scrape_status['summary']['skipped']} bỏ qua")
