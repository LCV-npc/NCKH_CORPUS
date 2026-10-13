#!/usr/bin/env python3
"""Hugging Face runtime helpers, imported lazily so data tools work without PyTorch."""

from __future__ import annotations

import inspect
from pathlib import Path

from supervised_ner_utils import BIO_LABELS, ID2LABEL, LABEL2ID, entity_counts_from_bio, record_to_words


def require_dependencies():
    try:
        import numpy as np
        import torch
        from torch import nn
        from torch.nn.utils.rnn import pad_sequence
        from transformers import (
            AutoConfig,
            AutoModel,
            AutoModelForTokenClassification,
            AutoTokenizer,
            DataCollatorForTokenClassification,
            EarlyStoppingCallback,
            PreTrainedModel,
            Trainer,
            TrainingArguments,
        )
        from transformers.modeling_outputs import TokenClassifierOutput
    except ImportError as exc:
        raise RuntimeError(
            "Missing supervised-training dependencies. Create a Python 3.10/3.11 environment and "
            "install ner_finetuning/requirements-supervised.txt"
        ) from exc
    return locals()


def create_crf_model_class(deps: dict):
    torch = deps["torch"]
    nn = deps["nn"]
    pad_sequence = deps["pad_sequence"]
    AutoModel = deps["AutoModel"]
    PreTrainedModel = deps["PreTrainedModel"]
    TokenClassifierOutput = deps["TokenClassifierOutput"]
    try:
        from torchcrf import CRF
    except ImportError as exc:
        raise RuntimeError("--use-crf requires pytorch-crf") from exc

    class TransformerCrfForTokenClassification(PreTrainedModel):
        def __init__(self, config, encoder=None):
            super().__init__(config)
            self.num_labels = config.num_labels
            self.encoder = encoder if encoder is not None else AutoModel.from_config(config)
            dropout_rate = getattr(config, "classifier_dropout", None) or getattr(config, "hidden_dropout_prob", 0.1)
            self.dropout = nn.Dropout(dropout_rate)
            self.classifier = nn.Linear(config.hidden_size, config.num_labels)
            self.crf = CRF(config.num_labels, batch_first=True)
            self.config.use_crf = True
            if encoder is None:
                self.post_init()

        def _compact(self, emissions, labels):
            compact_emissions = []
            compact_labels = []
            positions = []
            for batch_index in range(emissions.shape[0]):
                valid = torch.nonzero(labels[batch_index] != -100, as_tuple=False).flatten()
                positions.append(valid)
                compact_emissions.append(emissions[batch_index, valid])
                compact_labels.append(labels[batch_index, valid])
            lengths = [item.shape[0] for item in compact_emissions]
            padded_emissions = pad_sequence(compact_emissions, batch_first=True)
            padded_labels = pad_sequence(compact_labels, batch_first=True, padding_value=0)
            mask = torch.arange(padded_emissions.shape[1], device=emissions.device)[None, :] < torch.tensor(
                lengths, device=emissions.device
            )[:, None]
            return padded_emissions, padded_labels.long(), mask.bool(), positions

        def forward(self, input_ids=None, attention_mask=None, labels=None, **kwargs):
            outputs = self.encoder(input_ids=input_ids, attention_mask=attention_mask, **kwargs)
            emissions = self.classifier(self.dropout(outputs.last_hidden_state))
            loss = None
            display_logits = emissions
            if labels is not None:
                compact, compact_labels, compact_mask, positions = self._compact(emissions, labels)
                loss = -self.crf(compact, compact_labels, mask=compact_mask, reduction="mean")
                paths = self.crf.decode(compact, mask=compact_mask)
                display_logits = emissions.new_full(emissions.shape, -10000.0)
                display_logits[:, :, 0] = 0.0
                for batch_index, path in enumerate(paths):
                    for original_position, tag_id in zip(positions[batch_index].tolist(), path):
                        display_logits[batch_index, original_position, :] = -10000.0
                        display_logits[batch_index, original_position, tag_id] = 10000.0
            return TokenClassifierOutput(loss=loss, logits=display_logits, hidden_states=outputs.hidden_states, attentions=outputs.attentions)

    return TransformerCrfForTokenClassification


def load_tokenizer(deps: dict, model_or_checkpoint: str):
    return deps["AutoTokenizer"].from_pretrained(model_or_checkpoint, use_fast=False)


def load_model(deps: dict, model_or_checkpoint: str, use_crf: bool, initialize_from_encoder: bool):
    config = deps["AutoConfig"].from_pretrained(
        model_or_checkpoint,
        num_labels=len(BIO_LABELS),
        id2label=ID2LABEL,
        label2id=LABEL2ID,
    )
    if use_crf:
        model_class = create_crf_model_class(deps)
        if initialize_from_encoder:
            encoder = deps["AutoModel"].from_pretrained(model_or_checkpoint, config=config)
            return model_class(config, encoder=encoder)
        return model_class.from_pretrained(model_or_checkpoint, config=config)
    return deps["AutoModelForTokenClassification"].from_pretrained(model_or_checkpoint, config=config)


def _encode_single_sequence(tokenizer, token_ids: list[int]) -> tuple[dict, list[int]]:
    """Add special tokens without relying on helpers removed in Transformers 5."""
    if hasattr(tokenizer, "prepare_for_model"):
        encoded = tokenizer.prepare_for_model(
            token_ids,
            add_special_tokens=True,
            truncation=False,
            return_attention_mask=True,
            return_special_tokens_mask=True,
        )
        special_mask = encoded.pop("special_tokens_mask")
        return dict(encoded), special_mask

    start_id = getattr(tokenizer, "cls_token_id", None)
    if start_id is None:
        start_id = getattr(tokenizer, "bos_token_id", None)
    end_id = getattr(tokenizer, "sep_token_id", None)
    if end_id is None:
        end_id = getattr(tokenizer, "eos_token_id", None)
    special_count = tokenizer.num_special_tokens_to_add(pair=False)
    if special_count == 0:
        input_ids = list(token_ids)
        special_mask = [0] * len(token_ids)
    elif special_count == 2 and start_id is not None and end_id is not None:
        # PhoBERT and XLM-R both encode one sequence as BOS + tokens + EOS.
        input_ids = [start_id, *token_ids, end_id]
        special_mask = [1, *([0] * len(token_ids)), 1]
    else:
        raise RuntimeError(
            f"Unsupported tokenizer special-token layout: {tokenizer.__class__.__name__} "
            f"reports {special_count} special tokens"
        )
    encoded = {"input_ids": input_ids, "attention_mask": [1] * len(input_ids)}
    if "token_type_ids" in getattr(tokenizer, "model_input_names", []):
        encoded["token_type_ids"] = [0] * len(input_ids)
    return encoded, special_mask


def encode_records(rows: list[dict], tokenizer, max_length: int) -> tuple[list[dict], list[dict]]:
    features: list[dict] = []
    metadata: list[dict] = []
    for record_index, row in enumerate(rows):
        words, tags = record_to_words(row)
        pieces_by_word = []
        for word in words:
            pieces = tokenizer.tokenize(word["text"])
            pieces_by_word.append(pieces or [tokenizer.unk_token])
        special_count = tokenizer.num_special_tokens_to_add(pair=False)
        start = 0
        while start < len(words):
            end = start
            piece_count = 0
            while end < len(words) and piece_count + len(pieces_by_word[end]) + special_count <= max_length:
                piece_count += len(pieces_by_word[end])
                end += 1
            if end == start:
                raise ValueError(f"{row['id']}: one token exceeds max_length={max_length}")
            if end < len(words) and tags[end].startswith("I-"):
                entity_label = tags[end].split("-", 1)[1]
                while end > start and tags[end - 1] in {f"I-{entity_label}", f"B-{entity_label}"}:
                    end -= 1
            if end <= start:
                raise ValueError(f"{row['id']}: entity is too long for max_length={max_length}")

            flat_tokens: list[str] = []
            flat_word_ids: list[int] = []
            for local_word_id, pieces in enumerate(pieces_by_word[start:end]):
                flat_tokens.extend(pieces)
                flat_word_ids.extend([local_word_id] * len(pieces))
            token_ids = tokenizer.convert_tokens_to_ids(flat_tokens)
            encoded, special_mask = _encode_single_sequence(tokenizer, token_ids)
            word_ids: list[int | None] = []
            flat_index = 0
            for is_special in special_mask:
                if is_special:
                    word_ids.append(None)
                else:
                    word_ids.append(flat_word_ids[flat_index])
                    flat_index += 1
            first_subword: set[int] = set()
            aligned_labels: list[int] = []
            for word_id in word_ids:
                if word_id is None or word_id in first_subword:
                    aligned_labels.append(-100)
                else:
                    first_subword.add(word_id)
                    tag = tags[start + word_id]
                    aligned_labels.append(-100 if tag == "IGN" else LABEL2ID[tag])
            if len(first_subword) != end - start or len(encoded["input_ids"]) > max_length:
                raise ValueError(f"{row['id']}: invalid subword chunk alignment")
            feature = dict(encoded)
            feature["labels"] = aligned_labels
            features.append(feature)
            metadata.append({
                "record_index": record_index,
                "record_id": row["id"],
                "word_start": start,
                "word_end": end,
                "word_ids": word_ids,
            })
            start = end
    return features, metadata


def make_dataset_class(torch):
    class FeatureDataset(torch.utils.data.Dataset):
        def __init__(self, features: list[dict]):
            self.features = features

        def __len__(self):
            return len(self.features)

        def __getitem__(self, index):
            return self.features[index]

    return FeatureDataset


def compute_metrics(eval_prediction) -> dict:
    import numpy as np

    predictions = np.argmax(eval_prediction.predictions, axis=-1)
    gold_sequences: list[list[str]] = []
    pred_sequences: list[list[str]] = []
    for predicted, gold in zip(predictions, eval_prediction.label_ids):
        valid = gold != -100
        gold_sequences.append([ID2LABEL[int(value)] for value in gold[valid]])
        pred_sequences.append([ID2LABEL[int(value)] for value in predicted[valid]])
    result = entity_counts_from_bio(gold_sequences, pred_sequences)
    metrics = {
        "micro_precision": result["micro"]["precision"],
        "micro_recall": result["micro"]["recall"],
        "micro_f1": result["micro"]["f1"],
        "macro_f1": result["macro"]["f1"],
    }
    metrics.update({f"f1_{label.lower()}": result["per_label"][label]["f1"] for label in result["per_label"]})
    return metrics


def training_arguments(deps: dict, **kwargs):
    signature = inspect.signature(deps["TrainingArguments"].__init__)
    parameters = signature.parameters
    if "eval_strategy" in parameters and "evaluation_strategy" in kwargs:
        kwargs["eval_strategy"] = kwargs.pop("evaluation_strategy")
    if "warmup_ratio" in kwargs and "warmup_ratio" not in parameters:
        if "warmup_steps" in parameters:
            # Transformers 5 accepts a float in [0, 1) as a ratio here.
            kwargs["warmup_steps"] = kwargs.pop("warmup_ratio")
        elif "warmup_step" in parameters:
            kwargs["warmup_step"] = kwargs.pop("warmup_ratio")
    return deps["TrainingArguments"](**kwargs)


def trainer_instance(deps: dict, *, class_weights=None, use_crf=False, **kwargs):
    Trainer = deps["Trainer"]
    torch = deps["torch"]
    if class_weights is None or use_crf:
        return Trainer(**kwargs)

    class WeightedTrainer(Trainer):
        def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
            labels = inputs.pop("labels")
            outputs = model(**inputs)
            logits = outputs.logits
            weights = torch.tensor(class_weights, dtype=logits.dtype, device=logits.device)
            loss = torch.nn.functional.cross_entropy(
                logits.view(-1, logits.shape[-1]), labels.view(-1), weight=weights, ignore_index=-100
            )
            return (loss, outputs) if return_outputs else loss

    return WeightedTrainer(**kwargs)


def processing_keyword(deps: dict, tokenizer) -> dict:
    signature = inspect.signature(deps["Trainer"].__init__)
    return {"processing_class": tokenizer} if "processing_class" in signature.parameters else {"tokenizer": tokenizer}
