# Two-source and error-driven experiment protocol

## What is merged

The source text is never concatenated or rewritten. Each sentence remains one record with its original source,
split, offsets and checksum.

- ViMedNER provides complete five-label annotation and remains the Gold Dev/Test benchmark.
- VietBioNER contributes only `DiagnosticProcedure -> DIAGNOSTIC` to Train.
- Unannotated positions in VietBioNER are `IGN` and receive loss `-100`; they are not treated as negative `O`.
- VietBioNER never enters the five-label ViMedNER Dev/Test sets.

This produces a controlled source ablation rather than a misleading mixed Gold test.

## Experiment A: ViMedNER only

```bash
python ner_finetuning/scripts/train.py \
  --model-name FacebookAI/xlm-roberta-base \
  --exclude-partial-train \
  --output-dir ner_finetuning/models/xlmr-vimed-only-v1 \
  --max-length 128 --learning-rate 5e-5 \
  --batch-size 4 --gradient-accumulation 8 \
  --epochs 30 --early-stopping-patience 5 \
  --gradient-checkpointing --fp16

python ner_finetuning/scripts/evaluate_finetuned.py \
  --checkpoint ner_finetuning/models/xlmr-vimed-only-v1/best_model \
  --gold ner_finetuning/processed/medical_gold_v1/dev.complete.internal.jsonl \
  --split dev \
  --output-dir ner_finetuning/evaluation_runs/xlmr-vimed-only-v1-dev
```

## Experiment B: ViMedNER plus VietBioNER diagnostic support

Omitting `--exclude-partial-train` activates the audited partial VietBioNER supplement.

```bash
python ner_finetuning/scripts/train.py \
  --model-name FacebookAI/xlm-roberta-base \
  --output-dir ner_finetuning/models/xlmr-vimed-vietbio-dia-v1 \
  --max-length 128 --learning-rate 5e-5 \
  --batch-size 4 --gradient-accumulation 8 \
  --epochs 30 --early-stopping-patience 5 \
  --gradient-checkpointing --fp16

python ner_finetuning/scripts/evaluate_finetuned.py \
  --checkpoint ner_finetuning/models/xlmr-vimed-vietbio-dia-v1/best_model \
  --gold ner_finetuning/processed/medical_gold_v1/dev.complete.internal.jsonl \
  --split dev \
  --output-dir ner_finetuning/evaluation_runs/xlmr-vimed-vietbio-dia-v1-dev
```

## Compare scores and individual errors

```bash
python ner_finetuning/scripts/compare_dev_predictions.py \
  --before ner_finetuning/evaluation_runs/xlmr-vimed-only-v1-dev/predictions.jsonl \
  --after ner_finetuning/evaluation_runs/xlmr-vimed-vietbio-dia-v1-dev/predictions.jsonl \
  --before-name "ViMedNER only" \
  --after-name "ViMedNER + VietBioNER DIAGNOSTIC" \
  --output-dir ner_finetuning/evaluation_runs/source-ablation-v1
```

Read `report.md` for score deltas and `error_transitions.json` for:

- `RESOLVED`: an old error was corrected.
- `PERSISTENT`: exactly the same error remains.
- `CHANGED`: the same Gold entity is still wrong, but its error type/span changed.
- `NEW`: the candidate introduced an error absent from the baseline.

## Prompt error-driven rounds

```bash
python ner_finetuning/scripts/iterative_error_correction.py \
  --dev ner_finetuning/processed/medical_gold_v1/dev.complete.internal.jsonl \
  --limit 100 --max-rounds 5 --target-precision 0.80 \
  --run-id prompt-error-driven-v1
```

Each round now writes `transitions_roundN_to_roundN+1.json`. These rounds tune the prompt on Dev and therefore
must not be reported as final test performance. Lock the winning prompt before one final Test run.

## Claims against the papers

- Beat ViMedNER only by evaluating the locked model on the official ViMedNER Test split and exceeding its
  XLM-R-large `Mic-F1=72.5` and `Mac-F1=64.0` under exact entity matching.
- Beat VietBioNER only by evaluating on its original Test split with its native five-label schema and exceeding
  `Precision=77.49`, `Recall=81.83`, `F1=79.60`.
- A score on a merged or remapped test set cannot be described as directly beating either paper.

Run XLM-R-large, CRF, class weighting and augmentation as separate ablations. Change one factor per run and
select only on Dev macro-F1. Never append Dev/Test mistakes directly to Train; derive rules or Train-only
augmentation from them to avoid leakage.
