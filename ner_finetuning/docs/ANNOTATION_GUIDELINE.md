# Hướng dẫn gán nhãn NER y khoa tiếng Việt

## Schema

| Nhãn | Định nghĩa vận hành |
|---|---|
| `DISEASE` | Tên bệnh, hội chứng, rối loạn hoặc tình trạng bệnh lý đã được định danh. |
| `SYMPTOM` | Biểu hiện chủ quan, dấu hiệu lâm sàng hoặc bất thường quan sát được. |
| `CAUSE` | Tác nhân, yếu tố nguy cơ, phơi nhiễm hoặc cơ chế được câu khẳng định là nguyên nhân hay yếu tố góp phần gây bệnh. |
| `DIAGNOSTIC_PROCEDURE` | Xét nghiệm, kỹ thuật hình ảnh, thủ thuật hoặc phương pháp dùng để phát hiện, xác nhận hay theo dõi bệnh. |
| `TREATMENT` | Thuốc, phẫu thuật, thủ thuật điều trị, liệu pháp hoặc chăm sóc dùng để điều trị hay kiểm soát bệnh. |

Schema là NER phẳng và ưu tiên vai trò của cụm từ trong câu. Mỗi span chỉ nhận một nhãn.

## Quy tắc biên span

1. Lấy nguyên văn, không sửa chính tả hoặc chuẩn hóa trong trường `text`.
2. Lấy span ngắn nhất còn đủ ý nghĩa: `chụp X-quang ngực`, không lấy `bác sĩ chỉ định chụp X-quang ngực`.
3. Giữ thành phần phân biệt bệnh: `viêm ruột thừa cấp`, không rút còn `viêm`.
4. Không lấy từ dẫn như `triệu chứng`, `phương pháp`, trừ khi là phần cố định của tên.
5. Tách các thực thể độc lập nối bằng `và`, `hoặc` hoặc dấu phẩy.
6. Không gán lồng nhau. Khi hai nhãn chồng lấn, chọn vai trò chính trong câu.
7. Vẫn gán nhãn cho thực thể phủ định, nghi ngờ hoặc giả định.
8. Giữ mọi lần xuất hiện lặp lại và sắp theo thứ tự trái sang phải.

## Quy tắc phân biệt

| Tình huống | Quyết định |
|---|---|
| `tiểu đường` là chẩn đoán | `DISEASE` |
| `tiểu đường làm tăng nguy cơ suy thận` | `CAUSE` nếu câu nhấn mạnh vai trò căn nguyên; phải áp dụng nhất quán |
| `sốt` trong `bệnh nhân sốt 39°C` | `SYMPTOM` |
| `sốt xuất huyết`, `sốt rét` | Toàn cụm là `DISEASE` |
| Tác nhân trong `virus dengue gây sốt xuất huyết` | `virus dengue` là `CAUSE` |
| Vi sinh vật chỉ xuất hiện trong kết quả xét nghiệm | Chỉ là `CAUSE` nếu ngữ cảnh khẳng định quan hệ căn nguyên |
| `nội soi` để khảo sát | `DIAGNOSTIC_PROCEDURE` |
| `nội soi can thiệp cầm máu` | `TREATMENT` nếu mục đích chính là điều trị |
| `PCR dương tính` | `PCR` là `DIAGNOSTIC_PROCEDURE`; `dương tính` là `O` |
| Thuốc được kê để chữa bệnh | `TREATMENT` |
| Thuốc được nêu là nguyên nhân tác dụng phụ | `CAUSE` |
| Bệnh A được khẳng định gây bệnh B | A là `CAUSE`, B là `DISEASE` |

## Xử lý `Symptom_and_Disease` của VietBioNER

Áp dụng theo thứ tự: từ điển đã kiểm duyệt, mẫu ngữ cảnh, dấu hiệu từ vựng, mô hình hỗ trợ, rồi duyệt thủ công. Không tự ép trường hợp không chắc chắn vào gold. Pipeline ghi các trường hợp có độ tin cậy thấp vào `processed/review/vietbioner_mapping_review.jsonl`.

Người duyệt điền `final_label` bằng `DISEASE` hoặc `SYMPTOM` và đặt `approved` thành `true`. Một vòng duyệt thứ hai cần giải quyết bất đồng trước khi dùng dữ liệu làm test gold.

## Kiểm soát chất lượng

- Kiểm tra `input_text[start:end] == text` cho mọi entity.
- Hai annotator gán độc lập một tập đại diện trước khi mở rộng.
- Theo dõi agreement riêng cho `DISEASE/SYMPTOM` và `DISEASE/CAUSE`.
- Không dùng test để xây từ điển, sửa prompt hoặc chọn ngưỡng.
- Không coi `O` là âm tính cho nhãn mà corpus nguồn không chú thích.
