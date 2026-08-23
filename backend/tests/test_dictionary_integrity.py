"""Regression checks for the validated dictionary-backed NER pipeline."""

import hashlib
import json
import unicodedata
import unittest

from core.ner_dict import DATA_DIR, DICT_DIR, dictionary_metadata
from core.ner_engine import run_ner
from core.ai_label import _find_spans, _merge_ai_and_dictionary


class DictionaryIntegrityTests(unittest.TestCase):
    def _payload(self, filename):
        return json.loads((DICT_DIR / filename).read_text(encoding="utf-8"))

    def test_backend_snapshots_match_original_files_in_data(self):
        checks = (
            ("icd10_v1.json", "phu-lu-c-1-danh-mu-c-icd-10-thay-the-dmdc-phie-n-ba-n-6.xlsx", "source_rows"),
            ("yhct_v1.json", "PhuLuc1.pdf", "all"),
        )
        for artifact_name, source_name, count_key in checks:
            with self.subTest(artifact=artifact_name):
                payload = self._payload(artifact_name)
                source_path = DATA_DIR / source_name
                self.assertTrue(source_path.is_file())
                self.assertEqual(payload["source"]["file"], source_name)
                self.assertEqual(
                    hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    payload["source"]["sha256"],
                )
                self.assertEqual(len(payload["entries"]), payload["counts"][count_key])

    def test_real_terms_from_icd_and_yhct_are_found_with_exact_source(self):
        checks = (
            ("icd10_v1.json", "icd10", None),
            ("yhct_v1.json", "yhct", "base"),
        )
        for artifact_name, expected_source, record_type in checks:
            with self.subTest(artifact=artifact_name):
                entry = next(
                    item
                    for item in self._payload(artifact_name)["entries"]
                    if item.get("active_for_ner")
                    and not item.get("ambiguous")
                    and (record_type is None or item.get("record_type") == record_type)
                )
                term = entry["canonical_term"]
                _, _, entities, _ = run_ner(
                    term,
                    enable_tone_restore=False,
                    enable_noun_phrase=False,
                )
                self.assertTrue(
                    any(
                        item["text"] == term
                        and item["icd_code"] == entry["code"]
                        and item["source"] == expected_source
                        for item in entities
                    ),
                    f"Không tìm được đúng mục từ điển: {term}",
                )

    def test_runtime_metadata_lists_validated_sources_and_coverage(self):
        self.assertEqual(dictionary_metadata["dictionary_version"], "2026.08.09-v1")
        self.assertGreater(dictionary_metadata["loaded_terms"], 12_000)
        self.assertEqual(
            set(dictionary_metadata["categories"]),
            {"Bệnh Lý", "Triệu Chứng", "Đông Y / YHCT"},
        )
        self.assertEqual(
            set(dictionary_metadata["source_files"]),
            {"icd10", "yhct", "custom_aliases"},
        )

    def test_decomposed_vietnamese_words_do_not_create_short_symptoms(self):
        # PDF text may use NFD: "học" becomes h + o + combining marks + c.
        # It must remain one word, not fabricate the symptom "ho" (R05).
        text = unicodedata.normalize("NFD", "Khoa Y học Hà Nội")
        _, _, entities, _ = run_ner(text, enable_tone_restore=False, enable_noun_phrase=False)
        self.assertFalse(any(item["icd_code"] == "R05" for item in entities))

    def test_diacritic_near_matches_do_not_become_a_different_term(self):
        # "u mô" is not the ICD D17 term "u mỡ".  Accentless fallback may
        # only be applied to genuinely unaccented source text.
        _, _, entities, _ = run_ner("u mô", enable_tone_restore=False, enable_noun_phrase=False)
        self.assertFalse(any(item["icd_code"] == "D17" for item in entities))

    def test_complete_prostate_carcinoma_phrase_wins_over_fragments(self):
        text = "Ung thư biểu mô tuyến tiền liệt"
        _, _, entities, _ = run_ner(text, enable_tone_restore=False, enable_noun_phrase=False)
        self.assertEqual(
            [(item["text"], item["icd_code"]) for item in entities],
            [(text, "C61")],
        )

    def test_short_symptom_is_only_matched_as_a_complete_token(self):
        # "ho" remains a valid symptom when it is genuinely a whole word.
        _, _, entities, _ = run_ner("Bệnh nhân ho kéo dài.")
        self.assertTrue(any(item["text"] == "ho" and item["icd_code"] == "R05" for item in entities))

    def test_ai_span_search_also_respects_combining_mark_word_boundaries(self):
        text = unicodedata.normalize("NFD", "Khoa Y học Hà Nội")
        self.assertEqual(_find_spans(text, "ho"), [])

    def test_ai_fragment_inside_validated_longer_term_is_discarded(self):
        text = "Ung thư biểu mô tuyến tiền liệt"
        result = _merge_ai_and_dictionary(
            text,
            {"Bệnh lý": ["u mô"]},
            [{
                "term": text,
                "start": 0,
                "end": len(text),
                "code": "C61",
                "label_vn": "U ác của tuyến tiền liệt",
                "dictionary_type": "Bệnh Lý",
                "matched_by": "alias",
            }],
        )
        self.assertEqual([item["term"] for item in result["Bệnh lý"]], [text])
