import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import evaluate_ner  # noqa: E402
import prepare_gemini_data as prepare  # noqa: E402
import run_four_systems as runner  # noqa: E402


class PrepareTests(unittest.TestCase):
    def test_vietbio_io_spans_and_offsets(self):
        rows = [
            prepare.TokenRow("bệnh", "I-Symptom_and_Disease"),
            prepare.TokenRow("lao", "I-Symptom_and_Disease"),
            prepare.TokenRow(",", "O"),
            prepare.TokenRow("ho", "I-Symptom_and_Disease"),
            prepare.TokenRow(".", "O"),
        ]
        text, _ = prepare.detokenize(rows)
        spans, issues = prepare.collect_spans(rows, "vietbioner")
        self.assertEqual(text, "bệnh lao, ho.")
        self.assertEqual([item["text"] for item in spans], ["bệnh lao", "ho"])
        self.assertEqual(issues, [])
        for span in spans:
            self.assertEqual(text[span["start"] : span["end"]], span["text"])

    def test_vimed_adjacent_b_tags_stay_separate(self):
        rows = [
            prepare.TokenRow("sởi", "B-ten_benh"),
            prepare.TokenRow("sởi", "B-ten_benh"),
        ]
        spans, _ = prepare.collect_spans(rows, "vimedner")
        self.assertEqual([item["text"] for item in spans], ["sởi", "sởi"])

    def test_read_conll_supports_tokens_containing_spaces_only_by_last_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.txt"
            path.write_text("viêm B-ten_benh\nphổi I-ten_benh\n\n", encoding="utf-8")
            sentences, issues = prepare.read_conll(path)
        self.assertEqual(len(sentences), 1)
        self.assertEqual(sentences[0][1].token, "phổi")
        self.assertEqual(issues, [])

    def test_named_fever_is_disease(self):
        label, confidence, _ = prepare.classify_symptom_disease(
            "sốt xuất huyết", "Bệnh nhân mắc sốt xuất huyết.", 14, set(), set()
        )
        self.assertEqual(label, "DISEASE")
        self.assertGreaterEqual(confidence, 0.9)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.gold = [
            {
                "id": "x",
                "input_text": "Bệnh nhân viêm phổi nặng.",
                "entities": [
                    {"text": "viêm phổi", "label": "DISEASE", "start": 10, "end": 20}
                ],
            }
        ]

    def test_exact_match(self):
        result = evaluate_ner.evaluate(self.gold, self.gold, "exact", 0.5)
        self.assertEqual(result["micro"]["f1"], 1.0)

    def test_relaxed_match_accepts_boundary_overlap(self):
        prediction = [
            {
                "id": "x",
                "input_text": "Bệnh nhân viêm phổi nặng.",
                "entities": [
                    {"text": "viêm phổi nặng", "label": "DISEASE", "start": 10, "end": 25}
                ],
            }
        ]
        exact = evaluate_ner.evaluate(self.gold, prediction, "exact", 0.5)
        relaxed = evaluate_ner.evaluate(self.gold, prediction, "relaxed", 0.5)
        self.assertEqual(exact["micro"]["f1"], 0.0)
        self.assertEqual(relaxed["micro"]["f1"], 1.0)

    def test_output_text_is_aligned_left_to_right(self):
        record = {
            "input_text": "ho rồi ho",
            "output_text": json.dumps(
                {"entities": [{"text": "ho", "label": "SYMPTOM"}, {"text": "ho", "label": "SYMPTOM"}]}
            ),
        }
        entities, unaligned = evaluate_ner.add_offsets(
            record["input_text"], evaluate_ner.parse_entities(record)
        )
        self.assertEqual([(item["start"], item["end"]) for item in entities], [(0, 2), (7, 9)])
        self.assertEqual(unaligned, 0)


class RunnerTests(unittest.TestCase):
    def test_dictionary_matcher_prefers_longest_non_overlapping_term(self):
        matcher = runner.DictionaryMatcher(
            [
                {"TÊN BỆNH": "viêm phổi", "ENTITY_TYPE": "DISEASE"},
                {"TÊN BỆNH": "phổi", "ENTITY_TYPE": "DISEASE"},
            ]
        )
        result = matcher.find("Bệnh nhân bị viêm phổi.")
        self.assertEqual([(item["text"], item["label"]) for item in result], [("viêm phổi", "DISEASE")])

    def test_decode_structured_response(self):
        payload = {
            "candidates": [
                {"content": {"parts": [{"text": "```json\n{\"entities\":[]}\n```"}]}}
            ]
        }
        self.assertEqual(runner.decode_response(payload), {"entities": []})


if __name__ == "__main__":
    unittest.main()
