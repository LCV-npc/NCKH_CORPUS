#!/usr/bin/env python3
"""Fine-tune PhoBERT/XLM-R for five-label Vietnamese medical NER on Train/Dev only."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from supervised_ner_hf import (
    compute_metrics,
    encode_records,
    load_model,
    load_tokenizer,
    make_dataset_class,
    processing_keyword,
    require_dependencies,
    trainer_instance,
    training_arguments,
)
from supervised_ner_utils import augment_with_synonyms, class_weights, read_jsonl


ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_args() -> argparse.Namespace:
    gold = ROOT / "ner_finetuning" / "processed" / "medical_gold_v1"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, default=gold / "train.complete.internal.jsonl")
    parser.add_argument("--partial-train", type=Path, default=gold / "diagnostic_supplement.partial.internal.jsonl")
    parser.add_argument("--exclude-partial-train", action="store_true")
    parser.add_argument("--dev", type=Path, default=gold / "dev.complete.internal.jsonl")
    parser.add_argument("--model-name", default="vinai/phobert-base", help="Examples: vinai/phobert-base, FacebookAI/xlm-roberta-large")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--epochs", type=float, default=10.0)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--gradient-accumulation", type=int, default=2)
    parser.add_argument("--gradient-checkpointing", action="store_true")
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-ratio", type=float, default=0.1)
    parser.add_argument("--early-stopping-patience", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--use-crf", action="store_true")
    parser.add_argument("--class-weighted", action="store_true")
    parser.add_argument("--outside-weight", type=float, default=0.15)
    parser.add_argument("--augmentation-map", type=Path)
    parser.add_argument("--augmentation-labels", default="CAUSE,DIAGNOSTIC")
    parser.add_argument("--max-augment-per-record", type=int, default=1)
    parser.add_argument("--fp16", action="store_true")
    parser.add_argument("--bf16", action="store_true")
    return parser.parse_args()


def assert_protocol(args: argparse.Namespace, train_rows: list[dict], dev_rows: list[dict]) -> None:
    if "test" in args.train.name.lower() or "test" in args.dev.name.lower():
        raise ValueError("train.py must never receive a Test split")
    if any(str(row.get("source_split", "")).lower() == "test" for row in train_rows + dev_rows):
        raise ValueError("Train/Dev input contains a Test record")
    train_ids = {row["id"] for row in train_rows}
    dev_ids = {row["id"] for row in dev_rows}
    overlap = train_ids & dev_ids
    if overlap:
        raise ValueError(f"Train/Dev ID leakage: {len(overlap)} records")


def main() -> int:
    args = parse_args()
    random.seed(args.seed)
    train_rows = read_jsonl(args.train)
    dev_rows = read_jsonl(args.dev)
    assert_protocol(args, train_rows, dev_rows)

    partial_rows: list[dict] = []
    if not args.exclude_partial_train and args.partial_train and args.partial_train.exists():
        partial_rows = read_jsonl(args.partial_train)
        if any(not row.get("is_partial_annotation") for row in partial_rows):
            raise ValueError("--partial-train must contain only partial annotations")

    augmentation_count = 0
    if args.augmentation_map:
        labels = {label.strip() for label in args.augmentation_labels.split(",") if label.strip()}
        augmented = augment_with_synonyms(train_rows, args.augmentation_map, labels, args.max_augment_per_record)
        augmentation_count = len(augmented)
        train_rows = train_rows + augmented
    train_rows = train_rows + partial_rows

    deps = require_dependencies()
    tokenizer = load_tokenizer(deps, args.model_name)
    train_features, train_metadata = encode_records(train_rows, tokenizer, args.max_length)
    dev_features, dev_metadata = encode_records(dev_rows, tokenizer, args.max_length)
    Dataset = make_dataset_class(deps["torch"])
    train_dataset = Dataset(train_features)
    dev_dataset = Dataset(dev_features)

    weights = class_weights(train_rows, args.outside_weight) if args.class_weighted else None
    model = load_model(deps, args.model_name, args.use_crf, initialize_from_encoder=True)
    if args.gradient_checkpointing:
        model.gradient_checkpointing_enable()
    model.config.training_recipe = {
        "model_name": args.model_name,
        "use_crf": args.use_crf,
        "class_weighted": args.class_weighted,
        "max_length": args.max_length,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    hf_args = training_arguments(
        deps,
        output_dir=str(args.output_dir / "checkpoints"),
        num_train_epochs=args.epochs,
        learning_rate=args.learning_rate,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation,
        weight_decay=args.weight_decay,
        warmup_ratio=args.warmup_ratio,
        evaluation_strategy="epoch",
        save_strategy="epoch",
        logging_strategy="steps",
        logging_steps=50,
        load_best_model_at_end=True,
        metric_for_best_model="macro_f1",
        greater_is_better=True,
        save_total_limit=2,
        report_to=[],
        seed=args.seed,
        data_seed=args.seed,
        fp16=args.fp16,
        bf16=args.bf16,
        gradient_checkpointing=args.gradient_checkpointing,
    )
    callbacks = [deps["EarlyStoppingCallback"](early_stopping_patience=args.early_stopping_patience)]
    trainer = trainer_instance(
        deps,
        class_weights=weights,
        use_crf=args.use_crf,
        model=model,
        args=hf_args,
        train_dataset=train_dataset,
        eval_dataset=dev_dataset,
        data_collator=deps["DataCollatorForTokenClassification"](tokenizer=tokenizer),
        compute_metrics=compute_metrics,
        callbacks=callbacks,
        **processing_keyword(deps, tokenizer),
    )
    train_result = trainer.train()
    dev_metrics = trainer.evaluate()
    best_dir = args.output_dir / "best_model"
    trainer.save_model(str(best_dir))
    tokenizer.save_pretrained(str(best_dir))

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "Train/Dev only; checkpoint selected by Dev entity macro-F1; Test untouched",
        "model_name": args.model_name,
        "architecture": "transformer_crf" if args.use_crf else "transformer_token_classification",
        "class_weighted": args.class_weighted,
        "class_weights": weights,
        "train_path": str(args.train.resolve()),
        "dev_path": str(args.dev.resolve()),
        "train_checksum": sha256(args.train),
        "dev_checksum": sha256(args.dev),
        "train_records_original": len(train_rows) - augmentation_count,
        "train_records_augmented": augmentation_count,
        "partial_train_records": len(partial_rows),
        "dev_records": len(dev_rows),
        "train_chunks": len(train_features),
        "dev_chunks": len(dev_features),
        "max_length": args.max_length,
        "best_checkpoint": trainer.state.best_model_checkpoint,
        "best_metric": trainer.state.best_metric,
        "dev_metrics": dev_metrics,
        "train_metrics": train_result.metrics,
        "test_accessed": False,
        "hyperparameters": vars(args) | {"train": str(args.train), "dev": str(args.dev), "partial_train": str(args.partial_train) if args.partial_train else None, "output_dir": str(args.output_dir), "augmentation_map": str(args.augmentation_map) if args.augmentation_map else None},
    }
    (args.output_dir / "training_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps({"best_model": str(best_dir), "best_dev_macro_f1": trainer.state.best_metric, "test_accessed": False}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
