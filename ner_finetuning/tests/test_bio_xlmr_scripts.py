from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from bio_xlmr_utils import (  # noqa: E402
    encode_bio_sentences,
    exact_counts,
    read_bio,
    relaxed_metrics,
    seqeval_exact_metrics,
)
from train_bio_xlmr import latest_checkpoint, rebase_best_checkpoint  # noqa: E402


class BioXlmrScriptTests(unittest.TestCase):
    def test_latest_checkpoint_uses_largest_step(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "checkpoints" / "checkpoint-20").mkdir(parents=True)
            (root / "checkpoints" / "checkpoint-100").mkdir()
            (root / "checkpoints" / "checkpoint-invalid").mkdir()
            self.assertEqual(root / "checkpoints" / "checkpoint-100", latest_checkpoint(root))

    def test_rebase_best_checkpoint_after_mount_path_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            best = root / "checkpoints" / "checkpoint-20"
            latest = root / "checkpoints" / "checkpoint-100"
            best.mkdir(parents=True)
            latest.mkdir()
            state_path = latest / "trainer_state.json"
            state_path.write_text(
                '{"best_model_checkpoint": "/old/drive/checkpoints/checkpoint-20"}',
                encoding="utf-8",
            )

            self.assertEqual(best, rebase_best_checkpoint(latest))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(str(best), state["best_model_checkpoint"])

    def test_read_bio_and_ign_mask(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "train.txt"
            path.write_text("viêm B-DISEASE\nphổi I-DISEASE\n\nẩn IGN\nđoán B-DIAGNOSTIC\n", encoding="utf-8")
            rows = read_bio(path)
        self.assertEqual(2, len(rows))
        self.assertEqual(["viêm", "phổi"], rows[0]["tokens"])
        self.assertEqual(["IGN", "B-DIAGNOSTIC"], rows[1]["tags"])

        class Tokenizer:
            unk_token = "<unk>"
            model_input_names = ["input_ids", "attention_mask"]
            bos_token_id = 0
            eos_token_id = 2
            cls_token_id = None
            sep_token_id = None

            def tokenize(self, token):
                return [token]

            def convert_tokens_to_ids(self, tokens):
                return list(range(10, 10 + len(tokens)))

            def num_special_tokens_to_add(self, pair=False):
                return 2

        features, _ = encode_bio_sentences(rows[1:], Tokenizer(), max_length=8)
        self.assertEqual([-100, -100, 7, -100], features[0]["labels"])

    def test_relaxed_overlap_differs_from_exact(self):
        gold = [["B-DISEASE", "I-DISEASE", "O"]]
        predicted = [["O", "B-DISEASE", "O"]]
        exact = exact_counts(gold, predicted)
        relaxed = relaxed_metrics(gold, predicted)
        self.assertEqual(0, exact["DISEASE"]["tp"])
        self.assertEqual(1, relaxed["per_label"]["DISEASE"]["tp"])
        self.assertEqual(1.0, relaxed["micro"]["f1"])

    def test_exact_metrics_work_without_seqeval(self):
        gold = [["B-DISEASE", "I-DISEASE", "O", "B-SYMPTOM"]]
        predicted = [["B-DISEASE", "I-DISEASE", "O", "O"]]
        result = seqeval_exact_metrics(gold, predicted)
        self.assertEqual(1, result["micro"]["tp"])
        self.assertEqual(0, result["micro"]["fp"])
        self.assertEqual(1, result["micro"]["fn"])
        self.assertEqual(1.0, result["micro"]["precision"])
        self.assertEqual(0.5, result["micro"]["recall"])
        self.assertAlmostEqual(2 / 3, result["micro"]["f1"])


if __name__ == "__main__":
    unittest.main()
