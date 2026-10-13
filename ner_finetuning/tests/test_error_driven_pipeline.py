import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import error_driven_utils as utils  # noqa: E402
from generate_dev_error_log import ensure_dev_only  # noqa: E402


class ErrorClassificationTests(unittest.TestCase):
    def test_four_non_tp_categories_are_mutually_exclusive(self):
        sentence = "viêm phổi và ho dùng thuốc"
        gold = [
            {"text": "viêm phổi", "label": "DISEASE", "start": 0, "end": 9},
            {"text": "ho", "label": "SYMPTOM", "start": 13, "end": 15},
            {"text": "thuốc", "label": "TREATMENT", "start": 21, "end": 26},
        ]
        predicted = [
            {"text": "viêm phổi và", "label": "DISEASE", "start": 0, "end": 13},
            {"text": "ho", "label": "DISEASE", "start": 13, "end": 15},
            {"text": "dùng", "label": "TREATMENT", "start": 16, "end": 20},
        ]
        tp, errors = utils.classify_sentence("x", sentence, predicted, gold, 1)
        self.assertEqual(tp, 0)
        self.assertEqual(
            {row["error_type"] for row in errors},
            {"sai_bien", "sai_nhan", "nhan_du", "bo_sot"},
        )
        self.assertEqual(len(errors), 4)

    def test_exact_entity_is_tp_and_not_written_as_error(self):
        entity = {"text": "viêm phổi", "label": "DISEASE", "start": 0, "end": 9}
        tp, errors = utils.classify_sentence("x", "viêm phổi", [entity], [entity], 1)
        self.assertEqual(tp, 1)
        self.assertEqual(errors, [])

    def test_round_comparison_tracks_resolved_persistent_changed_and_new(self):
        sentence = "viêm phổi ho sốt"
        disease = {"text": "viêm phổi", "label": "DISEASE", "start": 0, "end": 9}
        symptom = {"text": "ho", "label": "SYMPTOM", "start": 10, "end": 12}
        old_extra = {"text": "sốt", "label": "DISEASE", "start": 13, "end": 16}
        new_extra = {"text": "sốt", "label": "SYMPTOM", "start": 13, "end": 16}
        previous = [
            utils.make_error("x", sentence, "bo_sot", None, disease, 1),
            utils.make_error("x", sentence, "bo_sot", None, symptom, 1),
            utils.make_error("x", sentence, "nhan_du", old_extra, None, 1),
        ]
        wrong_disease = {**disease, "label": "SYMPTOM"}
        current = [
            utils.make_error("x", sentence, "sai_nhan", wrong_disease, disease, 2),
            utils.make_error("x", sentence, "bo_sot", None, symptom, 2),
            utils.make_error("x", sentence, "nhan_du", new_extra, None, 2),
        ]
        comparison = utils.compare_error_rounds(previous, current)
        self.assertEqual(
            {
                "previous_errors": 3,
                "current_errors": 3,
                "resolved": 1,
                "persistent": 1,
                "changed": 1,
                "new": 1,
                "net_error_delta": 0,
            },
            comparison["summary"],
        )


class FewShotSelectionTests(unittest.TestCase):
    def make_error(self, index, error_type, label):
        entity = {"text": f"thực thể {index}", "label": label, "start": 0, "end": 10}
        return {
            "id": f"x{index}",
            "round": 1,
            "sentence": f"Câu y khoa số {index}",
            "error_type": error_type,
            "predicted": None if error_type == "bo_sot" else entity,
            "gold": None if error_type == "nhan_du" else entity,
            "pattern_key": f"{error_type}|{label}|{index}",
        }

    def test_selection_covers_error_types_and_labels(self):
        rows = []
        for index, error_type in enumerate(utils.ERROR_TYPES):
            rows.append(self.make_error(index, error_type, utils.LABELS[index]))
        rows.append(self.make_error(10, "bo_sot", "TREATMENT"))
        selected = utils.select_diverse_examples(rows, max_examples=10)
        self.assertEqual({item["error_type"] for item in selected}, set(utils.ERROR_TYPES))
        self.assertEqual({item["label"] for item in selected}, set(utils.LABELS))

    def test_prompt_is_bounded_and_replaces_old_error_section(self):
        rows = [self.make_error(index, "nhan_du", "DISEASE") for index in range(20)]
        base = "Quy tắc gốc\n\n" + utils.SECTION_MARKER + "\nví dụ cũ"
        prompt, selected = utils.build_prompt(base, rows, max_examples=20, max_chars=900)
        self.assertLessEqual(len(prompt), 900)
        self.assertEqual(prompt.count(utils.SECTION_MARKER), 1)
        self.assertGreater(len(selected), 0)


class DevGuardTests(unittest.TestCase):
    def test_rejects_test_path(self):
        with self.assertRaises(ValueError):
            ensure_dev_only(Path("processed/test.internal.jsonl"), [{"id": "vimedner:test:1"}])

    def test_accepts_dev_records(self):
        ensure_dev_only(Path("processed/dev.complete.internal.jsonl"), [{"id": "vimedner:dev:1"}])


if __name__ == "__main__":
    unittest.main()
