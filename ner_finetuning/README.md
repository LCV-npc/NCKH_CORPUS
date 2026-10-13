# Pipeline NER y khoa Việt Nam cho Gemini

Pipeline Gold mới dùng ViMedNER làm nguồn chuẩn năm nhãn và chỉ lấy
`DiagnosticProcedure` từ BRAT của VietBioNER được mô tả tại
[`docs/MEDICAL_GOLD_PIPELINE.md`](docs/MEDICAL_GOLD_PIPELINE.md). Pipeline này
dùng nhãn chuẩn `DIAGNOSTIC`; evaluator vẫn nhận alias cũ
`DIAGNOSTIC_PROCEDURE` để tương thích prediction đã có.

Vòng lặp học qua lỗi trên Dev được chạy bằng
`scripts/iterative_error_correction.py`; đây là prompt engineering có checkpoint,
không cập nhật trọng số mô hình.

Quy trình so sánh ViMedNER-only với ViMedNER + VietBioNER DIAGNOSTIC, cùng báo
cáo lỗi đã sửa/lỗi còn lại/lỗi mới, nằm tại
[`docs/TWO_SOURCE_ERROR_DRIVEN_RUNBOOK.md`](docs/TWO_SOURCE_ERROR_DRIVEN_RUNBOOK.md).

Thư mục này chuẩn hóa VietBioNER và ViMedNer về năm nhãn `DISEASE`, `SYMPTOM`, `CAUSE`, `DIAGNOSTIC_PROCEDURE`, `TREATMENT`; tạo JSONL cho Vertex AI Supervised Fine-Tuning; và đánh giá exact/relaxed entity F1.

## Cấu trúc

```text
ner_finetuning/
  config/label_mapping.json
  docs/ANNOTATION_GUIDELINE.md
  docs/EVALUATION_PROTOCOL.md
  prompts/system_prompt_vi.txt
  raw/VietBioNER/
  raw/ViMedNer/
  scripts/prepare_gemini_data.py
  scripts/evaluate_ner.py
  tests/test_pipeline.py
```

## 1. Chuẩn bị dữ liệu

Hai repository nguồn đã được đặt trong `raw/`. Chạy:

```powershell
python ner_finetuning/scripts/prepare_gemini_data.py
```

Nếu clone project ở máy khác, tải lại nguồn trước:

```powershell
git clone --depth 1 https://github.com/ptpuyen1511/VietBioNER.git ner_finetuning/raw/VietBioNER
git clone --depth 1 https://github.com/tdtrinh11/ViMedNer.git ner_finetuning/raw/ViMedNer
```

Các đầu ra chính:

- `processed/safe/*.jsonl`: JSONL Vertex AI, chỉ lấy các ví dụ có coverage đủ năm nhãn và không bị audit cảnh báo.
- `processed/safe/*.internal.jsonl`: cùng dữ liệu nhưng giữ `id`, offset và metadata để đánh giá.
- `processed/combined_provisional/*.jsonl`: trộn cả hai corpus; chưa được xem là gold trước khi duyệt VietBioNER.
- `processed/review/vietbioner_mapping_review.jsonl`: hàng đợi duyệt `DISEASE/SYMPTOM`.
- `processed/reports/dataset_stats.json`: số câu và entity theo nguồn, split và nhãn.
- `processed/reports/conversion_issues.txt`: lỗi BIO/IO phát hiện trong lúc chuyển đổi.

Thêm `--include-optional` nếu cần giữ `LOCATION`, `DATETIME`, `ORGANISATION`. Không dùng đầu ra 8 nhãn để train trước khi bổ sung ba nhãn này cho ViMedNer.

## 2. Fine-tune trên Vertex AI

Upload `processed/safe/train.jsonl` và `processed/safe/dev.jsonl` lên Cloud Storage. Dữ liệu dùng schema `systemInstruction` và `contents` theo định dạng tuning hiện hành của Vertex AI.

Giữ `processed/safe/test.internal.jsonl` hoàn toàn ngoài quá trình train, chọn prompt, chọn checkpoint và xây từ điển.

## 3. Chạy bốn hệ thống

Mỗi hệ thống ghi prediction JSONL có cùng `id` với gold. Mỗi dòng có thể dùng một trong hai dạng:

```json
{"id":"vimedner:test:000001","entities":[{"text":"viêm phổi","label":"DISEASE","start":10,"end":20}]}
```

hoặc:

```json
{"id":"vimedner:test:000001","input_text":"...","output_text":"{\"entities\":[...]}"}
```

Nếu không có offset, evaluator căn các cụm lặp theo thứ tự trái sang phải. Với công bố nghiên cứu, nên lưu `start/end` trực tiếp.

Runner có thể gọi cả bốn cấu hình và tự tiếp tục từ file prediction đã chạy dở:

```powershell
$env:GEMINI_API_KEY="..."
python ner_finetuning/scripts/run_four_systems.py `
  --provider gemini-api `
  --base-model gemini-flash-lite-latest `
  --tuned-model YOUR_TUNED_MODEL_ID
```

Với tuned endpoint trên Vertex AI:

```powershell
$env:GOOGLE_OAUTH_ACCESS_TOKEN="ACCESS_TOKEN_FROM_GCLOUD"
python ner_finetuning/scripts/run_four_systems.py `
  --provider vertex `
  --project YOUR_PROJECT_ID `
  --location us-central1 `
  --base-model YOUR_BASE_MODEL_ID `
  --tuned-model projects/YOUR_PROJECT_ID/locations/us-central1/endpoints/YOUR_ENDPOINT_ID
```

Dùng `--limit 10` để chạy smoke test trước khi gọi toàn bộ test set. Script không ghi API key hoặc access token vào prediction/log.

Có thể chạy riêng hai baseline trước khi tuned model sẵn sàng:

```powershell
python ner_finetuning/scripts/run_four_systems.py `
  --systems prompt_only,prompt_dictionary `
  --limit 10
```

## 4. Đánh giá

```powershell
python ner_finetuning/scripts/evaluate_ner.py `
  --gold ner_finetuning/processed/safe/test.internal.jsonl `
  --system prompt_only=predictions/prompt_only.jsonl `
  --system prompt_dictionary=predictions/prompt_dictionary.jsonl `
  --system prompt_tuned=predictions/prompt_tuned.jsonl `
  --system prompt_tuned_dictionary=predictions/prompt_tuned_dictionary.jsonl `
  --output ner_finetuning/processed/reports/evaluation.json `
  --markdown ner_finetuning/processed/reports/evaluation.md
```

Evaluator báo per-label, macro và micro P/R/F1 cho exact match và relaxed match (`IoU >= 0.5`).

## Giới hạn cần duyệt

VietBioNER gộp bệnh và triệu chứng, đồng thời không chú thích `CAUSE` và `TREATMENT`. Vì vậy `combined_provisional` chỉ là dữ liệu hỗ trợ duyệt. ViMedNer có đủ năm nhãn nhưng một số span dạng mệnh đề dài; pipeline loại những câu này khỏi bộ `safe` và ghi cờ trong dữ liệu nội bộ.

VietBioNER công bố CC BY 4.0. Repository ViMedNer không có giấy phép dữ liệu rõ ràng ở thư mục gốc tại thời điểm tải; cần xin phép hoặc xác minh quyền sử dụng trước khi phân phối lại corpus hay model đã tuning.

## 5. Supervised PhoBERT/XLM-R

Pipeline supervised có căn span sang BIO/subword, chia câu dài mà không cắt ngang entity, weighted loss, CRF tùy chọn, augmentation synonym chỉ từ Train, early stopping theo Dev macro-F1 và chốt Test riêng. Xem `docs/SUPERVISED_BENCHMARK_RUNBOOK.md`.

Repository hiện chưa có checkpoint PhoBERT/XLM-R đã train, vì vậy không được báo cáo số supervised cho đến khi hoàn tất huấn luyện và frozen-Test evaluation.

## 6. CLI trực tiếp cho dữ liệu BIO

Ba CLI dưới đây đọc trực tiếp `TOKEN TAG`, hỗ trợ `IGN -> -100`, exact entity
metric bằng `seqeval` strict IOB2 và relaxed overlap một-một:

```powershell
python ner_finetuning/scripts/train_bio_xlmr.py `
  --train ner_finetuning/processed/medical_gold_v1/train.txt `
  --partial-train ner_finetuning/processed/medical_gold_v1/diagnostic_supplement.partial.txt `
  --dev ner_finetuning/processed/medical_gold_v1/dev.txt `
  --output-dir ner_finetuning/models/xlmr-vimed-vietbio-v1 `
  --learning-rate 2e-5 --batch-size 4 --gradient-accumulation 4 `
  --max-length 256 --epochs 10 --seed 42 --fp16

python ner_finetuning/scripts/evaluate_bio_checkpoint.py `
  --checkpoint ner_finetuning/models/xlmr-vimed-vietbio-v1 `
  --dev ner_finetuning/processed/medical_gold_v1/dev.txt `
  --output ner_finetuning/evaluation_runs/xlmr-vimed-vietbio-v1-dev.json

python ner_finetuning/scripts/compare_bio_checkpoints.py `
  --checkpoint-a ner_finetuning/models/xlmr-vimed-only-v1 `
  --checkpoint-b ner_finetuning/models/xlmr-vimed-vietbio-v1 `
  --dev ner_finetuning/processed/medical_gold_v1/dev.txt `
  --output ner_finetuning/evaluation_runs/bio-checkpoint-comparison.json
```

Nếu runtime bị ngắt sau khi đã lưu ít nhất một checkpoint, chạy lại đúng lệnh
train với thêm `--resume`. Script tìm checkpoint có số bước lớn nhất trong
`<output-dir>/checkpoints/checkpoint-*` và khôi phục model, optimizer cùng
learning-rate scheduler. Khi không có `--resume`, thư mục output cũ vẫn được
bảo vệ và không bị ghi đè.
