import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bs4 import BeautifulSoup

from config.language_filter import VietnameseCorpusSettings
from core.scraper import _accept_candidate, _archive_issue_links, _archive_next_links, _article_view_links, _candidate_pdf_path, _log, scrape_status


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
