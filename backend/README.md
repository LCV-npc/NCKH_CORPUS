# MedNLP Corpus Review System — Backend

Backend của hệ thống thu thập và thẩm định ngữ liệu y tế tiếng Việt. Ứng dụng hiện chạy bằng FastAPI, lưu metadata/review trong MySQL và lưu một số artifact PDF/TXT/JSON trên filesystem.

## Công nghệ và luồng chính

- Python 3.11+, FastAPI, Uvicorn, Pydantic.
- MySQL cho bài báo, người dùng, phiên gán nhãn, review và adjudication.
- Pipeline crawler/PDF → làm sạch và trích xuất → NER/AI → Expert review → Reviewer adjudication → Gold corpus.
- Controlled vocabulary ICD-10/YHCT được kiểm tra checksum qua `core/ner_dict.py`.

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

## Database

`mysql.txt` là schema/migration tham khảo. Khi khởi động, backend tự kiểm tra một số cột/index bổ sung để tương thích với database cũ. Trước khi chạy migration trên dữ liệu thật cần backup MySQL và kiểm tra các bản ghi review cũ.

Các bảng cốt lõi gồm `users`, `articles`, `labeling_sessions`, `labeling_session_assignments`, `expert_reviews` và `reviewer_adjudications`.

### Manual Labeling v2

Manual Labeling dùng `backend/core/Tu Dien Y Hoc/icd10_v1.json` và `yhct_v1.json` làm nguồn Concept duy nhất. `exact`, alias có sẵn trong JSON và alias đã được Admin/Reviewer duyệt mới được đưa vào `matched_concepts`/HTML highlight. Candidate mơ hồ hoặc fuzzy chỉ nằm trong `suggestions` với trạng thái `UNCERTAIN`, không được lưu thành nhãn y khoa.

Trước khi dùng trang `Alias từ điển` trên database cũ, chạy các câu `ALTER TABLE` được chú thích trong `mysql.txt`. API discovery mặc định chỉ sinh grammar variant; fuzzy candidate chỉ chạy khi truyền `include_fuzzy_candidates=true`. Khi duyệt alias, backend kiểm tra Concept ID, dictionary version và xung đột surface form rồi reload runtime dictionary.

Các trường provenance quan trọng gồm `concept_id`, `match_method`, `confidence_score`, `dictionary_version`, offset gốc và evidence/context. Vì vậy có thể đối chiếu lại từng highlight với JSON và bài báo nguồn.

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
