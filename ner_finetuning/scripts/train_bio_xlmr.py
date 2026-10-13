#!/usr/bin/env python3
"""Fine-tune XLM-R token classification directly from Train/Dev BIO files."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from bio_xlmr_utils import encode_bio_sentences, read_bio
from supervised_ner_hf import (
    compute_metrics,
    load_model,
    load_tokenizer,
    make_dataset_class,
    processing_keyword,
    require_dependencies,
    trainer_instance,
    training_arguments,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument(
        "--partial-train",
        type=Path,
        action="append",
        default=[],
        help="Optional partial BIO file; pass more than once if needed. IGN tokens receive loss -100.",
    )
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-name", default="FacebookAI/xlm-roberta-base")
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--gradient-accumulation", type=int, default=2)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--epochs", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-ratio", type=float, default=0.1)
    parser.add_argument("--early-stopping-patience", type=int, default=3)
    parser.add_argument("--gradient-checkpointing", action="store_true")
    parser.add_argument("--fp16", action="store_true")
    parser.add_argument("--bf16", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the latest output-dir/checkpoints/checkpoint-* directory.",
    )
    return parser.parse_args()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest_checkpoint(output_dir: Path) -> Path | None:
    checkpoints_dir = output_dir / "checkpoints"
    candidates: list[tuple[int, Path]] = []
    if checkpoints_dir.is_dir():
        for path in checkpoints_dir.glob("checkpoint-*"):
            try:
                step = int(path.name.rsplit("-", 1)[1])
            except ValueError:
                continue
            if path.is_dir():
                candidates.append((step, path))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def rebase_best_checkpoint(resume_checkpoint: Path) -> Path | None:
    """Keep Trainer's best-checkpoint path valid after a Drive mount/path change."""
    state_path = resume_checkpoint / "trainer_state.json"
    if not state_path.is_file():
        return None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    stored = state.get("best_model_checkpoint")
    if not stored:
        return None
    candidate = resume_checkpoint.parent / Path(stored).name
    if candidate.is_dir() and str(candidate) != stored:
        state["best_model_checkpoint"] = str(candidate)
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return candidate
    return Path(stored)


def main() -> int:
    args = parse_args()
    if any("test" in path.name.casefold() for path in [args.train, args.dev, *args.partial_train]):
        raise ValueError("Training accepts Train/Dev only; Test must stay frozen")
    resume_checkpoint = latest_checkpoint(args.output_dir) if args.resume else None
    if args.resume and resume_checkpoint is None:
        raise FileNotFoundError(f"No resumable checkpoint found under: {args.output_dir / 'checkpoints'}")
    if resume_checkpoint:
        rebased_best = rebase_best_checkpoint(resume_checkpoint)
        if rebased_best:
            print(f"Best checkpoint for this mount: {rebased_best}")
    if args.output_dir.exists() and not args.resume:
        raise FileExistsError(f"Output checkpoint already exists: {args.output_dir}")
    random.seed(args.seed)
    train_sentences = read_bio(args.train)
    partial_sentences: list[dict] = []
    for path in args.partial_train:
        rows = read_bio(path)
        if any("IGN" not in sentence["tags"] for sentence in rows):
            raise ValueError(f"Every partial sentence must contain IGN: {path}")
        partial_sentences.extend(rows)
    train_sentences.extend(partial_sentences)
    dev_sentences = read_bio(args.dev)
    if any("IGN" in sentence["tags"] for sentence in dev_sentences):
        raise ValueError("Dev must be completely annotated; IGN is Train-only")

    deps = require_dependencies()
    tokenizer = load_tokenizer(deps, args.model_name)
    train_features, _ = encode_bio_sentences(train_sentences, tokenizer, args.max_length)
    dev_features, _ = encode_bio_sentences(dev_sentences, tokenizer, args.max_length)
    Dataset = make_dataset_class(deps["torch"])
    model = load_model(deps, args.model_name, use_crf=False, initialize_from_encoder=True)
    if args.gradient_checkpointing:
        model.gradient_checkpointing_enable()
    model.config.training_recipe = {"source_format": "BIO", "ign_loss_id": -100, "model_name": args.model_name}

    args.output_dir.mkdir(parents=True, exist_ok=args.resume)
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
    trainer = trainer_instance(
        deps,
        model=model,
        args=hf_args,
        train_dataset=Dataset(train_features),
        eval_dataset=Dataset(dev_features),
        data_collator=deps["DataCollatorForTokenClassification"](tokenizer=tokenizer),
        compute_metrics=compute_metrics,
        callbacks=[deps["EarlyStoppingCallback"](early_stopping_patience=args.early_stopping_patience)],
        **processing_keyword(deps, tokenizer),
    )
    train_result = trainer.train(resume_from_checkpoint=str(resume_checkpoint) if resume_checkpoint else None)
    dev_metrics = trainer.evaluate()
    best_model = args.output_dir / "best_model"
    trainer.save_model(str(best_model))
    tokenizer.save_pretrained(str(best_model))
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "train": str(args.train.resolve()),
        "partial_train": [str(path.resolve()) for path in args.partial_train],
        "dev": str(args.dev.resolve()),
        "train_sha256": sha256(args.train),
        "partial_train_sha256": {str(path.resolve()): sha256(path) for path in args.partial_train},
        "dev_sha256": sha256(args.dev),
        "train_sentences": len(train_sentences),
        "partial_train_sentences": len(partial_sentences),
        "dev_sentences": len(dev_sentences),
        "train_chunks": len(train_features),
        "dev_chunks": len(dev_features),
        "train_has_ign": any("IGN" in sentence["tags"] for sentence in train_sentences),
        "best_checkpoint": trainer.state.best_model_checkpoint,
        "resumed_from_checkpoint": str(resume_checkpoint) if resume_checkpoint else None,
        "best_dev_macro_f1": trainer.state.best_metric,
        "train_metrics": train_result.metrics,
        "dev_metrics": dev_metrics,
        "hyperparameters": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "test_accessed": False,
    }
    (args.output_dir / "training_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "best_model": str(best_model),
        "best_dev_macro_f1": trainer.state.best_metric,
        "resumed_from_checkpoint": str(resume_checkpoint) if resume_checkpoint else None,
        "test_accessed": False,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
