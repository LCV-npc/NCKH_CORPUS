# NCKH_CORPUS — Kho ngữ liệu y tế tiếng Việt

Hệ thống thu thập, chuẩn hóa, trích xuất và gán nhãn kho ngữ liệu y tế tiếng Việt. Ứng dụng hỗ trợ thu thập bài báo khoa học và hỏi đáp y khoa, tách cấu trúc bài báo từ PDF, gán nhãn thực thể bằng từ điển hoặc Gemini, đồng thời cung cấp quy trình để chuyên gia thẩm định kết quả.

Repository: <https://github.com/LCV-npc/NCKH_CORPUS>

## Chức năng chính

- Thu thập PDF bài báo từ các tạp chí y học Việt Nam theo nguồn và khoảng năm.
- Crawl hỏi đáp y khoa từ chuyên mục Tư vấn của Bệnh viện Đa khoa Tâm Anh.
- Tách PDF thành tiêu đề, tác giả, tóm tắt và các section dưới dạng TXT/JSON.
- Nhận diện thực thể y khoa bằng từ điển ICD-10, YHCT và từ điển tùy chỉnh.
- Gán nhãn văn bản bằng Gemini và lưu kết quả vào MySQL.
- Admin tạo tài khoản chuyên gia; hệ thống đăng nhập và phân quyền `admin`/`expert` ở backend.
- Chuyên gia duyệt nhãn ICD-10/AI, nhận xét và xem lịch sử review.
- Admin quản lý toàn bộ corpus, người dùng và review của chuyên gia.

## Kiến trúc hiện tại

```text
Trình duyệt
   │
   │ HTTP /api/*
   ▼
Frontend Vite (localhost:5173)
   │ proxy /api
   ▼
FastAPI (localhost:8000)
   ├── MySQL: corpus, nhãn, tài khoản, review, log
   ├── Crawler OJS: PDF bài báo
   ├── Crawler Tâm Anh: câu hỏi BN + trả lời BS
   ├── PDF extractor: TXT/JSON theo section
   ├── Dictionary NER: ICD-10/YHCT/từ điển tùy chỉnh
   └── Gemini: AI labeling và hỗ trợ tách cấu trúc PDF
```

| Thành phần | Công nghệ thực tế |
| --- | --- |
| Frontend | Vite, HTML, CSS, JavaScript trong `frontend/public` |
| Backend | Python, FastAPI, Uvicorn |
| Database | MySQL qua `mysql-connector-python` |
| PDF | PyMuPDF, pdfplumber |
| Crawl/parse HTML | requests, BeautifulSoup4 |
| AI | Google Gemini |
| Ngôn ngữ | langdetect và bộ kiểm tra tiếng Việt nội bộ |

> `frontend/package.json` hiện có React trong dependencies, nhưng giao diện đang chạy là bộ trang JavaScript tĩnh được Vite phục vụ từ `frontend/public`.

## Cấu trúc thư mục quan trọng

```text
NCKH/
├── backend/
│   ├── main.py                         # Khởi tạo FastAPI và MySQL
│   ├── seed_admin.py                   # Tạo/cập nhật tài khoản admin
│   ├── api/routes.py                   # API của toàn hệ thống
│   ├── core/
│   │   ├── scraper.py                  # Crawler bài báo/PDF
│   │   ├── tamanh_crawler.py           # Crawler Q&A Tâm Anh
│   │   ├── pdf_pipeline.py             # Điều phối tách và xuất PDF
│   │   ├── llm_pdf_extractor.py        # Kiểm chứng cấu trúc bằng Gemini
│   │   ├── ner_engine.py               # NER bằng từ điển
│   │   ├── ai_ner.py                   # Gọi Gemini cho AI labeling
│   │   ├── auth.py                     # Hash mật khẩu và phiên đăng nhập
│   │   ├── article_exporter.py         # Xuất TXT/JSON của bài báo
│   │   └── Tu Dien Y Hoc/              # Dữ liệu từ điển runtime
│   ├── prompts/
│   │   └── pdf_section_extraction.md   # Prompt tách section bằng LLM
│   ├── Văn_Bản_Y_Tế_PDF/
│   │   ├── candidates/                 # PDF đạt điều kiện
│   │   └── quarantine/                 # PDF cần kiểm tra
│   └── Kho_Ngu_Lieu_Txt/
│       ├── pdf_extracted/              # Kết quả tách PDF
│       └── tamanh/                     # Corpus Q&A Tâm Anh
├── frontend/
│   ├── public/                         # Trang, script và CSS giao diện
│   ├── package.json
│   └── vite.config.js                  # Vite và proxy API
├── tests/                              # Test tổng hợp cấp repository
├── mysql.txt                           # Schema MySQL
├── requirements.txt                    # Danh sách phụ thuộc bổ sung
└── run.txt                             # Lệnh chạy nhanh backend
```

## Yêu cầu môi trường

- Python 3.11 trở lên được khuyến nghị.
- MySQL 8.x.
- Node.js phiên bản tương thích với Vite 8.
- npm.
- Gemini API key nếu dùng AI labeling hoặc LLM PDF extraction.

## Cài đặt

### 1. Clone repository

```powershell
git clone https://github.com/LCV-npc/NCKH_CORPUS.git
cd NCKH_CORPUS
```

### 2. Khởi tạo MySQL

Mở MySQL client và chạy file schema:

```sql
SOURCE D:/NCKH_CORPUS/NCKH/mysql.txt;
```

Nếu repository nằm ở vị trí khác, thay đường dẫn trên bằng đường dẫn tuyệt đối đến `mysql.txt`. Schema mặc định là `yhoc_corpus`.

Các bảng chính:

| Bảng | Mục đích |
| --- | --- |
| `articles` | Metadata và nội dung bài báo |
| `extracted_concepts` | Thực thể/khái niệm đã gán nhãn |
| `crawl_progress`, `crawl_logs` | Tiến độ và nhật ký crawler |
| `tamanh_qa_metadata` | Metadata Q&A Tâm Anh |
| `users`, `user_sessions` | Tài khoản và phiên đăng nhập |
| `ai_document_labels` | Kết quả gán nhãn bằng AI |
| `expert_reviews` | Review và lịch sử nhận xét chuyên gia |
| `corpus_language_audit` | Kết quả kiểm tra ngôn ngữ corpus |

### 3. Cài backend

Từ thư mục gốc repository:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cd ..
```

Nếu PowerShell chặn script kích hoạt, có thể gọi trực tiếp Python trong môi trường ảo:

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

### 4. Cấu hình `.env`

Sao chép file mẫu:

```powershell
Copy-Item backend\.env.example backend\.env
```

Điền giá trị thật trong `backend/.env`:

```dotenv
DB_PASSWORD=your_mysql_password
DB_USER=root
DB_HOST=127.0.0.1
DB_NAME=yhoc_corpus
GEMINI_API_KEY=your_gemini_api_key
```

Các tùy chọn PDF/Gemini đã được mô tả trong `backend/.env.example`, gồm model, timeout, số lần retry, prompt version và ngưỡng kiểm tra ngôn ngữ.

`backend/.env.example` là mẫu đầy đủ được lưu trên Git để mô tả mọi biến hỗ trợ; `backend/.env` là cấu hình thật của riêng máy và bị Git bỏ qua. Các biến tùy chọn có thể vắng mặt vì source đã có giá trị mặc định, nhưng:

- `DB_PASSWORD` là bắt buộc để đăng nhập và sử dụng mọi chức năng cần MySQL.
- `GEMINI_API_KEY` là bắt buộc cho AI labeling và bước kiểm chứng PDF bằng Gemini.
- `DB_USER`, `DB_HOST`, `DB_NAME`, các biến `PDF_LLM_*`, `AUTH_SESSION_HOURS` và `CORPUS_LANGUAGE_*` có giá trị mặc định nhưng nên ghi rõ để môi trường chạy có thể tái lập.
- `CORPUS_PDF_CANDIDATES_DIR`, `CORPUS_PDF_QUARANTINE_DIR`, `CORPUS_LANGUAGE_AUDIT_REPORT_DIR` và `PDF_EXTRACT_OUTPUT_DIR` kiểm soát nơi lưu PDF, quarantine, báo cáo audit và kết quả tách PDF.
- `CRAWLER_TAMANH_*` kiểm soát URL nguồn, retry, độ trễ, timeout và thư mục lưu Q&A Tâm Anh.

Các đường dẫn tương đối trong `.env` luôn được tính từ thư mục `backend`, vì vậy vị trí lưu không thay đổi khi chạy lệnh từ thư mục gốc hoặc từ `backend`.

Không commit `backend/.env`, API key hoặc mật khẩu thật lên Git.

### 5. Cài frontend

```powershell
cd frontend
npm install
cd ..
```

## Chạy hệ thống

Mở hai terminal tại thư mục gốc repository.

### Terminal 1 — Backend

Chế độ phát triển, tự reload khi source thay đổi:

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --no-access-log
```

Khi đang crawl hoặc tách nhiều PDF, nên chạy không có `--reload` để tránh tiến trình bị khởi động lại giữa chừng:

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn main:app --no-access-log
```

Backend: <http://127.0.0.1:8000><br>
API docs: <http://127.0.0.1:8000/docs>

### Terminal 2 — Frontend

```powershell
cd frontend
npm run dev
```

Frontend: <http://localhost:5173>

`frontend/vite.config.js` chuyển tiếp các request `/api` đến `http://127.0.0.1:8000`.

## Tạo tài khoản admin

Hệ thống không cho đăng ký tài khoản công khai. Admin được tạo từ terminal; sau khi đăng nhập, Admin tạo tài khoản Expert trong mục **Users**. Tạo Admin từ thư mục `backend` bằng lệnh:

```powershell
.\.venv\Scripts\python.exe seed_admin.py --name "Admin Corpus" --email "admin@example.com"
```

Chương trình sẽ yêu cầu nhập và xác nhận mật khẩu. Khi gõ, terminal không hiển thị ký tự hoặc dấu `*`; đây là hành vi bảo mật bình thường của `getpass`. Nhập xong rồi nhấn Enter.

Nếu email đã tồn tại, script cập nhật tài khoản đó thành admin và thay mật khẩu bằng giá trị mới đã được hash.

## Luồng thu thập PDF bài báo

1. Admin chọn nguồn tạp chí và khoảng năm trên trang **Thu thập dữ liệu**.
2. Frontend gửi `POST /api/scrape`.
3. Backend khởi chạy crawler nền và cập nhật `crawl_progress`/`crawl_logs`.
4. Crawler duyệt archive, số tạp chí, bài báo và liên kết PDF.
5. Hệ thống kiểm tra trùng, năm, ngôn ngữ và tình trạng PDF.
6. PDF hợp lệ được lưu theo website và năm; file cần kiểm tra được chuyển vào quarantine.

```text
backend/Văn_Bản_Y_Tế_PDF/
├── candidates/
│   └── <ten-mien>/
│       └── <nam>/
│           └── *.pdf
└── quarantine/
    └── <ten-mien>/
        └── <nam>/
            └── *.pdf
```

Crawler hiện hỗ trợ các nguồn được cấu hình trên giao diện, gồm:

- `tapchinghiencuuyhoc.vn`
- `tapchiyhcd.vn`
- `tapchiyhocvietnam.vn`

Nút **Dừng thu thập** gọi `POST /api/scrape/stop`. Trạng thái tổng quát lấy từ `GET /api/status`, còn log lấy từ `GET /api/crawl-logs`.

## Luồng crawl Q&A Tâm Anh

1. Admin nhập khoảng năm và tham số crawl.
2. Frontend gửi `POST /api/crawler/tamanh/start`.
3. `TamanhCrawler` duyệt khu vực `/tu-van/`, chuyên khoa, phân trang và trang chi tiết Q&A.
4. Mỗi cặp hỏi–đáp được tách thành nội dung bệnh nhân và bác sĩ.
5. Hệ thống ghi TXT UTF-8 và lưu metadata vào JSON/MySQL.

```text
backend/Kho_Ngu_Lieu_Txt/tamanh/
├── metadata.json
└── tu_van/
    └── <chuyen-khoa>/
        ├── <chuyen-khoa>_<id>_BN.txt
        └── <chuyen-khoa>_<id>_BS.txt
```

- `_BN.txt`: câu hỏi/nội dung của bệnh nhân.
- `_BS.txt`: câu trả lời của bác sĩ.
- Bảng `tamanh_qa_metadata`: URL nguồn, chuyên khoa, đường dẫn file, thời gian và metadata liên quan.
- Trạng thái job: `GET /api/crawler/tamanh/status/{job_id}`.
- Dừng job: `POST /api/crawler/tamanh/stop/{job_id}`.

## Luồng tách PDF thành TXT/JSON

Trang **Tách nội dung PDF** cho phép chọn từng PDF hoặc upload cả thư mục. Với thư mục, frontend chỉ gửi các file `.pdf`; backend kiểm tra SHA-256 để bỏ qua nội dung đã xử lý, tránh tạo bản trích xuất trùng.

Quy trình xử lý:

```text
PDF
  → kiểm tra hash trùng
  → đọc text và thông tin bố cục bằng PyMuPDF/pdfplumber
  → chuẩn hóa Unicode, khoảng trắng và dòng
  → nhận diện tiêu đề/tác giả/tóm tắt theo nhiều tín hiệu
  → nhận diện heading và section
  → kiểm tra, sửa kết quả bằng Gemini khi được cấu hình
  → ghi TXT từng phần + JSON từng section + Master JSON
```

Kết quả mặc định:

```text
backend/Kho_Ngu_Lieu_Txt/pdf_extracted/
└── <ten-bai-bao>_<ma-hash>/
    ├── title.txt
    ├── authors.txt
    ├── abstract.txt
    ├── keywords.txt                    # nếu PDF có từ khóa
    ├── affiliations.txt                # nếu PDF có đơn vị công tác
    ├── metadata.json
    ├── structured_article.json         # Master JSON
    ├── 01_abstract.json
    ├── 02_<section>.txt
    ├── 02_<section>.json
    └── ...
```

Văn bản trong mỗi trường/section được nối thành đoạn liên tục thay vì giữ cách xuống dòng trình bày của PDF. Prompt Gemini được lưu tại `backend/prompts/pdf_section_extraction.md`; kết quả LLM có cache theo hash đầu vào, model và phiên bản prompt.

Trong lúc xử lý nhiều file, frontend hiển thị tiến độ và nút dừng. Lệnh dừng hủy việc gửi file tiếp theo; file đang được backend xử lý có thể cần hoàn tất request hiện tại trước khi dừng hẳn.

## Gán nhãn y khoa

### Từ điển

Nguồn từ điển runtime chính:

```text
backend/core/Tu Dien Y Hoc/
├── manifest_v1.json
├── icd10_v1.json
└── yhct_v1.json
```

Từ điển tùy chỉnh nằm tại:

```text
backend/Kho Ngữ Liệu Y Học Tiếng Việt/Từ_Điển_v1.json
```

`ner_engine.py` chuẩn hóa mục từ, đối chiếu theo ranh giới từ/cụm từ và ưu tiên cụm dài nhằm hạn chế việc khớp một phần giữa từ như `ho` trong `học` hoặc `u mô` bên trong một tên bệnh dài.

### Gemini AI labeling

- `POST /api/ai-label`: sinh kết quả gán nhãn AI.
- `POST /api/ai-label/save`: lưu kết quả AI vào database.
- Kết quả lưu tại `ai_document_labels`, gắn với document thật.
- Admin và Expert xem cùng tập thực thể đã lưu; quyền truy cập document vẫn được backend kiểm tra.

## Authentication và phân quyền

- `POST /api/auth/login`: xác thực email/mật khẩu và tạo session.
- `GET /api/auth/me`: đọc người dùng hiện tại.
- `POST /api/auth/logout`: hủy session.
- `POST /api/admin/users`: chỉ Admin được tạo tài khoản Expert.
- Mật khẩu được hash ở backend; API không trả password/hash.
- Backend lấy role từ database, không tin role do frontend gửi lên.

### Admin

Admin truy cập toàn bộ chức năng hiện tại, tất cả document, AI label, trạng thái review, người review và nhận xét chuyên gia.

### Expert

Expert chỉ được backend trả về document đã có nhãn ICD-10 hoặc AI label. Expert không thể mở document chưa gán nhãn bằng URL/API trực tiếp.

Các trang chuyên gia:

- `/expert/dashboard`
- `/expert/icd10`
- `/expert/ai-labeled`
- `/expert/review/:documentId`
- `/expert/reviewed`

Review được lưu trong `expert_reviews`, gồm document, expert, quyết định, nhãn gốc, ICD-10 đề xuất, comment và thời gian. Khi chỉnh sửa, lịch sử cũ được giữ để phục vụ audit.

## API chính

| Nhóm | Endpoint |
| --- | --- |
| Auth | `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout` |
| Corpus | `GET /api/articles`, `GET /api/articles/{article_id}` |
| NER | `POST /api/ner`, `POST /api/highlight-text`, `POST /api/save-highlight` |
| AI label | `POST /api/ai-ner`, `POST /api/ai-label`, `POST /api/ai-label/save` |
| Dictionary | `GET /api/dictionary`, `GET /api/dictionary/status`, `POST /api/save-to-dictionary` |
| PDF | `POST /api/extract-pdf` |
| Crawler PDF | `POST /api/scrape`, `POST /api/scrape/stop`, `GET /api/status`, `GET /api/crawl-logs` |
| Crawler Tâm Anh | `POST /api/crawler/tamanh/start`, `GET /api/crawler/tamanh/status/{job_id}`, `POST /api/crawler/tamanh/stop/{job_id}` |
| Expert | `GET /api/expert/dashboard`, các API `/api/expert/documents/*` và review |
| Admin | `GET /api/admin/reviews`, `GET/POST /api/admin/users`, `GET /api/admin/documents/{document_id}` |
| Health | `GET /api/health` |

Chi tiết request/response hiện hành có tại Swagger UI: <http://127.0.0.1:8000/docs>.

## Kiểm thử

Chạy toàn bộ test backend từ thư mục gốc:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests tests -q
```

Build frontend:

```powershell
cd frontend
npm run build
```

Các luồng nên kiểm tra trước khi triển khai:

- Admin tạo tài khoản Expert trong trang Users, sau đó Expert đăng nhập.
- Đăng nhập Admin; Expert không truy cập được route Admin.
- Expert chỉ thấy document ICD-10/AI-labeled.
- Lưu review/comment, reload và đọc lại từ database.
- Admin xem được review của Expert.
- Crawl từng nguồn và kiểm tra cấu trúc `candidates/<domain>/<year>`.
- Crawl Tâm Anh và kiểm tra đúng cặp `_BN.txt`/`_BS.txt`.
- Upload lại cùng PDF và xác nhận hệ thống bỏ qua theo SHA-256.
- Đối chiếu title, authors, abstract và section TXT/JSON với PDF gốc.

## Bảo mật và vận hành

- Không đưa `.env`, Gemini API key, mật khẩu MySQL hoặc token phiên vào Git.
- Nếu một API key từng được chia sẻ công khai, hãy thu hồi và tạo key mới.
- Chạy backend không `--reload` trong các tác vụ crawl/tách PDF dài.
- Dữ liệu trong `candidates`, `quarantine`, `pdf_extracted` và corpus Tâm Anh có thể lớn; cần sao lưu và theo dõi dung lượng.
- Kết quả tách PDF tự động vẫn cần đối chiếu nguồn, đặc biệt với PDF scan, bố cục nhiều cột hoặc cấu trúc tạp chí không chuẩn.

## Tài liệu bổ sung

- [`CORPUS_REVIEW_SYSTEM.md`](CORPUS_REVIEW_SYSTEM.md): kiến trúc và quy trình review corpus.
- [`backend/README.md`](backend/README.md): ghi chú backend và crawler.
- [`backend/.env.example`](backend/.env.example): toàn bộ biến môi trường được hỗ trợ.
- [`backend/prompts/pdf_section_extraction.md`](backend/prompts/pdf_section_extraction.md): hợp đồng prompt tách section PDF.
