# MedNLP Corpus Review System — Backend

Backend của hệ thống thu thập và thẩm định ngữ liệu y tế tiếng Việt. Ứng dụng hiện chạy bằng FastAPI, lưu metadata/review trong MySQL và lưu một số artifact PDF/TXT/JSON trên filesystem.

## Công nghệ và luồng chính

- Python 3.11+, FastAPI, Uvicorn, Pydantic.
- MySQL cho bài báo, người dùng, phiên gán nhãn, review và adjudication.
- Pipeline crawler/PDF → làm sạch và trích xuất → NER/AI → Expert review → Reviewer adjudication → Gold corpus.
- Controlled vocabulary ICD-10/YHCT được kiểm tra checksum qua `core/ner_dict.py`.
- Rule NER dùng immutable snapshot theo từng generation; lỗi reload giữ snapshot
  cũ, còn lần nạp đầu thất bại sẽ trả `503 DICTIONARY_UNAVAILABLE` cho API annotation.

## Chạy local

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
```

Frontend Vite chạy ở cổng 5173 và proxy `/api` tới backend. Có thể cấu hình `CORS_ALLOWED_ORIGINS` nếu frontend chạy ở origin khác.

## Cấu hình

Sao chép `.env.example` thành `.env`, sau đó thay các giá trị mẫu, đặc biệt là `DB_PASSWORD`, `GEMINI_API_KEY` và thông tin SMTP. Không commit `.env` thật.

Các nhóm biến chính:

- `DB_*`: kết nối MySQL.
- `AUTH_*`: thời hạn session và giới hạn đăng nhập.
- `CORS_ALLOWED_ORIGINS`: danh sách origin được phép, phân cách bằng dấu phẩy.
- `GEMINI_*`, `PDF_LLM_*`: trích xuất PDF/AI.
- `SMTP_*`: thông báo gán phiên.
- `CORPUS_*`, `PDF_*`: thư mục và bộ lọc dữ liệu.
- `NCKH_DATA_DIR`, `NCKH_DICTIONARY_DIR`: ghi đè thư mục nguồn dữ liệu và
  artifact dictionary; đường dẫn tương đối được resolve từ `backend/`.

## Database

`mysql.txt` là schema/migration tham khảo. Khi khởi động, backend tự kiểm tra một số cột/index bổ sung để tương thích với database cũ. Trước khi chạy migration trên dữ liệu thật cần backup MySQL và kiểm tra các bản ghi review cũ.

Các bảng cốt lõi gồm `users`, `articles`, `labeling_sessions`, `labeling_session_assignments`, `expert_reviews` và `reviewer_adjudications`.

### Manual Labeling v2

Manual Labeling dùng `backend/core/Tu Dien Y Hoc/icd10_v1.json` và `yhct_v1.json` làm nguồn Concept duy nhất. Từ hai artifact này, `dictionary_builder.py` sinh `controlled_vocabulary_v1.json`, chứa các biến thể được kiểm soát như `các X`/`những X` → `X` và một số headword `Bệnh lý X` → `X` khi cụm còn lại có ít nhất hai token. Mỗi variant giữ nguyên `concept_id`; `rule_score` chỉ là điểm heuristic chưa calibration (`0.98` cho grammar variant, `0.97` cho headword variant), không phải xác suất. Runtime load file này và đưa các variant hợp lệ vào `matched_concepts`/HTML highlight, không cần Admin/Reviewer duyệt. Candidate mơ hồ hoặc fuzzy chỉ nằm trong `suggestions` với trạng thái `UNCERTAIN`, không được lưu thành nhãn y khoa.

Trước khi dùng chức năng ghi evidence trên database cũ, chạy các câu `ALTER TABLE` được chú thích trong `mysql.txt`. API discovery mặc định chỉ ghi grammar variant; fuzzy candidate chỉ chạy khi truyền `include_fuzzy_candidates=true` và vẫn là suggestion-only. Khi thay đổi nguồn ICD-10/YHCT, chạy `python -m core.dictionary_builder` để tái tạo `icd10_v1.json`, `yhct_v1.json`, `controlled_vocabulary_v1.json` và `manifest_v1.json` với checksum đồng bộ.

Có thể tạo báo cáo audit bằng:

```powershell
python scripts/audit_dictionary.py
```

Các báo cáo nằm trong `backend/reports/dictionary/`, gồm thuật ngữ một token,
surface form ambiguous và category không hợp lệ. Thuật ngữ một token không bị
block tự động; chỉ `ambiguous=true` mới bị loại khỏi exact highlight và chuyển
thành suggestion `UNCERTAIN`.

Các trường provenance quan trọng gồm `concept_id`, `match_method`, `confidence_score`, `dictionary_version`, offset gốc và evidence/context. Vì vậy có thể đối chiếu lại từng highlight với JSON và bài báo nguồn.

### Cơ sở tài liệu cho controlled grammar variant

Thiết kế `canonical term` → `surface variant` → cùng `concept_id` tham khảo các hướng sau:

- Tsuruoka, McNaught và Ananiadou (2008), *Normalizing biomedical terms by minimizing ambiguity and variability*: sinh luật chuẩn hóa và chọn luật làm giảm variability nhưng hạn chế tăng ambiguity. Hệ thống áp dụng nguyên tắc này bằng ambiguity gate, nhưng chỉ bật các luật tiếng Việt đã định trước.
- Kang et al. (2013), *Using rule-based natural language processing to improve disease normalization in biomedical text*: dùng rule-based NLP để điều chỉnh phrase/term variation, boundary và đưa phrase đã điều chỉnh trở lại bộ chuẩn hóa; hệ thống hiện áp dụng phiên bản đơn giản hơn, không dùng POS model.
- Funk, Cohen, Hunter và Verspoor (2016), *Gene Ontology synonym generation rules lead to increased performance in biomedical concept recognition*: sinh synonym bằng các luật syntactic/derivational và đo tác động lên F-measure; hệ thống chỉ dùng subset an toàn `các X`/`những X` để tránh over-generation.
- Aronson và cộng sự (2010), *An overview of MetaMap*: kiến trúc lexical lookup → variant generation → candidate mapping là cơ sở tham khảo; MetaMap/UMLS không được đưa vào runtime vì dự án chỉ cho phép ICD-10/YHCT.
- Ngo et al. (2020), *Towards a UMLS-Integratable Vietnamese Medical Terminology*: cho thấy lexical resources và corpus extraction có vai trò trong xây dựng thuật ngữ y khoa tiếng Việt; hệ thống dùng corpus nội bộ để ghi evidence nhưng không nhập UMLS bên ngoài.
- Tien et al. (2019), *Rule based English-Vietnamese bilingual terminology extraction from Vietnamese documents*: tham khảo rule-based candidate generation và thống kê cụm; MI/context chỉ dùng để xếp hạng evidence, không tự quyết định Concept ID.

Các nghiên cứu dùng embedding/semantic similarity như LexExp phù hợp cho lexicon discovery hoặc suggestion, nhưng không được dùng để tự gán nhãn y khoa cuối trong Manual Labeling.

## Kiểm tra

```powershell
cd backend
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Các kiểm thử tích hợp cần MySQL sẽ tự bỏ qua nếu môi trường chưa có database phù hợp. Với triển khai thật nên bổ sung pytest, API integration test và E2E test cho ba vai trò Admin/Expert/Reviewer.

## Lưu ý triển khai

- Không dùng CORS wildcard khi bật credentials.
- Reverse proxy cần bật HTTPS và giới hạn domain.
- Không ghi raw nội dung y khoa hoặc credential vào log.
- Job crawl/PDF/AI hiện cần được chuyển sang worker queue bền vững trước khi chạy tải lớn.
