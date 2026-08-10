import re
import time
import random
import os
import threading
import requests
import mysql.connector
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from urllib.parse import urlparse, urljoin
from core.lang_detector import detect_language

# Trạng thái scraping toàn cục — được đọc bởi /api/status
scrape_status: dict = {
    "running": False, "success": 0, "skipped": 0, "duplicates": 0,
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
    """Ghi log vào scrape_status để frontend đọc realtime."""
    scrape_status["log_messages"].append(msg)
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

        _log("Đang kết nối tới DB...")
        conn   = mysql.connector.connect(**db_config)
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

            # Thu thập link số/tập
            for page in range(1, 10):
                pu = f"{archive_url}/{page}" if page > 1 else archive_url
                _log(f"  Đang fetch danh sách trang {page}: {pu}")
                try:
                    r = session.get(pu, verify=False, timeout=60)
                    if r.status_code != 200:
                        _log(f"  ⚠️ Trang {page} trả về HTTP {r.status_code}, dừng phân trang.")
                        break
                    soup    = BeautifulSoup(r.text, "html.parser")
                    cur_blk = None
                    found_on_page = 0
                    for tag in soup.find_all(["div", "h2", "h3", "a"]):
                        t = tag.get_text(strip=True)
                        if tag.name in ("div", "h2", "h3") and re.fullmatch(r"20\d{2}", t):
                            cur_blk = t

                        if _stop_requested: break

                        if tag.name == "a" and "/issue/view/" in tag.get("href", ""):
                            # FIX #3: dùng _make_absolute thay vì ghép thủ công
                            lnk = _make_absolute(tag["href"], base_url)
                            if target_year in t or cur_blk == target_year:
                                if not any(il["url"] == lnk for il in issue_links):
                                    issue_links.append({"url": lnk, "name": clean_filename(t)})
                                    found_on_page += 1

                    _log(f"  → Trang {page}: tìm thấy {found_on_page} số mới của năm {target_year}")
                    # Nếu không tìm thấy gì trong trang này → dừng phân trang
                    if found_on_page == 0 and page > 1:
                        break

                except requests.exceptions.ConnectionError as ex:
                    _log(f"  ❌ Lỗi kết nối khi fetch trang {pu}: {ex}")
                    break
                except Exception as ex:
                    _log(f"  ❌ Lỗi khi fetch page {pu}: {ex}")
                    break

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

                    # FIX #4: Chuẩn hóa URL bài báo về dạng tuyệt đối
                    art_links = []
                    for a in soup_i.find_all("a", href=True):
                        lnk = _make_absolute(a["href"], base_url)
                        if (
                            "/article/view/" in lnk
                            and re.search(r"/article/view/\d+$", lnk)
                            and lnk not in art_links
                        ):
                            art_links.append(lnk)

                    _log(f"  📄 Tìm thấy {len(art_links)} bài báo trong số tạp chí này.")

                    for au in art_links:
                        if _stop_requested: break
                        cursor.execute("SELECT id FROM articles WHERE source_url=%s", (au,))
                        if cursor.fetchone():
                            scrape_status["duplicates"] += 1
                            _log(f"    ⏩ [TRÙNG] {au.split('/')[-1]}")
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
                                # Fallback: tìm theo heading "tóm tắt"
                                for h in s.find_all(["h2", "h3", "h4", "strong", "b", "span"]):
                                    if h.text and "tóm tắt" in h.text.lower():
                                        p = h.find_next_sibling() or h.find_parent("div") or h.find_parent("section")
                                        if p:
                                            abstract = p.get_text(separator=" ").strip()
                                            break

                            if not abstract or len(abstract) < 50:
                                scrape_status["skipped"] += 1
                                _log(f"    [BỎ QUA - KHÔNG TÓM TẮT] {title[:80]}")
                                continue

                            abstract_clean = re.sub(
                                r"^(Tóm tắt|Abstract|TÓM TẮT)[\s:\.\-]*", "",
                                abstract, flags=re.IGNORECASE
                            ).strip()

                            cursor.execute(
                                "INSERT INTO articles "
                                "(title,authors,abstract,publication_year,source_url) "
                                "VALUES(%s,%s,%s,%s,%s)",
                                (title, authors, abstract_clean, yr, au),
                            )
                            conn.commit()
                            aid = cursor.lastrowid

                            safe_title = clean_filename(title)
                            if len(safe_title) > 50:
                                safe_title = safe_title[:47] + "..."

                            # ============ TÌM VÀ TẢI PDF ============
                            pdf_url = None
                            meta_pdf = s.find("meta", attrs={"name": "citation_pdf_url"})
                            if meta_pdf:
                                pdf_url = meta_pdf.get("content")
                            else:
                                for a_tag in s.find_all("a", href=True):
                                    text_lower = a_tag.get_text(strip=True).lower()
                                    classes = " ".join(a_tag.get("class", []))
                                    if "pdf" in text_lower or "pdf" in classes.lower():
                                        href_val = a_tag["href"]
                                        if "/article/view/" in href_val or "/article/download/" in href_val:
                                            pdf_url = _make_absolute(href_val, base_url)
                                            break

                            if pdf_url and "/article/view/" in pdf_url:
                                pdf_url = pdf_url.replace("/article/view/", "/article/download/")

                            if pdf_url:
                                try:
                                    pdf_r = session.get(pdf_url, verify=False, timeout=120, stream=True)
                                    if pdf_r.status_code == 200:
                                        content_type = pdf_r.headers.get("Content-Type", "")
                                        if "pdf" in content_type or "octet-stream" in content_type:
                                            pdf_target_dir = os.path.join(
                                                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                "Văn_Bản_Y_Tế_PDF",
                                                site_folder,
                                                target_year,
                                            )
                                            os.makedirs(pdf_target_dir, exist_ok=True)
                                            pdf_path = os.path.join(pdf_target_dir, f"{safe_title}.pdf")
                                            with open(pdf_path, "wb") as pdf_out:
                                                for chunk in pdf_r.iter_content(chunk_size=8192):
                                                    pdf_out.write(chunk)
                                            _log(f"    ✅ Đã tải PDF: {safe_title}.pdf")
                                        else:
                                            _log(f"    [PDF SAI ĐỊNH DẠNG] Content-Type: {content_type}")
                                    else:
                                        _log(f"    [LỖI TẢI PDF] HTTP {pdf_r.status_code}: {pdf_url}")
                                except Exception as e_pdf:
                                    _log(f"    [LỖI TẢI PDF] {e_pdf}")
                            else:
                                _log(f"    [KHÔNG TÌM THẤY LINK PDF] {safe_title}")
                            # ========================================

                            # Xác định ngôn ngữ và lưu file txt
                            lang = detect_language(abstract_clean)
                            issue_dir = os.path.join(output_folder, site_folder, lang, target_year, issue_name)
                            os.makedirs(issue_dir, exist_ok=True)

                            fp = os.path.join(
                                issue_dir,
                                f"{datetime.now().strftime('%d%m%Y')}_{aid:04d}_{safe_title}.txt",
                            )
                            with open(fp, "w", encoding="utf-8-sig") as fout:
                                fout.write(
                                    f"TIÊU ĐỀ: {title}\n"
                                    f"TÁC GIẢ: {authors}\n"
                                    + "-" * 40 +
                                    f"\nTÓM TẮT:\n{abstract_clean}\n"
                                )
                            scrape_status["success"] += 1
                            _log(f"    💾 Đã lưu: {title[:70]}")
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
            k: scrape_status[k] for k in ("success", "duplicates", "skipped")
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
