#!/usr/bin/env python3
"""Run a frozen supervised checkpoint and compute exact/relaxed entity metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from evaluate_medical_three_systems import evaluate_system, markdown, write_errors
from supervised_ner_hf import (
    encode_records,
    load_model,
    load_tokenizer,
    make_dataset_class,
    processing_keyword,
    require_dependencies,
    trainer_instance,
    training_arguments,
)
from supervised_ner_utils import ID2LABEL, bio_to_entities, read_jsonl, record_to_words, write_jsonl


ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--final-test", action="store_true", help="Required acknowledgement when --split test")
    return parser.parse_args()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def predictions_to_records(rows: list[dict], metadata: list[dict], predictions) -> list[dict]:
    import numpy as np

    predicted_ids = np.argmax(predictions, axis=-1)
    full_tags: list[list[str]] = []
    full_words: list[list[dict]] = []
    for row in rows:
        words, _ = record_to_words(row)
        full_words.append(words)
        full_tags.append(["O"] * len(words))

    for chunk_index, meta in enumerate(metadata):
        seen: set[int] = set()
        for token_index, local_word_id in enumerate(meta["word_ids"]):
            if local_word_id is None or local_word_id in seen:
                continue
            seen.add(local_word_id)
            global_word_id = meta["word_start"] + local_word_id
            full_tags[meta["record_index"]][global_word_id] = ID2LABEL[int(predicted_ids[chunk_index, token_index])]

    output: list[dict] = []
    for row, words, tags in zip(rows, full_words, full_tags):
        output.append({
            "id": row["id"],
            "system": "supervised_finetuned",
            "input_text": row["input_text"],
            "entities": bio_to_entities(words, tags, row["input_text"]),
        })
    return output


def main() -> int:
    args = parse_args()
    if args.split == "test" and not args.final_test:
        raise ValueError("Test is frozen. Pass --final-test only after model and hyperparameters are locked on Dev")
    if args.split == "dev" and "test" in args.gold.name.lower():
        raise ValueError("Dev evaluation cannot use a Test file")
    if args.output_dir.exists():
        raise FileExistsError(f"Evaluation checkpoint already exists: {args.output_dir}")

    rows = read_jsonl(args.gold)
    observed_splits = {str(row.get("source_split", "")).lower() for row in rows}
    if args.split not in observed_splits:
        raise ValueError(f"Expected {args.split} records, observed source_split={sorted(observed_splits)}")

    deps = require_dependencies()
    tokenizer = load_tokenizer(deps, str(args.checkpoint))
    config = deps["AutoConfig"].from_pretrained(str(args.checkpoint))
    use_crf = bool(getattr(config, "use_crf", False))
    model = load_model(deps, str(args.checkpoint), use_crf, initialize_from_encoder=False)
    features, metadata = encode_records(rows, tokenizer, args.max_length)
    Dataset = make_dataset_class(deps["torch"])
    dataset = Dataset(features)
    args.output_dir.mkdir(parents=True)
    hf_args = training_arguments(
        deps,
        output_dir=str(args.output_dir / "hf_tmp"),
        per_device_eval_batch_size=args.batch_size,
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
    prediction_output = trainer.predict(dataset)
    prediction_rows = predictions_to_records(rows, metadata, prediction_output.predictions)
    predictions_path = args.output_dir / "predictions.jsonl"
    write_jsonl(predictions_path, prediction_rows)

    metrics, errors = evaluate_system(rows, prediction_rows, args.iou)
    (args.output_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.output_dir / "report.md").write_text(markdown({"supervised_finetuned": metrics}), encoding="utf-8")
    write_errors(args.output_dir / "errors.jsonl", errors)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "checkpoint": str(args.checkpoint.resolve()),
        "gold": str(args.gold.resolve()),
        "gold_checksum": sha256(args.gold),
        "split": args.split,
        "records": len(rows),
        "chunks": len(features),
        "use_crf": use_crf,
        "max_length": args.max_length,
        "final_test_acknowledged": args.final_test,
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    micro = metrics["exact"]["micro"]
    print(json.dumps({
        "split": args.split,
        "precision": micro["precision"],
        "recall": micro["recall"],
        "micro_f1": micro["f1"],
        "macro_f1": metrics["exact"]["macro"]["f1"],
        "output": str(args.output_dir),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
