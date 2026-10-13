# Pipeline Gold ViMedNER + VietBioNER Diagnostic

## Nguyên tắc dữ liệu

Schema chuẩn gồm `DISEASE`, `SYMPTOM`, `CAUSE`, `DIAGNOSTIC`, `TREATMENT`.

- ViMedNER là nguồn Gold đầy đủ năm nhãn.
- Test chính thức của ViMedNER được giữ nguyên và không dùng để chọn prompt, từ điển hoặc checkpoint.
- `train + dev` của ViMedNER tạo tập `ann_train` đầy đủ.
- VietBioNER chỉ lấy `DiagnosticProcedure -> DIAGNOSTIC` từ `data_brat/Annotator_A`.
- `Annotator_B` và `dup_*` bị loại để tránh nhân đôi cùng văn bản.
- Span BRAT rời đoạn hoặc sai offset bị loại khỏi chuyển đổi tự động và được ghi vào audit log.
- VietBioNER sau khi lọc là partial annotation. Các token ngoài `DIAGNOSTIC` mang nhãn `IGN`, không phải `O`.
- VietBioNER không xuất hiện trong test vì dùng nó làm test năm nhãn sẽ tạo false negative giả cho bốn nhãn bị bỏ.

## 1. Dựng Gold

```powershell
python ner_finetuning/scripts/build_medical_gold.py
```

Đầu ra tại `ner_finetuning/processed/medical_gold_v1/`:

| File | Mục đích |
|---|---|
| `train.txt`, `dev.txt`, `test.txt` | BIO đầy đủ, dùng trực tiếp với mã ViMedNER |
| `ann_train.complete.txt` | Train + dev ViMedNER, đầy đủ năm nhãn |
| `diagnostic_supplement.partial.txt` | VietBioNER, `IGN` ngoài span DIAGNOSTIC |
| `*.internal.jsonl` | Span offset chuẩn để chạy LLM và evaluator |
| `conversion_issues.jsonl` | Span rời đoạn, sai offset và lỗi nguồn |
| `duplicates_removed.jsonl` | Văn bản trùng bị loại |
| `manifest.json` | Số liệu, checksum và chính sách split |

Không nối thẳng `diagnostic_supplement.partial.txt` vào bộ BIO rồi đổi `IGN` thành `O`. Muốn dùng supplement khi fine-tune, data collator phải đổi `IGN` thành loss label `-100`.

## 2. Fine-tune PhoBERT/XLM-R

Ví dụ giữ nguyên baseline ViMedNER và chỉ train trên annotation đầy đủ:

```powershell
python ViMedNer/run_ner.py `
  --data_dir ner_finetuning/processed/medical_gold_v1 `
  --model_name_or_path vinai/phobert-base `
  --labels ner_finetuning/processed/medical_gold_v1/labels.txt `
  --output_dir ner_finetuning/models/phobert-medical-v1 `
  --max_seq_length 128 `
  --num_train_epochs 30 `
  --per_device_train_batch_size 16 `
  --per_device_eval_batch_size 32 `
  --seed 10 `
  --do_train true `
  --do_eval true `
  --do_predict true `
  --overwrite_output_dir
```

Chuyển prediction BIO của mô hình sang JSONL span-offset:

```powershell
python ner_finetuning/scripts/bio_predictions_to_jsonl.py `
  --predictions ner_finetuning/models/phobert-medical-v1/test_predictions.txt `
  --gold ner_finetuning/processed/medical_gold_v1/test.internal.jsonl `
  --output ner_finetuning/predictions/fine_tuned.jsonl
```

## 3. Chạy ba cấu hình

Thiết lập `GEMINI_API_KEY`, sau đó chạy. Cấu hình fine-tuned được tạo độc lập ở bước 2 và đưa vào runner bằng file prediction.

```powershell
$env:GEMINI_API_KEY="YOUR_KEY"
python ner_finetuning/scripts/run_medical_three_systems.py `
  --fine-tuned-predictions ner_finetuning/predictions/fine_tuned.jsonl `
  --run-id baseline-v1 `
  --note "Baseline khóa prompt và dictionary"
```

Ba output độc lập:

1. `prompt_only.jsonl`: Gemini chỉ dùng prompt, không từ điển và không fine-tune.
2. `prompt_dictionary.jsonl`: Gemini + prompt + từ điển, không fine-tune.
3. `fine_tuned.jsonl`: PhoBERT/XLM-R đã train độc lập.
4. `prompt_dictionary_gold.jsonl`: Gemini + prompt + từ điển + few-shot cố định từ `ann_train`.

Runner hỗ trợ resume cho hai cấu hình Gemini bằng `id`. Mỗi run là thư mục bất biến chứa prompt, few-shot IDs, model, checksum từ điển và ghi chú.

Nên chạy smoke test trước:

```powershell
python ner_finetuning/scripts/run_medical_three_systems.py `
  --systems prompt_dictionary,prompt_dictionary_gold `
  --input ner_finetuning/processed/medical_gold_v1/dev.complete.internal.jsonl `
  --limit 10 `
  --run-id smoke-10-v1
```

## 4. Đánh giá và phân tích lỗi

```powershell
python ner_finetuning/scripts/evaluate_medical_three_systems.py `
  --system prompt_dictionary=ner_finetuning/experiment_runs/baseline-v1/prompt_dictionary.jsonl `
  --system fine_tuned=ner_finetuning/experiment_runs/baseline-v1/fine_tuned.jsonl `
  --system prompt_dictionary_gold=ner_finetuning/experiment_runs/baseline-v1/prompt_dictionary_gold.jsonl `
  --run-id baseline-v1 `
  --note "Đánh giá baseline trên test khóa"
```

Evaluator xuất:

- Exact: cùng `start`, `end`, `label`.
- Relaxed: cùng label và `IoU >= 0.5`.
- Precision, Recall, F1 theo từng nhãn.
- Micro và macro P/R/F1.
- `FP`, `FN`, `LABEL_ERROR`, `BOUNDARY_ERROR`, `NON_VERBATIM`, `DUPLICATE`, `INVALID_JSON`.
- `metrics.json`, `report.md`, `errors.jsonl`, `errors.csv` và metric riêng từng hệ thống.

## 5. Quy trình checkpoint

```text
ner_finetuning/
  experiment_runs/<run_id>/       # prediction và cấu hình inference
  evaluation_runs/<run_id>/       # metric và error analysis
  models/<model_id>/               # checkpoint fine-tuned
```

Mỗi thay đổi prompt, từ điển, regex hoặc checkpoint phải có `run_id` mới và ghi `--note`. Chỉ tinh chỉnh trên `dev`; không xem lỗi test để sửa hệ thống. Khi cấu hình được khóa, chạy test một lần để lấy số liệu báo cáo.

## 6. Prompt V2 hai bước

Chạy Extractor rồi Verifier trên đúng 100 câu Dev:

```powershell
python ner_finetuning/scripts/run_prompt_v2_two_stage.py `
  --limit 100 `
  --run-id dev-prompt-v2-100-v1
```

So sánh với checkpoint Prompt V1 trên cùng 100 câu:

```powershell
python ner_finetuning/scripts/compare_prompt_v1_v2.py
```

Các entity bị Verifier loại được lưu tại
`experiment_runs/dev-prompt-v2-100-v1/verifier_removed.jsonl` để kiểm tra thủ công.

## 7. Vòng lặp error-driven

Ba thành phần độc lập:

```text
scripts/generate_dev_error_log.py
scripts/build_error_fewshot_prompt.py
scripts/iterative_error_correction.py
```

Chạy tối đa 5 vòng trên cùng 100 câu Dev:

```powershell
python ner_finetuning/scripts/iterative_error_correction.py `
  --limit 100 `
  --max-rounds 5 `
  --target-precision 0.70 `
  --max-examples 20 `
  --run-id error-driven-dev100-v1
```

Mỗi vòng tạo:

```text
error_driven_runs/<run_id>/
  prompts/prompt_v2.txt, prompt_v3.txt, ...
  errors/dev_errors_round1.jsonl, ...
  metrics/metrics_round1.json, ...
  rounds/round_01/predictions.jsonl, ...
  iteration_history.json
```

Điều kiện dừng dùng Exact Precision: đạt mục tiêu, tổng case lỗi không còn
giảm, hoặc hết số vòng. Prompt kế tiếp được dựng lại từ prompt gốc và lỗi của
các vòng gần nhất, không nối vô hạn toàn bộ lịch sử. Script từ chối mọi đường
dẫn có `test` và kiểm tra ID/metadata phải thuộc Dev.

Có thể chạy riêng từng bước:

```powershell
python ner_finetuning/scripts/generate_dev_error_log.py `
  --predictions path/to/predictions.jsonl `
  --round 1 --limit 100 `
  --output errors/dev_errors_round1.jsonl

python ner_finetuning/scripts/build_error_fewshot_prompt.py `
  --base-prompt ner_finetuning/prompts/extractor_v2_vi.txt `
  --errors errors/dev_errors_round1.jsonl `
  --output prompts/prompt_v3.txt
```

### Cảnh báo phương pháp

Error-driven prompting tối ưu trực tiếp trên Dev nên có nguy cơ overfit guideline
và lỗi chú thích của Dev. Một số entity y khoa hợp lý có thể bị đánh dấu `nhan_du`
chỉ vì Gold không bao phủ. Luôn duyệt các ví dụ được chọn trong
`prompt_vN.selection.json` và không chạy vòng lặp này trên Test. Sau khi khóa
prompt, Test chỉ được chạy một lần để báo cáo cuối cùng.
