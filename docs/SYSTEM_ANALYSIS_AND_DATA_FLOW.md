# Phân tích hệ thống NCKH_CORPUS

## 1. Mục đích và phạm vi

Tài liệu này mô tả kiến trúc, nguồn dữ liệu, luồng xử lý và mức độ áp dụng các kỹ thuật trong bộ tài liệu tham khảo tại `Newspaper/` của hệ thống NCKH_CORPUS.

Phạm vi phân tích gồm:

- Crawler và lưu bài báo.
- Trích xuất nội dung PDF.
- Manual Labeling bằng từ điển ICD-10/YHCT.
- AI Annotation bằng Gemini/XLM-R.
- Expert Review và Reviewer Adjudication.
- Candidate alias, Concept ID và provenance.
- Lưu trữ MySQL, giao diện frontend và API FastAPI.

Nguyên tắc quan trọng của Manual Labeling:

1. `icd10_v1.json` và `yhct_v1.json` là nguồn Concept chuẩn duy nhất.
2. Nhãn xác nhận phải có `concept_id`, `concept_code` và `canonical_term` truy nguyên được về JSON.
3. Exact matching chạy trước; matching theo cụm dài hơn được ưu tiên.
4. Fuzzy và surface form ambiguous chỉ tạo suggestion `UNCERTAIN`, không tự tạo nhãn chính thức.
5. Alias mới chỉ được nạp vào runtime sau khi Admin/Reviewer duyệt.
6. Offset luôn được tính trên văn bản gốc, không tính trên chuỗi đã normalize.
7. Manual Labeling không dùng ChatGPT, Gemini, embedding hoặc XLM-R để quyết định nhãn cuối.

## 2. Kiến trúc tổng thể

```text
                         +----------------------+
                         |  Browser / Frontend  |
                         |  Vite + HTML/CSS/JS  |
                         +----------+-----------+
                                    |
                         REST /api + proxy localhost
                                    |
                         +----------v-----------+
                         | FastAPI backend      |
                         | api/routes.py        |
                         +--+--------+-------+--+
                            |        |       |
             +--------------+        |       +----------------+
             |                       |                        |
    +--------v---------+   +---------v----------+    +--------v---------+
    | Rule NER          |   | AI Annotation      |    | Review workflow  |
    | ner_dict.py       |   | ai_ner.py         |    | expert/reviewer  |
    | ner_engine.py     |   | xlmr_ner.py       |    | adjudication     |
    +--------+----------+   +---------+----------+    +--------+---------+
             |                         |                       |
    +--------v---------+      +--------v---------+      +--------v---------+
    | ICD-10/YHCT JSON |      | Gemini/XLM-R     |      | MySQL review DB  |
    | manifest + hash   |      | isolated result  |      | labels/reviews   |
    +-------------------+      +------------------+      +------------------+
```

## 3. Thành phần chính và trách nhiệm

| Thành phần | File/thư mục | Trách nhiệm |
|---|---|---|
| Application startup | `backend/main.py` | Đọc biến môi trường, kiểm tra `DB_PASSWORD`, nạp dictionary, khởi tạo FastAPI, CORS, security headers và router. |
| API | `backend/api/routes.py` | Authentication, articles, highlight, crawler, PDF, AI, review, alias và statistics. |
| Dictionary loader | `backend/core/ner_dict.py` | Đọc JSON đã kiểm tra checksum, tạo Concept index, xử lý alias và ambiguity. |
| Rule NER | `backend/core/ner_engine.py` | Tokenization, exact matching, longest-match-first, accentless matching có kiểm soát, offset và HTML highlight. |
| AI Gemini | `backend/core/ai_ner.py` | Gửi văn bản đến Gemini qua module AI riêng và chuẩn hóa entity AI. |
| AI XLM-R | `backend/core/xlmr_ner.py` | Chạy mô hình XLM-R local nếu đã cấu hình checkpoint. |
| PDF extraction | `backend/pdf_extractor.py` | Đọc block/bbox, sắp xếp vùng, bỏ header/footer/table boilerplate và tách title/author/abstract/section. |
| Crawler | `backend/core/scraper.py` | Tìm archive, tải PDF, lọc host, kiểm tra file và ghi log tiến trình. |
| Manual UI | `frontend/public/index.html`, `script.js` | Chọn bài, gọi rule NER, hiển thị highlight, suggestion và lưu kết quả. |
| AI UI | `frontend/public/script.js`, `script_ai.js` | Chọn model, gọi Gemini/XLM-R, hiển thị nhóm entity và lưu AI result riêng. |
| Review UI | `frontend/public/review.js` | Expert review, Reviewer adjudication, tài khoản và alias candidate. |
| Database schema | `mysql.txt` | Tables cho article, concept, alias, user, session, review, adjudication và crawler. |

## 4. Nguồn dữ liệu và data lineage

### 4.1 Dictionary

Manual NER đọc hai artifact đã version hóa:

```text
backend/core/Tu Dien Y Hoc/icd10_v1.json
backend/core/Tu Dien Y Hoc/yhct_v1.json
backend/core/Tu Dien Y Hoc/manifest_v1.json
```

`manifest_v1.json` chứa số lượng entry, version và SHA-256. Khi backend khởi động:

1. Đọc manifest.
2. Kiểm tra file tồn tại.
3. Tính SHA-256 và so với manifest.
4. Kiểm tra số entry và dictionary version.
5. Kiểm tra liên kết về source trong `data/` nếu artifact có khai báo source checksum.
6. Chỉ sau đó mới nạp term vào runtime index.

Mỗi entry runtime có các trường quan trọng:

```json
{
  "concept_id": "ICD10:I652:base",
  "concept_code": "I652",
  "display_code": "I65.2",
  "canonical_term": "Nghẽn tắc và hẹp động mạch cảnh",
  "surface_form": "...",
  "concept_id_source": "ICD10"
}
```

### 4.2 Bài báo

Nguồn runtime chính của Manual Labeling là:

```text
articles.abstract trong MySQL
```

Metadata lấy từ `articles.title`, `authors`, `publication_year`, `source_url`. Nội dung PDF/TXT trên filesystem chỉ là nguồn nhập liệu hoặc provenance; không phải nguồn quyết định Concept ID.

### 4.3 Alias đã duyệt

Alias phát hiện từ corpus được lưu trong:

```text
dictionary_alias_candidates
```

Chỉ dòng `status = 'APPROVED'` mới được load bởi `load_runtime_aliases()`. Các dòng `PENDING_REVIEW` hoặc `REJECTED` không ảnh hưởng đến HTML highlight.

### 4.4 Annotation và provenance

Annotation Manual được lưu trong `extracted_concepts` với:

```text
article_id
concept_name
concept_type
concept_code
concept_id
match_method
confidence_score
dictionary_version
concept_start
concept_end
```

Vì vậy có thể trả lời ngược từ một highlight về bài báo, Concept JSON, phương pháp match, version dictionary và offset gốc.

## 5. Luồng khởi động và xác thực

```text
Start uvicorn
    |
    v
load_backend_env()
    |
    +--> Kiểm tra DB_PASSWORD
    +--> load_ner_dictionary()
    |       +--> verify manifest/checksum
    |       +--> load ICD-10
    |       +--> load YHCT
    |       +--> build exact/ambiguous/accentless index
    |
    +--> init_router(db_config, output_folder)
    |       +--> đọc approved aliases từ MySQL nếu bảng đã tồn tại
    |
    +--> ensure schema auth/review/language audit
    +--> kiểm tra migration crawl_logs
    +--> FastAPI ready
```

Authentication dùng session token qua `Authorization`. Các endpoint quản trị/manual yêu cầu Admin; endpoint review được giới hạn theo role. Backend cũng thêm `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, CSP và `Cache-Control: no-store` cho API.

## 6. Luồng Data Crawling

### Input

- URL archive hoặc website mục tiêu.
- Khoảng năm.
- Cấu hình DB và thư mục output.

### Xử lý

1. Frontend gửi `POST /api/scrape` hoặc `POST /api/crawler/tamanh/start`.
2. Backend kiểm tra request và tạo job.
3. Crawler tìm archive/issue/article link, kiểm tra cùng host và canonical URL.
4. PDF được tải vào thư mục candidate.
5. File được kiểm tra định dạng, tên và trạng thái download.
6. Metadata/progress ghi vào `crawl_progress`, `crawl_logs`.
7. Article hợp lệ được insert/update vào `articles`.

### Output

```text
articles.title
articles.authors
articles.abstract
articles.publication_year
articles.source_url
```

Crawler không tự gán Concept ID. NER chỉ chạy ở bước annotation sau đó.

## 7. Luồng PDF Content Extraction

### API

```text
POST /api/extract-pdf
```

### Pipeline

```text
PDF bytes/path
    ↓
PyMuPDF block + bbox extraction
    ↓
đọc thứ tự vùng/cột/trang
    ↓
nhận diện heading, title, author, abstract, section
    ↓
bỏ header/footer, caption, table boilerplate
    ↓
validate section coverage và offset
    ↓
trả structured document
```

Output có thể gồm title, authors, abstract, sections, page/line provenance và warnings. PDF extractor không quyết định nhãn bệnh; nó chỉ nâng chất lượng văn bản đầu vào cho crawler/corpus.

## 8. Luồng Manual Labeling bằng ICD-10/YHCT

### 8.1 Chọn bài

1. Frontend gọi `GET /api/articles`.
2. Người dùng chọn article.
3. Frontend gọi `GET /api/articles/{article_id}` nếu article chưa load.
4. Backend trả `abstract`, metadata và các label dictionary đã lưu.

### 8.2 Bật annotation

Frontend gọi:

```text
POST /api/highlight-text
```

Body:

```json
{
  "text": "articles.abstract",
  "threshold": 100,
  "enable_tone_restore": false,
  "enable_noun_phrase": false
}
```

Backend gọi `run_ner()`. Luồng bên trong:

```text
abstract gốc
    ↓
Unicode NFC + casefold cho lookup
    ↓
tokenization có start/end trên chuỗi gốc
    ↓
exact canonical/JSON alias
    ↓
longest-match-first
    ↓
boundary validation
    ↓
approved runtime alias
    ↓
accentless match nếu không gây collision
    ↓
resolve overlap
    ↓
tạo entity + Concept metadata
    ↓
HTML escape + <mark> theo offset gốc
```

### 8.3 Exact matching và boundary

Matcher không tìm substring tùy ý. Ví dụ:

```text
Không match "ho" trong "học".
Không match một cụm ngắn bên trong canonical term dài nếu cụm đó chưa có alias.
Không tự map "hẹp động mạch cảnh" vào "Nghẽn tắc và hẹp động mạch cảnh".
```

`longest-match-first` giúp chọn cụm đầy đủ trước cụm con. Khi nhiều candidate khác Concept dùng chung một surface form, key bị đưa khỏi `term_dict` unambiguous và chỉ trả về `suggestions`.

### 8.4 Kết quả API

```json
{
  "highlighted_html": "...",
  "matched_concepts": [
    {
      "name": "...",
      "start": 120,
      "end": 145,
      "concept_id": "ICD10:...",
      "code": "...",
      "matched_by": "exact",
      "confidence": 1.0
    }
  ],
  "suggestions": [
    {
      "text": "...",
      "start": 180,
      "end": 195,
      "matched_by": "fuzzy_candidate",
      "review_state": "UNCERTAIN",
      "candidate_concepts": []
    }
  ]
}
```

Chỉ `matched_concepts` được render thành highlight chính thức. `suggestions` chỉ hiển thị ở panel review.

### 8.5 Lưu kết quả

Frontend gọi:

```text
POST /api/save-highlight
```

Backend:

1. Chạy lại rule NER trên abstract gốc, không tin HTML do client gửi.
2. Xóa các `extracted_concepts` cũ của article.
3. Chỉ insert entity có offset hợp lệ và Concept metadata hợp lệ.
4. Lưu `concept_id`, `match_method`, `confidence_score`, `dictionary_version`, `concept_start`, `concept_end`.
5. Cập nhật `articles.highlighted_html`.

Điểm này bảo vệ database khỏi việc client sửa HTML để tự đưa entity ngoài dictionary vào.

## 9. Luồng phát hiện và duyệt alias

### 9.1 Sinh candidate từ corpus

```text
POST /api/dictionary/alias-candidates/discover
```

Mặc định chỉ sinh grammar variant an toàn, ví dụ loại determiner đầu cụm nếu canonical term xuất hiện thực tế trong abstract. Candidate được kiểm tra boundary và phải xuất hiện trong corpus.

Các thống kê được lưu:

```text
occurrence_count
document_frequency
boundary_score
context_consistency
mi_score
density_score
uncertainty_score
priority_score
```

Fuzzy candidate chỉ chạy khi request có:

```json
{
  "include_fuzzy_candidates": true,
  "include_context": true,
  "limit": 1000
}
```

### 9.2 Xếp hàng review

Candidate được sắp xếp theo:

```text
priority_score = 0.6 * uncertainty_score
               + 0.4 * density_score
```

Điều này ưu tiên candidate vừa không chắc chắn vừa có khả năng bao phủ nhiều document, nhưng không biến điểm thống kê thành Concept ID tự động.

### 9.3 Duyệt alias

Admin/Reviewer có thể duyệt trong `Alias từ điển` hoặc từ panel suggestion. Backend kiểm tra:

- Concept ID/code còn tồn tại trong JSON.
- Dictionary version không bị cũ.
- Surface form không xung đột Concept khác.
- Alias không trỏ tới một Concept ambiguous.
- Boundary và dữ liệu evidence hợp lệ.

Sau khi approve:

```text
PENDING_REVIEW → APPROVED
    ↓
load_runtime_aliases()
    ↓
reset_ner_engine()
    ↓
alias được dùng trong lần highlight tiếp theo
```

Reject không nạp alias vào runtime.

## 10. Luồng AI Annotation

AI Annotation là pipeline riêng, không trộn với Manual Labeling.

### Gemini

```text
POST /api/ai-ner hoặc /api/ai-label
    ↓
ai_ner.py / ai_label.py
    ↓
Gemini API
    ↓
chuẩn hóa entity, type, code, span
    ↓
frontend highlight AI
    ↓
ai_document_labels khi lưu
```

### XLM-R

```text
POST /api/xlmr-ner
    ↓
xlmr_ner.py
    ↓
checkpoint local đã cấu hình
    ↓
normalize DIAGNOSTIC → DIAGNOSTIC_PROCEDURE
    ↓
frontend highlight AI
```

AI result có thể chứa entity chưa có trong dictionary. Entity đó không được tự động đưa vào `extracted_concepts` Manual. Việc thêm term mới phải qua flow review/dictionary phù hợp.

## 11. Luồng Expert Review và Reviewer Adjudication

### Expert

1. Admin tạo labeling session tại `/api/admin/labeling-sessions`.
2. Session gắn article và expert/reviewer.
3. Expert xem document ICD-10 hoặc AI-labeled.
4. Expert gửi review tại `/api/expert/documents/{document_id}/reviews`.
5. Review lưu status, source, suggested code, annotation và comment.

### Reviewer

1. Reviewer xem queue tại `/api/reviewer/reviews`.
2. Mở document và các review của Expert.
3. Chọn quyết định cuối cho từng annotation.
4. Gửi `/api/reviewer/documents/{document_id}/adjudication`.
5. Backend lưu final labels, decisions, resolution status và note.

Gold data phải lấy từ các annotation đã Expert/Reviewer xác nhận, không lấy suggestion tự động làm gold.

## 12. Dashboard và thống kê

Dashboard lấy dữ liệu từ các bảng MySQL, chủ yếu là:

- `articles` cho số bài và trạng thái.
- `extracted_concepts` cho frequency của dictionary concepts.
- `ai_document_labels` cho AI statistics.
- `expert_reviews` và `reviewer_adjudications` cho review status.
- `crawl_logs`/`crawl_progress` cho crawler.

`/api/top-concepts` trả thống kê concept theo type. `concept_id` và `concept_code` giúp tránh gộp nhầm các surface form cùng tên nhưng khác Concept.

## 13. Đối chiếu với tài liệu tham khảo

### 13.1 Abbas et al. 2021 - lexical semantics và concept extraction

Nguồn: `Newspaper/(2).pdf`.

Ý tưởng được áp dụng:

- Tách surface form, canonical term và Concept ID.
- Cho phép nhiều surface form trỏ về một Concept.
- Giữ candidate ambiguity thay vì ghi đè Concept cuối cùng.
- Dùng approximate match như gợi ý, không làm nhãn y khoa tự động.

Mức độ áp dụng: **đã áp dụng trong `ner_dict.py`, `ner_engine.py` và alias workflow**.

Không dùng UMLS hoặc external medical ontology; hệ thống chỉ dùng ICD-10/YHCT theo phạm vi đề tài.

### 13.2 VnCoreNLP 2018 - Vietnamese word segmentation

Nguồn: `Newspaper/(8).pdf`.

Ý tưởng được áp dụng ở mức rule-only:

- Xử lý ranh giới token tiếng Việt.
- Không match một token nằm bên trong token khác.
- Giữ ranh giới ký tự riêng với normalization.
- Xử lý combining mark để không biến một chữ tiếng Việt thành token ngắn giả.

Mức độ áp dụng: **đã áp dụng nguyên tắc boundary/tokenization; không nhúng mô hình VnCoreNLP vào runtime Manual NER**.

### 13.3 Tien et al. 2019 - rule-based terminology extraction và MI

Nguồn: `Newspaper/(5).pdf`.

Ý tưởng được áp dụng:

- Sinh candidate bằng rule thay vì lấy mọi n-gram.
- Kiểm tra độ dài và boundary.
- Đếm occurrence/document frequency.
- Tính MI/PMI-like phrase score để xếp hạng cụm.
- Dùng context consistency để giảm candidate quá ngắn hoặc không ổn định.

Mức độ áp dụng: **đã áp dụng trong `/api/dictionary/alias-candidates/discover`; MI chỉ dùng xếp hạng review, không quyết định Concept ID**.

### 13.4 Cañizares-Díaz et al. 2021 - active learning

Nguồn: `Newspaper/2021.ranlp-1.26.pdf`.

Ý tưởng được áp dụng phù hợp với rule-only:

- Pre-annotation bằng matcher.
- Uncertainty sampling cho ambiguous/fuzzy/hiếm gặp.
- Density/corpus coverage dựa trên occurrence và document frequency.
- Human-in-the-loop: reviewer duyệt candidate rồi mới đưa vào runtime.

Mức độ áp dụng: **đã áp dụng phiên bản deterministic/statistical**.

Không dùng sentence embedding trong Manual Labeling vì yêu cầu của hệ thống cấm embedding/semantic model làm nhãn cuối.

### 13.5 Xie et al. 2023 - zero-shot NER với ChatGPT

Nguồn: `Newspaper/2023.emnlp-main.493.pdf`.

Bài báo được dùng để tham khảo cho **AI Annotation/Evaluation**, không đưa các kỹ thuật decomposed-QA, syntactic prompting, self-consistency hoặc majority voting vào Manual Labeling.

Lý do: Manual Labeling cần nhãn có thể truy nguyên trực tiếp về ICD-10/YHCT; LLM result có thể được lưu ở pipeline AI riêng để so sánh hoặc review.

### 13.6 Fundus và Trafilatura - chất lượng input web

Nguồn:

- `Newspaper/(4).pdf` - Fundus.
- `Newspaper/2021.acl-demo.15.pdf` - Trafilatura.

Ý tưởng áp dụng:

- Tách main content khỏi menu/footer/boilerplate.
- Giữ title, abstract, paragraph và metadata.
- Dùng ở tầng crawler/content cleaning, không dùng để chọn Concept ID.

Mức độ áp dụng: **áp dụng về nguyên tắc ở scraper/extraction; không phải matcher NER**.

### 13.7 S2ORC - corpus metadata và provenance

Nguồn: `Newspaper/2020.acl-main.447.pdf`.

Ý tưởng áp dụng:

- Lưu document ID và metadata có cấu trúc.
- Tách raw text, normalized lookup text và annotated HTML.
- Lưu dictionary version, pipeline version, offset và evidence.

Mức độ áp dụng: **đã áp dụng một phần qua articles, extracted_concepts và dictionary provenance**.

### 13.8 CCpdf và CERMINE - PDF layout/metadata

Nguồn:

- `Newspaper/(3).pdf` - CCpdf.
- `Newspaper/s10032-015-0249-8.pdf` - CERMINE.

Ý tưởng áp dụng:

- Tôn trọng thứ tự section và layout của PDF.
- Tách metadata, title, author, abstract, table/reference.
- Ghi nhận lỗi OCR hoặc extraction quality.

Mức độ áp dụng: **đã áp dụng ở `pdf_extractor.py`; không dùng để gán Concept ID**.

### 13.9 Các PDF khác trong Newspaper

`Newspaper/` còn có các bài về deduplication, database/corpus và crawler. Các bài này được coi là tài liệu tham khảo bổ trợ cho corpus engineering; chúng không được dùng làm căn cứ để tự động gán nhãn Manual nếu không có mapping rõ ràng về ICD-10/YHCT.

## 14. So sánh pipeline cũ và pipeline hiện tại

### Baseline rule dictionary

```text
canonical term + JSON alias
exact matching
normalization cơ bản
longest-match-first
```

### Rule dictionary hiện tại

```text
baseline
+ stable Concept ID
+ candidate set cho surface form ambiguous
+ boundary/token validation
+ controlled grammar variant discovery
+ approved alias runtime
+ accentless collision gate
+ fuzzy review-only suggestion
+ MI/context/frequency ranking
+ uncertainty + density review queue
+ offset/provenance/dictionary version
```

Kỳ vọng:

| Thành phần | Tác động chính |
|---|---|
| Exact canonical | Precision cao, Recall phụ thuộc coverage JSON. |
| Approved alias | Tăng Recall có kiểm soát. |
| Boundary/longest match | Giảm false positive và boundary error. |
| Ambiguity gate | Giữ Precision khi một surface form có nhiều Concept. |
| MI/context | Giảm alias candidate quá ngắn/quá dài. |
| Fuzzy suggestion | Gợi ý lỗi chính tả nhưng không làm tăng nhãn sai tự động. |
| Active review queue | Giảm thời gian tìm candidate và tăng Recall sau khi alias được duyệt. |

F1 phải được đo trên gold labels đã Reviewer xác nhận. Không được dùng output tự sinh của v2 làm gold cho v2.

## 15. API map nhanh

| Nhóm | Endpoint | Input chính | Output/lưu trữ |
|---|---|---|---|
| Auth | `/api/auth/login` | email/password | session/user |
| Article | `/api/articles`, `/api/articles/{id}` | filters/id | metadata + abstract |
| Manual NER | `/api/highlight-text`, `/api/ner` | text | HTML + concepts + suggestions |
| Manual save | `/api/save-highlight` | article + concepts | `articles`, `extracted_concepts` |
| Dictionary | `/api/dictionary`, `/api/dictionary/status` | - | manifest/status |
| Alias discovery | `/api/dictionary/alias-candidates/discover` | limit/options | pending candidates |
| Alias review | `/api/dictionary/alias-candidates` | filters | candidate/evidence/metrics |
| Alias approve | `.../{id}/approve`, `/approve-suggestion` | candidate/concept | APPROVED + runtime reload |
| Alias reject | `.../{id}/reject` | candidate id | REJECTED |
| AI | `/api/ai-ner`, `/api/xlmr-ner`, `/api/ai-label` | text/model | AI entities |
| AI save | `/api/ai-label/save` | article/labels | `ai_document_labels` |
| PDF | `/api/extract-pdf` | PDF bytes | structured extraction |
| Review | `/api/expert/...`, `/api/reviewer/...` | review decisions | reviews/adjudication |
| Experiment | `/api/ner-experiments/...` | run config | metrics/errors/checkpoints |
| Crawler | `/api/scrape`, `/api/crawler/tamanh/...` | URL/year | articles/logs/progress |

## 16. Kiểm thử và vận hành

### Kiểm thử đã có

- Dictionary checksum/source integrity.
- Canonical ICD-10/YHCT lookup.
- Combining mark boundary.
- Short token không match substring.
- Stable Concept ID.
- Fuzzy suggestion không vào entity/highlight.
- Ambiguous surface form không tự label.
- Frontend `node --check` và Vite build.

### Lệnh kiểm tra khuyến nghị

```powershell
cd backend
python -m unittest tests.test_dictionary_integrity -v
python -m py_compile core/ner_dict.py core/ner_engine.py api/routes.py

cd ..\frontend
npm run build
node --check public/script.js
node --check public/review.js
```

### Migration MySQL

Database mới có thể chạy toàn bộ `mysql.txt`. Database cũ cần chạy các `ALTER TABLE` được chú thích cho:

- `extracted_concepts`: `concept_id`, `match_method`, `confidence_score`, `dictionary_version`.
- `dictionary_alias_candidates`: method, score, frequency, context và dictionary version.

Nên backup database trước migration và không commit `.env`/credential/API key.

## 17. Kết luận

Hệ thống hiện có hai pipeline được tách rõ:

```text
Manual Labeling = JSON ICD-10/YHCT + rule matcher + approved alias + reviewer
AI Annotation   = Gemini/XLM-R + AI review/evaluation
```

Các bài báo đã được sử dụng theo đúng tầng phù hợp: lexical semantics, boundary, rule terminology extraction và active review được đưa vào Manual Labeling; crawler/PDF papers chỉ cải thiện input/provenance; ChatGPT zero-shot và embedding không được dùng làm nhãn y khoa cuối. Thiết kế này giúp tăng Recall qua alias được kiểm soát nhưng vẫn giữ được Precision, khả năng truy nguyên và khả năng rollback theo dictionary version.
