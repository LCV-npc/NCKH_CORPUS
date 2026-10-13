#!/usr/bin/env python3
"""Evaluate one fine-tuned XLM-R checkpoint on a complete BIO Dev file."""

from __future__ import annotations

import argparse
from pathlib import Path

from bio_xlmr_utils import (
    encode_bio_sentences,
    predictions_to_bio,
    read_bio,
    relaxed_metrics,
    seqeval_exact_metrics,
    write_json,
)
from supervised_ner_hf import (
    load_model,
    load_tokenizer,
    make_dataset_class,
    processing_keyword,
    require_dependencies,
    trainer_instance,
    training_arguments,
)
from supervised_ner_utils import ENTITY_LABELS


def resolve_checkpoint(path: Path) -> Path:
    candidate = path / "best_model"
    return candidate if candidate.is_dir() else path


def evaluate_checkpoint(checkpoint: Path, dev: Path, max_length: int, batch_size: int) -> dict:
    if "test" in dev.name.casefold():
        raise ValueError("This Dev evaluator refuses Test files")
    sentences = read_bio(dev)
    if any("IGN" in sentence["tags"] for sentence in sentences):
        raise ValueError("Dev Gold must be complete and cannot contain IGN")
    checkpoint = resolve_checkpoint(checkpoint)
    deps = require_dependencies()
    tokenizer = load_tokenizer(deps, str(checkpoint))
    config = deps["AutoConfig"].from_pretrained(str(checkpoint))
    use_crf = bool(getattr(config, "use_crf", False))
    model = load_model(deps, str(checkpoint), use_crf=use_crf, initialize_from_encoder=False)
    features, metadata = encode_bio_sentences(sentences, tokenizer, max_length)
    Dataset = make_dataset_class(deps["torch"])
    hf_args = training_arguments(
        deps,
        output_dir=".bio_eval_tmp",
        per_device_eval_batch_size=batch_size,
        report_to=[],
        do_train=False,
        do_eval=False,
    )
    trainer = trainer_instance(
        deps,
        model=model,
        args=hf_args,
        data_collator=deps["DataCollatorForTokenClassification"](tokenizer=tokenizer),
        **processing_keyword(deps, tokenizer),
    )
    prediction_output = trainer.predict(Dataset(features))
    predicted = predictions_to_bio(sentences, metadata, prediction_output.predictions)
    gold = [sentence["tags"] for sentence in sentences]
    return {
        "checkpoint": str(checkpoint.resolve()),
        "dev": str(dev.resolve()),
        "sentences": len(sentences),
        "exact": seqeval_exact_metrics(gold, predicted),
        "relaxed": relaxed_metrics(gold, predicted),
        "test_accessed": False,
    }


def pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def report(metrics: dict) -> str:
    exact = metrics["exact"]
    relaxed = metrics["relaxed"]
    lines = [
        "| Match | Precision | Recall | Micro-F1 | Macro-F1 |",
        "|---|---:|---:|---:|---:|",
        f"| Exact (seqeval strict IOB2) | {pct(exact['micro']['precision'])} | {pct(exact['micro']['recall'])} | {pct(exact['micro']['f1'])} | {pct(exact['macro_f1'])} |",
        f"| Relaxed overlap | {pct(relaxed['micro']['precision'])} | {pct(relaxed['micro']['recall'])} | {pct(relaxed['micro']['f1'])} | {pct(relaxed['macro_f1'])} |",
        "",
        "| Label | Exact P | Exact R | Exact F1 | Relaxed F1 | TP | FP | FN |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label in ENTITY_LABELS:
        value = exact["per_label"][label]
        relaxed_value = relaxed["per_label"][label]
        marker = " **" if label in {"CAUSE", "DIAGNOSTIC"} else ""
        lines.append(
            f"| {label}{marker} | {pct(value['precision'])} | {pct(value['recall'])} | {pct(value['f1'])} | "
            f"{pct(relaxed_value['f1'])} | {value['tp']} | {value['fp']} | {value['fn']} |"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=Path("xlmr-vimed-only-v1"))
    parser.add_argument("--dev", type=Path, default=Path("dev.txt"))
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    metrics = evaluate_checkpoint(args.checkpoint, args.dev, args.max_length, args.batch_size)
    print(report(metrics))
    if args.output:
        write_json(args.output, metrics)
        args.output.with_suffix(".md").write_text(report(metrics) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
