from __future__ import annotations

import unicodedata
import unittest
import json
import tempfile
from pathlib import Path

from core.ner_engine import NEREngine, run_ner
from core.ner_evaluation import (
    bio_to_spans,
    calculate_accuracy,
    evaluate_system,
    evaluate_run,
    spans_to_bio,
)


class DictionaryMatchingImprovementTests(unittest.TestCase):
    def test_weighted_overlap_prefers_two_exact_spans_over_one_lower_quality_span(self):
        candidates = [
            {
                "start": 0,
                "end": 30,
                "token_count": 5,
                "matched_by": "grammar_variant",
                "concept_id": "variant",
                "icd_code": "V",
            },
            {
                "start": 0,
                "end": 12,
                "token_count": 2,
                "matched_by": "exact",
                "concept_id": "exact-a",
                "icd_code": "A",
            },
            {
                "start": 14,
                "end": 30,
                "token_count": 3,
                "matched_by": "exact",
                "concept_id": "exact-b",
                "icd_code": "B",
            },
        ]

        result = NEREngine._resolve_overlaps(candidates)

        self.assertEqual(
            [(item["start"], item["end"], item["matched_by"]) for item in result],
            [(0, 12, "exact"), (14, 30, "exact")],
        )

    def test_weighted_overlap_keeps_longest_exact_span(self):
        candidates = [
            {
                "start": 0,
                "end": 5,
                "token_count": 1,
                "matched_by": "exact",
                "concept_id": "short",
                "icd_code": "S",
            },
            {
                "start": 0,
                "end": 12,
                "token_count": 2,
                "matched_by": "exact",
                "concept_id": "long",
                "icd_code": "L",
            },
        ]

        result = NEREngine._resolve_overlaps(candidates)

        self.assertEqual([item["concept_id"] for item in result], ["long"])

    def test_repeated_mentions_remain_occurrences_while_concepts_are_unique(self):
        text = "Bệnh lý tăng huyết áp và bệnh lý tăng huyết áp."
        _, concepts, entities, _ = run_ner(text, include_suggestions=False)

        matches = [item for item in entities if item["icd_code"] == "I10"]
        self.assertEqual(len(matches), 2)
        self.assertEqual(len([item for item in concepts if item["code"] == "I10"]), 1)
        self.assertNotEqual(matches[0]["start"], matches[1]["start"])

    def test_include_suggestions_false_does_not_run_review_candidates(self):
        _, _, entities, log = run_ner(
            "Bệnh tim do tăng huyet áp.",
            include_suggestions=False,
        )

        self.assertEqual(entities, [])
        self.assertEqual(log["suggestions"], [])

    def test_nfd_text_keeps_original_offsets(self):
        text = unicodedata.normalize("NFD", "Bệnh lý tăng huyết áp")
        _, _, entities, _ = run_ner(text, include_suggestions=False)

        self.assertTrue(entities)
        self.assertTrue(all(text[item["start"]:item["end"]] == item["text"] for item in entities))

    def test_bio_round_trip_preserves_span(self):
        text = "Bệnh nhân tăng huyết áp."
        entity = {
            "text": "tăng huyết áp",
            "label": "DISEASE",
            "start": text.index("tăng huyết áp"),
            "end": text.index("tăng huyết áp") + len("tăng huyết áp"),
        }

        tokens, issues = spans_to_bio(text, [entity])
        restored = bio_to_spans(text, tokens)

        self.assertEqual(issues, [])
        self.assertEqual(restored, [entity])

    def test_concept_id_mismatch_is_not_a_concept_true_positive(self):
        gold = [{
            "article_id": 1,
            "abstract": "tăng huyết áp",
            "entities": [{
                "text": "tăng huyết áp",
                "label": "DISEASE",
                "concept_id": "ICD10:I10:base",
                "start": 0,
                "end": len("tăng huyết áp"),
            }],
        }]
        prediction = [{
            "article_id": 1,
            "entities": [{
                "text": "tăng huyết áp",
                "label": "DISEASE",
                "concept_id": "ICD10:R03:base",
                "start": 0,
                "end": len("tăng huyết áp"),
            }],
        }]

        label_scores, _ = evaluate_system(gold, prediction, relaxed=False)
        concept_scores, _ = evaluate_system(
            gold,
            prediction,
            relaxed=False,
            require_concept_id=True,
        )

        self.assertEqual(label_scores["micro"]["f1"], 1.0)
        self.assertEqual(concept_scores["micro"]["tp"], 0)
        self.assertEqual(concept_scores["micro"]["fp"], 1)
        self.assertEqual(concept_scores["micro"]["fn"], 1)

    def test_accuracy_reports_token_and_document_accuracy(self):
        rows = [{
            "article_id": 1,
            "abstract": "tăng huyết áp",
            "entities": [{
                "text": "tăng huyết áp",
                "label": "DISEASE",
                "start": 0,
                "end": len("tăng huyết áp"),
            }],
        }]

        result = calculate_accuracy(rows, rows)

        self.assertTrue(result["token"]["available"])
        self.assertEqual(result["token"]["accuracy"], 1.0)
        self.assertEqual(result["document_exact"]["accuracy"], 1.0)

    def test_evaluate_run_writes_extended_metrics_without_breaking_legacy_csv(self):
        gold_row = {
            "article_id": 1,
            "abstract": "tăng huyết áp",
            "entities": [{
                "text": "tăng huyết áp",
                "label": "DISEASE",
                "concept_id": "ICD10:I10:base",
                "start": 0,
                "end": len("tăng huyết áp"),
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gold.jsonl").write_text(
                json.dumps(gold_row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            for system in ("current_ai", "vietbioner", "vimedner"):
                system_dir = root / system
                system_dir.mkdir()
                system_dir.joinpath("predictions.jsonl").write_text(
                    json.dumps({
                        "article_id": 1,
                        "entities": [{
                            "text": "tăng huyết áp",
                            "label": "DISEASE",
                            "concept_id": "ICD10:I10:base",
                            "matched_by": "exact",
                            "start": 0,
                            "end": len("tăng huyết áp"),
                        }],
                    }, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

            result = evaluate_run(root, root / "gold.jsonl")

            self.assertIn("concept_exact", result["current_ai"])
            self.assertIn("accuracy", result["current_ai"])
            self.assertIn("exact", result["current_ai"]["by_method"]["exact"])
            self.assertTrue((root / "metrics.csv").exists())
            self.assertTrue((root / "metrics_extra.csv").exists())


if __name__ == "__main__":
    unittest.main()
