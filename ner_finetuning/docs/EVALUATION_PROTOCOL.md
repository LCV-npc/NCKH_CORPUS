# Quy trình đánh giá bốn hệ thống NER

## Thiết kế 2 x 2

| Tên hệ thống | Fine-tune | Từ điển |
|---|---:|---:|
| `prompt_only` | Không | Không |
| `prompt_dictionary` | Không | Có |
| `prompt_tuned` | Có | Không |
| `prompt_tuned_dictionary` | Có | Có |

Dùng cùng phiên bản Gemini, prompt, test set, temperature và giới hạn token. Dictionary và mọi luật xử lý xung đột phải được đóng băng trước khi chạy test.

## Exact và relaxed match

Exact TP yêu cầu cùng `start`, `end` và `label`. Với relaxed match, hai span phải cùng nhãn và có `IoU >= 0.5`, trong đó:

```text
intersection = max(0, min(pred.end, gold.end) - max(pred.start, gold.start))
union        = max(pred.end, gold.end) - min(pred.start, gold.start)
IoU          = intersection / union
```

Ghép entity một-một bằng maximum bipartite matching. Prediction không ghép được là FP; gold không ghép được là FN.

```text
Precision = TP / (TP + FP)
Recall    = TP / (TP + FN)
F1        = 2 * Precision * Recall / (Precision + Recall)
```

Per-label dùng TP/FP/FN của từng nhãn. Micro cộng TP/FP/FN của năm nhãn trước khi tính. Macro là trung bình metric của năm nhãn.

## Kỷ luật thí nghiệm

- Test set phải được harmonize đủ năm nhãn và không xuất hiện trong train/dev.
- Tách theo tài liệu, loại exact duplicate và near-duplicate giữa các nguồn.
- Chọn prompt, ngưỡng, luật từ điển và checkpoint trên dev.
- Ghi `valid JSON rate`, số entity không căn được offset, latency và chi phí.
- Nếu API dao động, chạy 3-5 lần và báo cáo trung bình cùng độ lệch chuẩn.
- Dùng paired bootstrap theo tài liệu để lập confidence interval cho chênh lệch F1.
- Báo cáo exact là kết quả chính; relaxed dùng để phân tích lỗi biên.

## Phân tích lỗi

Phân nhóm lỗi thành: bỏ sót entity, dư entity, đúng nhãn sai biên, đúng biên sai nhãn và output JSON lỗi. Báo cáo thêm kết quả theo nguồn corpus để quan sát domain shift.
