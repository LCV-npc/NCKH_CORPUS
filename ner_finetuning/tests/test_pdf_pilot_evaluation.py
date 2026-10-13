import sys
import unittest
from collections import Counter
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_pilot10_gold_evaluation as pilot  # noqa: E402


class PilotGoldEvaluationTests(unittest.TestCase):
    def test_flat_longest_keeps_complete_span(self):
        entities = [
            {"text": "viêm phổi", "label": "DISEASE", "start": 0, "end": 10},
            {"text": "viêm phổi kẽ", "label": "DISEASE", "start": 0, "end": 13},
        ]
        kept, dropped = pilot.flat_longest(entities)
        self.assertEqual([item["text"] for item in kept], ["viêm phổi kẽ"])
        self.assertEqual(len(dropped), 1)

    def test_matching_is_one_to_one(self):
        gold = [{"text": "ho", "label": "SYMPTOM", "start": 0, "end": 2}]
        predicted = [dict(gold[0]), dict(gold[0])]
        self.assertEqual(len(pilot.maximum_pairs(predicted, gold, relaxed=False)), 1)

    def test_error_categories_and_self_score(self):
        gold = [{
            "article_id": "pdf-test",
            "entities": [
                {"text": "viêm phổi", "label": "DISEASE", "start": 0, "end": 9},
                {"text": "ho", "label": "SYMPTOM", "start": 10, "end": 12},
            ],
        }]
        exact, errors = pilot.evaluate_system(
            gold,
            [{"article_id": "pdf-test", "entities": gold[0]["entities"]}],
            relaxed=False,
        )
        self.assertEqual(exact["micro"]["f1"], 1.0)
        self.assertEqual(errors, [])

        _, errors = pilot.evaluate_system(
            gold,
            [{"article_id": "pdf-test", "entities": [
                {"text": "phổi", "label": "DISEASE", "start": 5, "end": 9},
                {"text": "ho", "label": "DISEASE", "start": 10, "end": 12},
                {"text": "sốt", "label": "SYMPTOM", "start": 20, "end": 23},
            ]}],
            relaxed=False,
        )
        self.assertEqual(Counter(error["type"] for error in errors), Counter({"BOUNDARY_ERROR": 1, "LABEL_ERROR": 1, "FP": 1}))


if __name__ == "__main__":
    unittest.main()
