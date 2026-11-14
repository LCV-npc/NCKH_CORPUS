from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.ner_dict import DICT_DIR
from core.ner_engine import run_ner
from scripts.build_ner_corpus import build_corpus


class ControlledVocabularyCorpusTests(unittest.TestCase):
    def test_corpus_source_filter_runs_icd10_before_conflict_resolution(self):
        yhct = json.loads((DICT_DIR / "yhct_v1.json").read_text(encoding="utf-8"))
        term = next(
            entry["canonical_term"]
            for entry in yhct["entries"]
            if entry.get("record_type") == "base" and entry.get("active_for_ner")
        )

        _, _, entities, _ = run_ner(
            term,
            include_suggestions=False,
            allowed_sources={"ICD10"},
        )

        self.assertTrue(entities)
        self.assertTrue(all(entity["concept_id_source"] == "ICD10" for entity in entities))

    def test_controlled_vocabulary_is_icd10_anchored_and_traceable(self):
        path = DICT_DIR / "controlled_vocabulary_v1.json"
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["source"]["dictionaries"], ["icd10_v1.json"])
        self.assertTrue(payload["entries"])
        for entry in payload["entries"]:
            with self.subTest(surface_form=entry["surface_form"]):
                self.assertTrue(entry["surface_form"])
                self.assertTrue(entry["canonical_term"])
                self.assertTrue(entry["concept_id"].startswith("ICD10:"))
                self.assertEqual(entry["concept_id_source"], "ICD10")
                self.assertTrue(entry["rule_id"])
                self.assertEqual(entry["score_type"], "heuristic")
                self.assertFalse(entry["score_calibrated"])
                self.assertGreaterEqual(len(entry["normalized_form"].split()), 2)
                self.assertFalse(entry["normalized_form"].startswith("các "))
                self.assertFalse(entry["normalized_form"].startswith("những "))

    def test_build_corpus_keeps_surface_canonical_concept_and_bio_outputs(self):
        rows = [
            {
                "article_id": 1,
                "title": "Bài kiểm thử",
                "abstract": "Bệnh lý tăng huyết áp và tăng huyết áp.",
            }
        ]

        with tempfile.TemporaryDirectory() as directory:
            summary = build_corpus(rows, Path(directory))
            preannotation = Path(directory) / "controlled_vocabulary_preannotation.jsonl"
            records = [
                json.loads(line)
                for line in preannotation.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(summary["annotation_status"], "PROVISIONAL")
            self.assertEqual(summary["splits"]["dev"]["articles"], 1)
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["source"], "articles.abstract")
            self.assertEqual(record["annotation_status"], "PROVISIONAL")
            self.assertEqual(record["dictionary_version"], "2026.11.14-cv2")
            self.assertTrue(record["manifest_sha256"])
            self.assertEqual(len(record["entities"]), 2)
            self.assertEqual(
                {entity["concept_id"] for entity in record["entities"]},
                {"ICD10:I10:base"},
            )
            self.assertEqual(
                [entity["text"] for entity in record["entities"]],
                ["Bệnh lý tăng huyết áp", "tăng huyết áp"],
            )
            for entity in record["entities"]:
                self.assertEqual(record["text"][entity["start"]:entity["end"]], entity["text"])
                self.assertEqual(entity["surface_form"], entity["text"])
                self.assertTrue(entity["canonical_term"])
                self.assertEqual(entity["label"], "DISEASE")

            for filename in ("train.jsonl", "dev.jsonl", "test.jsonl", "dev.conll", "corpus_manifest.json"):
                self.assertTrue((Path(directory) / filename).is_file(), filename)


if __name__ == "__main__":
    unittest.main()
