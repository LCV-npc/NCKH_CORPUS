# Báo cáo chuẩn hóa dữ liệu

Ngày chạy: 2026-09-22.

## Dữ liệu nguồn

| Nguồn | Split | Số mẫu | DISEASE | SYMPTOM | CAUSE | DIAGNOSTIC | TREATMENT |
|---|---|---:|---:|---:|---:|---:|---:|
| VietBioNER | train | 706 | 818* | 20* | - | 191 | - |
| VietBioNER | dev | 300 | 369* | 9* | - | 89 | - |
| VietBioNER | test | 700 | 796* | 14* | - | 202 | - |
| ViMedNer | train | 4.573 | 5.367 | 2.291 | 845 | 861 | 1.681 |
| ViMedNer | dev | 1.524 | 1.798 | 748 | 259 | 271 | 653 |
| ViMedNer | test | 1.525 | 1.822 | 712 | 276 | 308 | 632 |

`*` Kết quả VietBioNER là phân tách heuristic từ `Symptom_and_Disease`, chưa phải gold mới. Tỷ lệ nghiêng mạnh sang `DISEASE` phản ánh miền lao/HIV và từ điển ICD, nhưng cũng cho thấy cần duyệt lại trước khi công bố.

## Bộ `safe`

`safe` hiện chỉ lấy ViMedNer vì đây là nguồn duy nhất chú thích đủ cả năm nhãn. Các câu có span dài hoặc giống mệnh đề bị audit loại ra; exact duplicate được loại với ưu tiên giữ test, rồi dev, rồi train.

| Split | Số mẫu | DISEASE | SYMPTOM | CAUSE | DIAGNOSTIC | TREATMENT |
|---|---:|---:|---:|---:|---:|---:|
| train | 4.241 | 5.175 | 2.155 | 652 | 784 | 1.489 |
| dev | 1.420 | 1.753 | 703 | 215 | 242 | 576 |
| test | 1.409 | 1.766 | 668 | 216 | 261 | 579 |

Train có 10.255 entity. Phân phối xấp xỉ: `DISEASE` 50,46%, `SYMPTOM` 21,01%, `TREATMENT` 14,52%, `DIAGNOSTIC_PROCEDURE` 7,65%, `CAUSE` 6,36%. Vì vậy báo cáo nghiên cứu phải có macro-F1 và per-label F1, không chỉ micro-F1.

## Bộ `combined_provisional`

| Split | Số mẫu | DISEASE | SYMPTOM | CAUSE | DIAGNOSTIC | TREATMENT |
|---|---:|---:|---:|---:|---:|---:|
| train | 5.140 | 6.092 | 2.310 | 845 | 1.012 | 1.668 |
| dev | 1.780 | 2.138 | 757 | 259 | 347 | 653 |
| test | 2.177 | 2.586 | 725 | 276 | 502 | 632 |

Bộ này đã trộn cả hai nguồn nhưng chưa được dùng như gold. VietBioNER không chú thích `CAUSE` và `TREATMENT`, nên các nhãn `O` của nó có thể chứa âm tính giả.

## Phát hiện chất lượng

- 179 span VietBioNER có độ tin cậy tách bệnh/triệu chứng thấp và đã được đưa vào hàng đợi duyệt.
- ViMedNer có một dòng hỏng tại `data/train.txt:14217`: token rỗng mang nhãn `B-bien_phap_dieu_tri`. Converter bỏ dòng này và ghi log.
- Bộ `safe` loại 2 mẫu train trùng với split ưu tiên cao hơn.
- Bộ kết hợp loại 139 train, 44 dev và 48 test bị trùng chính xác.
- 9 unit test đã qua; evaluator self-test đạt exact và relaxed micro/macro-F1 bằng 100%.
- Smoke test Gemini thật trên một mẫu test thành công cho cả prompt-only và prompt+từ điển; cả hai trả đúng ba entity `DISEASE` và JSON hợp lệ.

## Việc cần làm trước khi gọi là gold hợp nhất

1. Duyệt 179 span trong `processed/review/vietbioner_mapping_review.jsonl`.
2. Gán bổ sung `CAUSE` và `TREATMENT` cho các câu VietBioNER được chọn.
3. Duyệt các span dạng mệnh đề dài của ViMedNer theo annotation guideline.
4. Đóng băng test set sau khi hai annotator giải quyết bất đồng.
5. Chỉ sau đó mới chạy tuning và đánh giá bốn hệ thống để dùng số liệu trong bài báo.
