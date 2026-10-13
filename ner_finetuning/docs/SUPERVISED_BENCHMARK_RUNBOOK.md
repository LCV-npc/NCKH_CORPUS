# Supervised NER benchmark runbook

## Protocol

- Train: `processed/medical_gold_v1/train.complete.internal.jsonl`.
- Partial Train supplement: `diagnostic_supplement.partial.internal.jsonl`; only annotated `DIAGNOSTIC` tokens contribute to loss, all other tokens are masked with `-100`.
- Dev: `dev.complete.internal.jsonl`, used for early stopping and model selection.
- Test: `test.internal.jsonl`, frozen until every hyperparameter and prompt is locked.
- Best checkpoint is selected by exact entity `macro_f1` on Dev.
- Final reporting uses exact entity span + label matching. Relaxed IoU is diagnostic only.

The current Windows environment has Python 3.13, no PyTorch, and a 2 GB Quadro M620. Use Python 3.10/3.11 and preferably a CUDA GPU with at least 12-16 GB for base models. XLM-R-large normally needs substantially more memory or a managed GPU runtime.

## Environment

```powershell
py -3.11 -m venv .venv-ner
.venv-ner\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r ner_finetuning/requirements-supervised.txt
```

Install the CUDA-specific PyTorch wheel recommended for the training machine before the requirements file when applicable.

## Dev experiments

Start with four controlled experiments. Do not use Test to choose between them.

### PhoBERT baseline

```powershell
python ner_finetuning/scripts/train.py `
  --model-name vinai/phobert-base `
  --output-dir ner_finetuning/models/phobert-base-v1 `
  --batch-size 8 --gradient-accumulation 2 --epochs 10 --fp16
```

### XLM-R baseline

```powershell
python ner_finetuning/scripts/train.py `
  --model-name FacebookAI/xlm-roberta-base `
  --output-dir ner_finetuning/models/xlmr-base-v1 `
  --batch-size 8 --gradient-accumulation 2 --epochs 10 --fp16
```

### Weighted loss

```powershell
python ner_finetuning/scripts/train.py `
  --model-name FacebookAI/xlm-roberta-base `
  --output-dir ner_finetuning/models/xlmr-base-weighted-v1 `
  --class-weighted --outside-weight 0.15 `
  --batch-size 8 --gradient-accumulation 2 --epochs 10 --fp16
```

### CRF and weighted loss

```powershell
python ner_finetuning/scripts/train.py `
  --model-name FacebookAI/xlm-roberta-base `
  --output-dir ner_finetuning/models/xlmr-base-crf-weighted-v1 `
  --use-crf --class-weighted --outside-weight 0.15 `
  --batch-size 8 --gradient-accumulation 2 --epochs 10 --fp16
```

For curated Train-only augmentation, add:

```powershell
--augmentation-map ner_finetuning/config/medical_synonyms.example.json `
--augmentation-labels CAUSE,DIAGNOSTIC
```

Review and expand that mapping manually. Do not use Dev/Test entities to build it.

## Evaluate Dev

```powershell
python ner_finetuning/scripts/evaluate_finetuned.py `
  --checkpoint ner_finetuning/models/xlmr-base-crf-weighted-v1/best_model `
  --gold ner_finetuning/processed/medical_gold_v1/dev.complete.internal.jsonl `
  --split dev `
  --output-dir ner_finetuning/evaluation_runs/xlmr-base-crf-weighted-dev-v1
```

Select the model using Dev macro-F1, then record and lock model name, seed, prompt, dictionary, augmentation map, label mapping and all hyperparameters.

## Final frozen Test

Run this only after selection is complete:

```powershell
python ner_finetuning/scripts/evaluate_finetuned.py `
  --checkpoint ner_finetuning/models/BEST_MODEL/best_model `
  --gold ner_finetuning/processed/medical_gold_v1/test.internal.jsonl `
  --split test --final-test `
  --output-dir ner_finetuning/evaluation_runs/BEST_MODEL-test-final
```

Run the locked Gemini pipeline on the same complete Test split, then generate the paper table:

```powershell
python ner_finetuning/scripts/build_benchmark_comparison.py `
  --supervised-metrics ner_finetuning/evaluation_runs/BEST_MODEL-test-final/metrics.json `
  --supervised-name "Our XLM-R/CRF" `
  --gemini-metrics ner_finetuning/evaluation_runs/GEMINI-test-final/metrics.json `
  --gemini-system prompt_dictionary_gold `
  --gemini-name "Our Gemini Extractor-Verifier" `
  --output ner_finetuning/reports/final_benchmark.md
```

Published ViMedNER and VietBioNER values come from different original test sets and, for VietBioNER, a different label schema. They are useful reference rows, but only local systems evaluated on the same frozen local Test constitute a direct comparison.
