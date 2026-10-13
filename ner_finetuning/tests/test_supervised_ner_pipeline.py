from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from supervised_ner_utils import (  # noqa: E402
    augment_with_synonyms,
    bio_to_entities,
    entity_counts_from_bio,
    record_to_words,
)
from supervised_ner_hf import encode_records, training_arguments  # noqa: E402


class SupervisedNerPipelineTests(unittest.TestCase):
    def test_transformers_v5_training_argument_aliases(self):
        class TrainingArgumentsV5:
            def __init__(self, output_dir, eval_strategy="no", warmup_steps=0):
                self.output_dir = output_dir
                self.eval_strategy = eval_strategy
                self.warmup_steps = warmup_steps

        args = training_arguments(
            {"TrainingArguments": TrainingArgumentsV5},
            output_dir="checkpoints",
            evaluation_strategy="epoch",
            warmup_ratio=0.1,
        )
        self.assertEqual("epoch", args.eval_strategy)
        self.assertEqual(0.1, args.warmup_steps)

    def test_transformers_v5_slow_tokenizer_fallback(self):
        class SlowTokenizer:
            unk_token = "<unk>"
            model_input_names = ["input_ids", "attention_mask"]
            bos_token_id = 0
            eos_token_id = 2
            cls_token_id = None
            sep_token_id = None

            def tokenize(self, text):
                return [text]

            def convert_tokens_to_ids(self, tokens):
                return list(range(10, 10 + len(tokens)))

            def num_special_tokens_to_add(self, pair=False):
                return 2

        text = "viêm phổi"
        row = {
            "id": "train:slow",
            "input_text": text,
            "entities": [{"text": text, "label": "DISEASE", "start": 0, "end": len(text)}],
        }
        features, metadata = encode_records([row], SlowTokenizer(), max_length=8)
        self.assertEqual([0, 10, 11, 2], features[0]["input_ids"])
        self.assertEqual([-100, 1, 2, -100], features[0]["labels"])
        self.assertEqual([None, 0, 1, None], metadata[0]["word_ids"])

    def test_span_to_bio_and_back(self):
        text = "Bệnh nhân bị viêm phổi và sốt cao."
        disease_start = text.index("viêm phổi")
        symptom_start = text.index("sốt cao")
        row = {
            "id": "dev:1",
            "input_text": text,
            "entities": [
                {"text": "viêm phổi", "label": "DISEASE", "start": disease_start, "end": disease_start + len("viêm phổi")},
                {"text": "sốt cao", "label": "SYMPTOM", "start": symptom_start, "end": symptom_start + len("sốt cao")},
            ],
        }
        words, tags = record_to_words(row)
        self.assertIn("B-DISEASE", tags)
        self.assertIn("I-DISEASE", tags)
        self.assertEqual(row["entities"], bio_to_entities(words, tags, text))

    def test_entity_exact_metrics(self):
        gold = [["B-DISEASE", "I-DISEASE", "O", "B-SYMPTOM"]]
        pred = [["B-DISEASE", "I-DISEASE", "O", "O"]]
        metrics = entity_counts_from_bio(gold, pred)
        self.assertEqual(1, metrics["micro"]["tp"])
        self.assertEqual(0, metrics["micro"]["fp"])
        self.assertEqual(1, metrics["micro"]["fn"])
        self.assertAlmostEqual(2 / 3, metrics["micro"]["f1"])

    def test_curated_augmentation_updates_following_offsets(self):
        text = "Do vi khuẩn A gây viêm phổi."
        cause_start = text.index("vi khuẩn A")
        disease_start = text.index("viêm phổi")
        rows = [{
            "id": "train:1",
            "input_text": text,
            "entities": [
                {"text": "vi khuẩn A", "label": "CAUSE", "start": cause_start, "end": cause_start + len("vi khuẩn A")},
                {"text": "viêm phổi", "label": "DISEASE", "start": disease_start, "end": disease_start + len("viêm phổi")},
            ],
        }]
        with tempfile.TemporaryDirectory() as directory:
            mapping = Path(directory) / "synonyms.json"
            mapping.write_text(json.dumps({"vi khuẩn A": ["virus B"]}, ensure_ascii=False), encoding="utf-8")
            augmented = augment_with_synonyms(rows, mapping, {"CAUSE"})
        self.assertEqual(1, len(augmented))
        self.assertEqual("virus B", augmented[0]["entities"][0]["text"])
        disease = augmented[0]["entities"][1]
        self.assertEqual("viêm phổi", augmented[0]["input_text"][disease["start"]:disease["end"]])


if __name__ == "__main__":
    unittest.main()
