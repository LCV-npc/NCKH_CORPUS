# Quy trình xây dựng từ điển y khoa trong MedNLP Studio

## 1. Mục tiêu

Hệ thống xây dựng một bộ từ điển y khoa có phiên bản, có kiểm tra toàn vẹn và có thể dùng lại cho nhận diện thực thể bằng luật. Hiện tại phạm vi nguồn chính thức chỉ gồm hai nguồn:

1. Danh mục ICD-10 từ file Excel.
2. Phụ lục YHCT từ file PDF `PhuLuc1.pdf`.

Các thuật ngữ phát hiện từ bài báo PDF không được tự động thêm vào từ điển. Đây là chủ ý thiết kế: từ điển chỉ chứa dữ liệu đã được kiểm tra từ hai nguồn trên.

## 2. Các file tham gia

### File đầu vào

```text
data/
├── phu-lu-c-1-danh-mu-c-icd-10-thay-the-dmdc-phie-n-ba-n-6.xlsx
└── PhuLuc1.pdf
```

### Code xây dựng và nạp từ điển

```text
backend/core/dictionary_builder.py  # Đọc Excel/PDF và tạo artifact JSON
backend/core/ner_dict.py            # Kiểm tra và nạp artifact vào bộ nhớ
backend/core/ner_engine.py          # Tìm thuật ngữ trong văn bản bằng dictionary
backend/api/routes.py               # API xem trạng thái và dữ liệu dictionary
```

### File đầu ra

```text
backend/core/Tu Dien Y Hoc/
├── icd10_v1.json
├── yhct_v1.json
└── manifest_v1.json
```

`manifest_v1.json` là file điều phối. Nó ghi phiên bản, danh sách artifact, số lượng bản ghi và SHA-256 của từng artifact.

## 3. Luồng tổng quát

```text
Excel ICD-10 ───────┐
                    ├─> dictionary_builder.py ─> icd10_v1.json ─┐
PDF Phụ lục YHCT ───┘                                           ├─> manifest_v1.json
                                                               └─> ner_dict.py
                                                                    │
                                                                    ├─> term_dict trong RAM
                                                                    └─> ner_engine.py nhận diện văn bản
```

Có thể chạy lại quá trình tạo artifact bằng:

```bash
cd backend
python -m core.dictionary_builder
```

Lệnh này không sửa file Excel hoặc PDF gốc. Nó đọc nguồn, kiểm tra dữ liệu rồi ghi JSON đầu ra theo cách atomic để tránh tạo file dở dang.

## 4. Xử lý file Excel ICD-10

### 4.1. Đọc sheet và xác định dòng tiêu đề

Trong `build_icd10()` của `backend/core/dictionary_builder.py`, hệ thống dùng pandas:

```python
frame = pd.read_excel(
    source,
    sheet_name="ICD10",
    header=2,
    dtype=str,
).fillna("")
```

Ý nghĩa:

- Chỉ đọc sheet tên `ICD10`.
- `header=2` nghĩa là dòng tiêu đề nằm ở dòng thứ ba của Excel.
- `dtype=str` giữ mã bệnh dưới dạng chuỗi, tránh Excel biến mã thành số hoặc làm mất định dạng.
- Ô trống được đổi thành chuỗi rỗng.

Sau đó `_clean()` chuẩn hóa tên cột và giá trị: loại bỏ khoảng trắng thừa, gom nhiều khoảng trắng thành một và chuẩn hóa Unicode NFC.

### 4.2. Kiểm tra schema đầu vào

Code bắt buộc Excel phải có các cột:

```text
STT CHƯƠNG
MÃ CHƯƠNG
TÊN CHƯƠNG
MÃ NHÓM CHÍNH
TÊN NHÓM CHÍNH
MÃ BỆNH
MÃ BỆNH KHÔNG DẤU
DISEASE NAME
TÊN BỆNH
GHI CHÚ
NGÀY CẬP NHẬT
```

Nếu thiếu cột, hàm dừng ngay bằng `ValueError`. Đây là kiểm tra cấu trúc, giúp tránh việc tạo từ điển sai do file Excel đổi tên cột hoặc đọc nhầm sheet.

### 4.3. Tạo một entry cho từng dòng

Với mỗi dòng, code lấy ba trường chính:

```python
display_code = _clean(row["MÃ BỆNH"])
code = _clean(row["MÃ BỆNH KHÔNG DẤU"])
source_name = _clean(row["TÊN BỆNH"])
```

Nếu thiếu mã hoặc tên bệnh, hệ thống dừng và báo rõ số dòng lỗi. Mỗi entry được tạo với các thông tin:

- `canonical_term`: tên bệnh tiếng Việt dùng làm thuật ngữ chuẩn.
- `source_term`: tên lấy trực tiếp từ Excel.
- `code`: mã bệnh không dấu, dùng cho nhận diện và lưu mã.
- `display_code`: mã hiển thị, có thể giữ ký hiệu `†` hoặc `*`.
- `name_en`: tên tiếng Anh.
- `chapter`: chương ICD-10.
- `main_group`: nhóm bệnh chính.
- `note`: ghi chú.
- `updated_at_source`: ngày cập nhật trong nguồn.
- `source`: tên file, sheet và số dòng Excel để truy xuất ngược.

### 4.4. Phân loại và lọc dùng cho NER

Hàm `_category_for_icd()` phân loại theo ký tự đầu của mã:

- Mã bắt đầu bằng `R`: `Triệu Chứng`.
- Mã bắt đầu bằng `V`, `W`, `X`, `Y`, `Z`: đánh dấu `Không sử dụng NER`.
- Các mã còn lại: `Bệnh Lý`.

Như vậy không phải mọi dòng Excel đều được dùng để tìm entity. Các mã nguyên nhân bên ngoài, tai nạn, yếu tố môi trường hoặc yếu tố ảnh hưởng sức khỏe được giữ trong artifact để truy xuất nhưng có `active_for_ner=false`.

### 4.5. Alias ICD-10

`_safe_aliases()` tạo một số alias bảo thủ, ví dụ:

- Bỏ phần chú thích trong ngoặc nếu việc bỏ đó không làm mất nghĩa chính.
- Một số mẫu `u ác...` có thể tạo thêm alias dạng `ung thư...`.

Code không cắt tên bệnh tùy tiện tại dấu phẩy, vì cách đó dễ tạo ra thuật ngữ sai nghĩa.

### 4.6. Kiểm tra số lượng và ký hiệu

Sau khi đọc xong, code kiểm tra:

- Có đúng `12.219` dòng ICD-10.
- Có `82` mã có ký hiệu dagger.
- Có `776` mã có ký hiệu asterisk.

Nếu số liệu khác kỳ vọng, quá trình build dừng. Đây là kiểm tra hồi quy dữ liệu, không phải bước rút gọn tùy ý.

Kết quả hiện tại ghi nhận:

- `12.219` dòng nguồn Excel.
- `12.137` mã compact duy nhất, vì có một số dòng dùng lại cùng mã nhưng khác thông tin hoặc ký hiệu.
- Một phần trong đó bị loại khỏi nhận diện NER theo `active_for_ner`.

## 5. Xử lý file PDF Phụ lục YHCT

### 5.1. PDF này dùng để xây từ điển

`data/PhuLuc1.pdf` là một PDF dạng bảng, không được xử lý như bài báo thông thường. Hàm `build_yhct()` dùng `pdfplumber` để đọc các bảng trong từng trang.

```python
for page_number, page in enumerate(pdf.pages, 1):
    for table in page.extract_tables():
        ...
```

`_table_groups()` tìm mã chung có dạng `650xxxx`, bắt đầu từ `6500000` đến `6500227`. Mỗi mã chung trở thành một nhóm dữ liệu. Các dòng tiếp theo trong bảng được gom vào nhóm đó cho đến khi gặp mã chung kế tiếp.

### 5.2. Kiểm tra thứ tự mã

Code yêu cầu đủ chuỗi mã:

```text
6500000, 6500001, ..., 6500227
```

Nếu thiếu mã hoặc có mã trùng, hàm báo lỗi và không tạo từ điển. Việc kiểm tra này quan trọng vì các dòng dạng bệnh nền và các dòng thể lâm sàng phụ thuộc vào thứ tự và quan hệ cha-con.

### 5.3. Nhận diện bản ghi gốc và thể lâm sàng

Mỗi nhóm được kiểm tra xem có mã `U...` ở vùng cột tương ứng hay không.

#### Bản ghi gốc

Bản ghi gốc lấy:

- Tên bệnh theo YHHD.
- Mã ICD-10 liên kết.
- Tên bệnh theo YHCT.
- Mã YHCT dạng `U...`.

Entry có dạng khái quát:

```json
{
  "record_type": "base",
  "canonical_term": "Tên YHHD (Tên YHCT)",
  "yhhd_term": "Tên YHHD",
  "yhct_term": "Tên YHCT",
  "icd10_code": "...",
  "code": "U...",
  "active_for_ner": true
}
```

#### Bản ghi thể lâm sàng

Nếu nhóm có mã `U...` ở phần thể bệnh, nó được gắn với bản ghi gốc gần nhất thông qua `parent_id`. Các trường quan trọng gồm:

- `record_type=clinical_form`.
- `canonical_term`: tên thể bệnh.
- `parent_yhhd_term` và `parent_yhct_term`.
- `code`: mã thể bệnh.

Riêng tên `Thể khác` được giữ để truy xuất nhưng đánh dấu `ambiguous=true` và không dùng cho NER.

### 5.4. Làm sạch văn bản PDF

`_repair_cell_text()` xử lý lỗi thường gặp khi trích bảng PDF, ví dụ một ô bị cắt mất phần đóng ngoặc. Sau đó code kiểm tra tất cả trường bắt buộc của bản ghi.

Cuối cùng phải đạt:

- `228` entry YHCT.
- `38` bản ghi gốc.
- `190` thể lâm sàng.

## 6. Tạo artifact và manifest

`build_all()` gọi lần lượt:

```python
icd = build_icd10()
yhct = build_yhct()
```

Sau đó ghi:

- `icd10_v1.json`.
- `yhct_v1.json`.
- `manifest_v1.json`.

Mỗi JSON chứa phiên bản schema, phiên bản dictionary, thông tin nguồn, số lượng và danh sách entry. Manifest chỉ khai báo hai file `icd10` và `yhct`, phù hợp với yêu cầu hiện tại là không sử dụng từ điển custom.

Hàm `_atomic_json()` ghi dữ liệu vào file tạm trong cùng thư mục rồi thay thế file đích. Nhờ vậy nếu chương trình dừng giữa chừng, file artifact cũ không bị ghi đè bởi dữ liệu chưa hoàn chỉnh.

## 7. Nạp từ điển khi backend khởi động

Trong `backend/core/ner_dict.py`, lệnh cuối module gọi `load_ner_dictionary()` ngay khi module được import.

Quy trình nạp:

1. Đọc `manifest_v1.json`.
2. Mở đúng hai artifact được manifest trỏ tới.
3. Kiểm tra SHA-256 của artifact.
4. Kiểm tra phiên bản của ICD-10 và YHCT có trùng manifest.
5. Kiểm tra SHA-256 của file nguồn Excel/PDF gốc.
6. Nạp các entry ICD-10 còn `active_for_ner=true`.
7. Nạp các entry YHCT còn hoạt động.
8. Tạo chỉ mục không dấu khi không gây xung đột.
9. Lưu metadata như số thuật ngữ, phiên bản, nguồn và danh mục.

Nếu file nguồn bị sửa sau khi artifact được tạo, backend chủ động dừng với lỗi `Sai SHA-256` hoặc thông báo yêu cầu chạy lại `python -m core.dictionary_builder`. Cơ chế này ngăn backend dùng artifact không còn tương ứng với nguồn.

## 8. Chuẩn hóa và tạo chỉ mục tìm kiếm

Hàm `normalize_match_text()` trong `ner_dict.py`:

1. Chuẩn hóa Unicode NFC.
2. Chuyển về chữ thường bằng `casefold()`.
3. Tách token Unicode, bỏ dấu câu thừa.
4. Gom khoảng trắng.

Ví dụ về mặt logic:

```text
"  Viêm   Phổi. " -> "viêm phổi"
```

Offset trong văn bản gốc không bị thay đổi. Chỉ khóa tìm kiếm được chuẩn hóa, còn vị trí highlight vẫn lấy từ chuỗi nguyên bản.

Ngoài chỉ mục có dấu, hệ thống tạo chỉ mục không dấu có điều kiện. Các cụm một từ thường không được tự động bỏ dấu vì dễ gây nhầm giữa nhiều thuật ngữ. Cụm nhiều từ chỉ được dùng khi sau khi bỏ dấu vẫn ánh xạ duy nhất về cùng mã và cùng loại entity.

## 9. Nhận diện entity trong văn bản

Khi `ner_engine.py` nhận một đoạn văn, nó tìm các thuật ngữ trong `term_dict` đã nạp. Với mỗi kết quả, hệ thống trả về tối thiểu:

- đoạn text khớp.
- vị trí `start`, `end` trong văn bản gốc.
- loại entity.
- mã ICD-10 hoặc mã YHCT.
- thuật ngữ chuẩn.
- nguồn `icd10` hoặc `yhct`.
- màu hiển thị.

Các kết quả chồng lấn được xử lý theo độ dài và độ ưu tiên nguồn để tránh highlight nhiều lần cùng một đoạn. Những entry bị đánh dấu ambiguous hoặc inactive không được đưa vào kết quả NER.

Do đây là dictionary NER, hệ thống chỉ nhận diện những cụm thật sự tồn tại trong ICD-10/YHCT hoặc alias được tạo hợp lệ từ chính các entry đó. Nó không tự suy luận rằng mọi cụm có vẻ y khoa trong bài báo đều là entity.

## 10. PDF bài báo upload có vai trò gì?

Endpoint `/api/extract-pdf` dùng `ExtractorPipeline`, không dùng `dictionary_builder` để tạo thuật ngữ.

Luồng của PDF bài báo:

```text
Upload PDF
  -> kiểm tra phần mở rộng, kích thước, magic bytes và SHA-256
  -> PyMuPDF đọc block/chữ/trang
  -> loại header/footer
  -> phát hiện tiêu đề và heading
  -> chia section, abstract, body
  -> kiểm tra ngôn ngữ tiếng Việt nếu được yêu cầu
  -> tùy chọn gọi Gemini để lấy cấu trúc bài báo
  -> lưu structured_article.json, metadata.json và các file section .txt
```

Các module chính:

- `backend/pdf_extractor.py`: trích text, block, heading và section bằng PyMuPDF.
- `backend/core/pdf_pipeline.py`: điều phối pre-check, chống xử lý trùng, lưu kết quả.
- `backend/core/llm_pdf_extractor.py`: tùy chọn lấy metadata/section có cấu trúc bằng Gemini.
- `backend/api/routes.py`: endpoint upload `/api/extract-pdf`.

Luồng này phục vụ corpus và hiển thị bài báo. Nó không gọi `build_all()` và không thêm từ mới vào `icd10_v1.json` hoặc `yhct_v1.json`.

## 11. API kiểm tra từ điển

`GET /api/dictionary` đọc manifest và trả các entry từ hai nguồn ICD-10/YHCT cho giao diện quản trị.

`GET /api/dictionary/status` trả metadata của dictionary đang chạy, gồm:

- phiên bản.
- số lượng thuật ngữ đã nạp.
- số thuật ngữ không dấu.
- nguồn file và SHA-256.
- phạm vi nhãn.

Endpoint `POST /api/save-to-dictionary` hiện bị vô hiệu hóa với HTTP 410. Lý do là hệ thống không cho phép thêm thuật ngữ tùy chỉnh trực tiếp từ giao diện; muốn thay đổi từ điển phải cập nhật nguồn chính thức rồi build lại artifact.

## 12. Tóm tắt bằng một ví dụ

Giả sử Excel có dòng:

```text
MÃ BỆNH KHÔNG DẤU = J18.9
TÊN BỆNH = Viêm phổi, không xác định
```

Sau `build_icd10()`, dòng này trở thành một entry ICD-10 có mã, tên chuẩn, alias an toàn, loại `Bệnh Lý`, thông tin chương/nhóm và vị trí dòng nguồn.

Khi `ner_dict.py` nạp artifact, nó tạo khóa tìm kiếm `viêm phổi không xác định`. Khi văn bản chứa đúng cụm đó, `ner_engine.py` trả entity và vị trí highlight. Nếu văn bản chỉ chứa một cụm khác như `gây tê ngoài màng cứng` mà cụm đó không có trong hai nguồn, dictionary NER sẽ không trả kết quả.

## 13. Kết luận

Hệ thống hiện có kiến trúc tách biệt ba nhiệm vụ:

1. **Xây dựng nguồn từ điển:** đọc Excel ICD-10 và PDF Phụ lục YHCT, kiểm tra, chuẩn hóa và tạo JSON có phiên bản.
2. **Nạp và bảo vệ từ điển:** xác minh manifest, SHA-256, phiên bản rồi tạo chỉ mục tìm kiếm trong RAM.
3. **Xử lý tài liệu:** đọc PDF bài báo, tách nội dung và lưu corpus; việc này không tự động làm phát sinh thuật ngữ từ điển.

Thiết kế này giúp kết quả dictionary NER có thể truy xuất nguồn, tái lập và kiểm soát được. Đổi lại, độ bao phủ của NER phụ thuộc trực tiếp vào các thuật ngữ có trong ICD-10 và YHCT; các cụm ngoài phạm vi đó phải được bổ sung qua nguồn dữ liệu chính thức, không thêm thủ công vào code.
