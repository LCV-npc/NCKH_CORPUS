import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bs4 import BeautifulSoup

from config.language_filter import VietnameseCorpusSettings
from core.language_audit import quarantine_pdf
from core.scraper import (
    _accept_candidate,
    _archive_issue_links,
    _archive_next_links,
    _archive_url_from_input,
    _article_view_links,
    _candidate_pdf_path,
    _discover_archive_issues,
    _download_candidate,
    _find_archive_url,
    _journal_base_url,
    _log,
    scrape_status,
)


class OjsDiscoveryTests(unittest.TestCase):
    def test_repeated_log_message_is_kept_once(self):
        previous = scrape_status["log_messages"]
        try:
            scrape_status["log_messages"] = []
            _log("Đang cào: https://example.test/issue/1")
            _log("Đang cào: https://example.test/issue/1")
            self.assertEqual(["Đang cào: https://example.test/issue/1"], scrape_status["log_messages"])
        finally:
            scrape_status["log_messages"] = previous

    def test_candidate_path_is_scoped_by_source_domain_and_year(self):
        with TemporaryDirectory() as temporary_dir:
            settings = VietnameseCorpusSettings(candidates_dir=Path(temporary_dir))
            path = _candidate_pdf_path(
                "tapchiyhcd.vn", "2025", "Bài báo", "https://tapchiyhcd.vn/article/1", settings
            )
            self.assertEqual(Path(temporary_dir) / "tapchiyhcd.vn" / "2025", path.parent)
            self.assertTrue(path.name.startswith("Bài báo_"))

    def test_accepted_pdf_remains_at_its_candidate_path(self):
        with TemporaryDirectory() as temporary_dir:
            candidate = Path(temporary_dir) / "candidates" / "tapchiyhcd.vn" / "2025" / "article.pdf"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"%PDF-test")
            self.assertEqual(candidate, _accept_candidate(candidate))
            self.assertTrue(candidate.is_file())

    def test_downloaded_english_pdf_can_be_moved_from_candidates_to_quarantine(self):
        class PdfResponse:
            status_code = 200
            headers = {"Content-Type": "application/pdf"}

            @staticmethod
            def iter_content(chunk_size=8192):
                del chunk_size
                yield b"%PDF-English article"

        class Session:
            @staticmethod
            def get(*_args, **_kwargs):
                return PdfResponse()

        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            settings = VietnameseCorpusSettings(
                candidates_dir=root / "candidates",
                quarantine_dir=root / "quarantine",
            )
            candidate = _candidate_pdf_path(
                "example.test", "2025", "English article", "https://example.test/article/1", settings
            )

            self.assertIsNone(_download_candidate(Session(), "https://example.test/article/1.pdf", candidate))
            quarantined = quarantine_pdf(
                candidate, "REJECTED_ENGLISH", ("example.test", "2025"), settings
            )

            self.assertFalse(candidate.exists())
            self.assertTrue(quarantined.is_file())
            self.assertEqual(root / "quarantine" / "english" / "example.test" / "2025", quarantined.parent)

    def test_archive_extracts_year_from_heading_and_follows_next_link(self):
        soup = BeautifulSoup(
            """
            <h2>2023</h2>
            <ul><li><a href="/index.php/vmj/issue/view/71">Vol. 500 No. 1</a></li></ul>
            <nav><a class="next" href="/index.php/vmj/issue/archive/2">Next →</a></nav>
            """,
            "html.parser",
        )
        base = "https://tapchiyhocvietnam.vn/index.php/vmj/issue/archive"
        issues = _archive_issue_links(soup, base)
        self.assertEqual("2023", issues[0]["year"])
        self.assertEqual("https://tapchiyhocvietnam.vn/index.php/vmj/issue/view/71", issues[0]["url"])
        self.assertEqual(
            ["https://tapchiyhocvietnam.vn/index.php/vmj/issue/archive/2"],
            _archive_next_links(soup, base),
        )

    def test_archive_extracts_year_from_issue_label(self):
        soup = BeautifulSoup(
            '<a href="/index.php/tcncyh/issue/view/165">Tập 165 Số 4 (2023)</a>',
            "html.parser",
        )
        issues = _archive_issue_links(soup, "https://tapchinghiencuuyhoc.vn/index.php/tcncyh/issue/archive")
        self.assertEqual("2023", issues[0]["year"])

    def test_archive_applies_plain_div_year_to_all_following_issue_cards(self):
        soup = BeautifulSoup(
            """
            <div class="issues media-list">
              <div style="width: 100%; font-size: 20px">2025</div>
              <div class="issue-summary media">
                <div class="media-body">
                  <a class="title" href="/index.php/yhcd/issue/view/126">Issue 6</a>
                </div>
              </div>
              <div class="issue-summary media">
                <div class="media-body">
                  <a class="title" href="/index.php/yhcd/issue/view/125">Special issue 23</a>
                </div>
              </div>
            </div>
            """,
            "html.parser",
        )

        issues = _archive_issue_links(soup, "https://tapchiyhcd.vn/index.php/yhcd/issue/archive")

        self.assertEqual(2, len(issues))
        self.assertEqual(["2025", "2025"], [issue["year"] for issue in issues])

    def test_direct_archive_page_is_normalized_to_first_page(self):
        url = "https://tapchiyhcd.vn/index.php/yhcd/issue/archive/2?unused=1#items"
        self.assertEqual(
            "https://tapchiyhcd.vn/index.php/yhcd/issue/archive",
            _archive_url_from_input(url),
        )
        self.assertEqual(
            "https://tapchiyhcd.vn/index.php/yhcd",
            _journal_base_url(url),
        )

    def test_homepage_archive_is_found_from_href_without_visible_text(self):
        soup = BeautifulSoup(
            '<a aria-label="stored issues" href="/index.php/journal/issue/archive"><svg></svg></a>',
            "html.parser",
        )
        self.assertEqual(
            "https://example.test/index.php/journal/issue/archive",
            _find_archive_url(soup, "https://example.test/index.php/journal"),
        )

    def test_archive_follows_numbered_and_query_pagination_links(self):
        soup = BeautifulSoup(
            """
            <a href="/index.php/journal/issue/archive/2">2</a>
            <a href="/index.php/journal/issue/archive?issuesPage=3">3</a>
            <a href="/index.php/journal/issue/archive">Archive root</a>
            <a href="https://other.test/index.php/journal/issue/archive/4">4</a>
            """,
            "html.parser",
        )
        self.assertEqual(
            [
                "https://example.test/index.php/journal/issue/archive/2",
                "https://example.test/index.php/journal/issue/archive?issuesPage=3",
            ],
            _archive_next_links(soup, "https://example.test/index.php/journal/issue/archive"),
        )

    def test_archive_pages_are_traversed_once_for_a_year_range(self):
        first_url = "https://example.test/index.php/journal/issue/archive"
        second_url = f"{first_url}/2"
        pages = {
            first_url: """
                <h2>2025</h2>
                <a href="/index.php/journal/issue/view/25">Issue 2025</a>
                <a class="next" href="/index.php/journal/issue/archive/2">Next</a>
            """,
            second_url: """
                <div>2024</div>
                <a href="/index.php/journal/issue/view/24">Issue 2024</a>
            """,
        }

        class Response:
            def __init__(self, url, text):
                self.url = url
                self.text = text
                self.status_code = 200

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **_kwargs):
                self.calls.append(url)
                return Response(url, pages[url])

        session = Session()
        previous_pages = scrape_status["pages_processed"]
        previous_logs = scrape_status["log_messages"]
        try:
            scrape_status["pages_processed"] = 0
            scrape_status["log_messages"] = []
            issues = _discover_archive_issues(session, first_url)
        finally:
            scrape_status["pages_processed"] = previous_pages
            scrape_status["log_messages"] = previous_logs

        self.assertEqual([first_url, second_url], session.calls)
        self.assertEqual({"2024", "2025"}, {issue["year"] for issue in issues})
        self.assertEqual(2, len(issues))

    def test_article_discovery_keeps_only_canonical_article_pages(self):
        soup = BeautifulSoup(
            """
            <a href="/index.php/vmj/article/view/18452">Article</a>
            <a href="/index.php/vmj/article/view/18452/15603">PDF</a>
            <a href="https://other.example/article/view/2">Other site</a>
            """,
            "html.parser",
        )
        links = _article_view_links(soup, "https://tapchiyhocvietnam.vn/index.php/vmj/issue/view/1")
        self.assertEqual(["https://tapchiyhocvietnam.vn/index.php/vmj/article/view/18452"], links)


if __name__ == "__main__":
    unittest.main()
