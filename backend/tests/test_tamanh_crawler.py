import json
from pathlib import Path
import tempfile
import unittest

from core.tamanh_crawler import (
    TamanhCategory,
    TamanhFileExporter,
    TamanhHtmlParser,
    TamanhQaRecord,
    slugify,
)


BASE_URL = "https://tamanhhospital.vn/"
CATEGORY_URL = "https://tamanhhospital.vn/tu-van/chan-thuong-chinh-hinh/"


class TamanhHtmlParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = TamanhHtmlParser()

    def test_discovers_tu_van_from_home_links(self):
        html = '''<a href="/tin-tuc/">Tin tức</a>
        <a href="https://tamanhhospital.vn/tu-van/">BÁC SĨ TƯ VẤN</a>'''
        self.assertEqual(self.parser.discover_tu_van_url(html, BASE_URL), "https://tamanhhospital.vn/tu-van/")

    def test_discovers_categories_without_hardcoded_list(self):
        html = '''
        <a class="title_catetuvan" href="/tu-van/chan-thuong-chinh-hinh/">Chấn thương chỉnh hình</a>
        <a class="title_catetuvan" href="/tu-van/tim-mach/">Tim mạch</a>
        <a href="/tu-van/cau-hoi-benh-nhan/">Một câu hỏi</a>
        <a class="title_catetuvan" href="https://example.org/tu-van/mat/">Mắt</a>
        '''
        categories = self.parser.discover_categories(html, "https://tamanhhospital.vn/tu-van/")
        self.assertEqual(
            [(item.name, item.url) for item in categories],
            [
                ("Chấn thương chỉnh hình", CATEGORY_URL),
                ("Tim mạch", "https://tamanhhospital.vn/tu-van/tim-mach/"),
            ],
        )

    def test_pagination_and_detail_links_are_deduplicated_by_shape(self):
        html = '''
        <a href="/tu-van/chan-thuong-chinh-hinh/page/2/">2</a>
        <a href="/tu-van/chan-thuong-chinh-hinh/page/2/">next</a>
        <a href="/tu-van/bao-lau-sau-thay-khop-moi-choi-the-thao-duoc/">Xem thêm</a>
        <a href="/tu-van/chan-thuong-chinh-hinh/">Chấn thương chỉnh hình</a>
        '''
        category_urls = {CATEGORY_URL}
        self.assertEqual(
            self.parser.pagination_links(html, CATEGORY_URL, CATEGORY_URL),
            {"https://tamanhhospital.vn/tu-van/chan-thuong-chinh-hinh/page/2/"},
        )
        self.assertEqual(
            self.parser.detail_links(html, CATEGORY_URL, category_urls),
            {"https://tamanhhospital.vn/tu-van/bao-lau-sau-thay-khop-moi-choi-the-thao-duoc/"},
        )

    def test_parses_only_patient_question_and_doctor_answer(self):
        html = '''
        <section class="box_detail">
          <div class="title"><h1>Bao lâu sau thay khớp mới chơi thể thao được?</h1></div>
          <div class="box_tuvan"><div class="cl_brand">Nguyễn văn lợi</div>
            <div class="mt_10">Thay khớp gối nhân tạo sau khi phục hồi có chạy bộ được không?</div></div>
          <div class="tuvan_detail">
            <div class="row"><a class="font_helB"><div>THS.BS.CKII TRẦN ANH VŨ</div></a></div>
            <p>Chào bạn,</p><p>Thông thường người bệnh cần phục hồi và được bác sĩ đánh giá trước khi chơi thể thao.</p>
            <p>Nếu cần, liên hệ hotline 028 7102 6789 để được hỗ trợ.</p>
            <p>Cảm ơn bạn đã gửi câu hỏi đến Tuần tư vấn.</p>
          </div>
        </section><meta property="article:published_time" content="2024-02-01T00:00:00+07:00">'''
        record = self.parser.parse_qa(
            html,
            "https://tamanhhospital.vn/tu-van/mau/",
            TamanhCategory("Chấn thương chỉnh hình", CATEGORY_URL),
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.patient_question, "Thay khớp gối nhân tạo sau khi phục hồi có chạy bộ được không?")
        self.assertEqual(record.doctor_name, "THS.BS.CKII TRẦN ANH VŨ")
        self.assertEqual(
            record.doctor_answer,
            "Chào bạn,\n\nThông thường người bệnh cần phục hồi và được bác sĩ đánh giá trước khi chơi thể thao.",
        )
        self.assertEqual(record.published_year, 2024)
        self.assertNotIn("Nguyễn văn lợi", record.patient_question)
        self.assertNotIn("hotline", record.doctor_answer.casefold())


class TamanhFileExporterTests(unittest.TestCase):
    def _record(self):
        return TamanhQaRecord(
            category="Chấn thương chỉnh hình",
            question_title="Tiêu đề",
            patient_question="Câu hỏi bệnh nhân",
            doctor_name="BS A",
            doctor_answer="Câu trả lời bác sĩ",
            source_url="https://tamanhhospital.vn/tu-van/mau/",
            category_url=CATEGORY_URL,
        )

    def test_filename_and_duplicate_fingerprint(self):
        self.assertEqual(slugify("Chấn thương chỉnh hình"), "chan_thuong_chinh_hinh")
        with tempfile.TemporaryDirectory() as directory:
            exporter = TamanhFileExporter(Path(directory))
            record = self._record()
            metadata = exporter.export(record)
            self.assertEqual(metadata["questionFile"], "tu_van/chan_thuong_chinh_hinh/chan_thuong_chinh_hinh_000001_BN.txt")
            self.assertEqual(metadata["answerFile"], "tu_van/chan_thuong_chinh_hinh/chan_thuong_chinh_hinh_000001_BS.txt")
            self.assertTrue(exporter.is_duplicate(record))
            question_path = Path(directory) / metadata["questionFile"]
            answer_path = Path(directory) / metadata["answerFile"]
            self.assertEqual(question_path.read_text(encoding="utf-8"), "Câu hỏi bệnh nhân")
            self.assertEqual(answer_path.read_text(encoding="utf-8"), "Bác sĩ: BS A\n\nCâu trả lời bác sĩ")
            saved = json.loads((Path(directory) / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved), 1)

    def test_persists_metadata_to_repository(self):
        class FakeRepository:
            def __init__(self):
                self.saved = []

            def fingerprints(self):
                return set()

            def upsert(self, metadata):
                self.saved.append(metadata)

        with tempfile.TemporaryDirectory() as directory:
            repository = FakeRepository()
            exporter = TamanhFileExporter(Path(directory), repository)
            metadata = exporter.export(self._record())
            self.assertEqual(repository.saved, [metadata])


if __name__ == "__main__":
    unittest.main()
