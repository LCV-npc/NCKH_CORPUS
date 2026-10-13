import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_medical_gold as build  # noqa: E402
import evaluate_medical_three_systems as evaluator  # noqa: E402
import run_prompt_v2_two_stage as prompt_v2  # noqa: E402


class MedicalGoldBuildTests(unittest.TestCase):
    def test_brat_keeps_only_continuous_diagnostic_with_matching_text(self):
        with tempfile.TemporaryDirectory() as temp:
            ann = Path(temp) / "sample.ann"
            text = "Chụp CT phát hiện bệnh."
            ann.write_text(
                "T1\tDiagnosticProcedure 0 7\tChụp CT\n"
                "T2\tSymptom_and_Disease 18 22\tbệnh\n"
                "T3\tDiagnosticProcedure 0 4;5 7\tChụp CT\n"
                "T4\tDiagnosticProcedure 0 3\tSai\n",
                encoding="utf-8",
            )
            entities, issues = build.parse_brat_ann(ann, text)
        self.assertEqual([(item["text"], item["label"]) for item in entities], [("Chụp CT", "DIAGNOSTIC")])
        self.assertIn("DISCONTINUOUS_EXCLUDED", {item["type"] for item in issues})
        self.assertIn("BRAT_TEXT_MISMATCH", {item["type"] for item in issues})

    def test_partial_bio_marks_unknown_tokens_ign(self):
        row = {
            "id": "x",
            "input_text": "Chụp CT phát hiện bệnh",
            "entities": [{"text": "Chụp CT", "label": "DIAGNOSTIC", "start": 0, "end": 7}],
        }
        block, issues = build.to_bio(row, partial_unknown=True)
        self.assertIn("Chụp B-DIAGNOSTIC", block)
        self.assertIn("bệnh IGN", block)
        self.assertEqual(issues, [])


class MedicalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.gold = [{"id": "x", "input_text": "Chụp CT thấy viêm phổi.", "entities": [
            {"text": "Chụp CT", "label": "DIAGNOSTIC", "start": 0, "end": 7},
            {"text": "viêm phổi", "label": "DISEASE", "start": 13, "end": 22},
        ]}]

    def test_self_score_and_diagnostic_alias(self):
        prediction = [{"id": "x", "entities": [
            {"text": "Chụp CT", "label": "DIAGNOSTIC_PROCEDURE", "start": 0, "end": 7},
            {"text": "viêm phổi", "label": "DISEASE", "start": 13, "end": 22},
        ]}]
        result, errors = evaluator.evaluate_system(self.gold, prediction, 0.5)
        self.assertEqual(result["exact"]["micro"]["f1"], 1.0)
        self.assertEqual(errors, [])

    def test_error_categories(self):
        prediction = [{"id": "x", "entities": [
            {"text": "Chụp", "label": "DIAGNOSTIC", "start": 0, "end": 4},
            {"text": "viêm phổi", "label": "SYMPTOM", "start": 13, "end": 22},
        ]}]
        _, errors = evaluator.evaluate_system(self.gold, prediction, 0.5)
        self.assertEqual({item["type"] for item in errors}, {"BOUNDARY_ERROR", "LABEL_ERROR"})


class PromptV2Tests(unittest.TestCase):
    def test_canonicalize_corrects_unique_offset(self):
        text = "Bệnh nhân bị viêm phổi."
        entities, issues = prompt_v2.canonicalize(text, [{
            "text": "viêm phổi", "label": "DISEASE", "start_offset": 0, "end_offset": 4
        }], "extractor")
        self.assertEqual((entities[0]["start"], entities[0]["end"]), (13, 22))
        self.assertEqual(issues[0]["type"], "OFFSET_CORRECTED")

    def test_canonicalize_rejects_ambiguous_surface_without_valid_offset(self):
        entities, issues = prompt_v2.canonicalize("ho rồi ho", [{
            "text": "ho", "label": "SYMPTOM", "start_offset": 1, "end_offset": 3
        }], "verifier")
        self.assertEqual(entities, [])
        self.assertEqual(issues[0]["type"], "NON_VERBATIM_OR_AMBIGUOUS")

    def test_flat_selection_prefers_shorter_overlap(self):
        entities = [
            {"text": "chẩn đoán viêm phổi", "label": "DISEASE", "start": 0, "end": 21},
            {"text": "viêm phổi", "label": "DISEASE", "start": 11, "end": 21},
        ]
        selected, rejected = prompt_v2.enforce_flat(entities)
        self.assertEqual([item["text"] for item in selected], ["viêm phổi"])
        self.assertEqual(len(rejected), 1)


if __name__ == "__main__":
    unittest.main()
