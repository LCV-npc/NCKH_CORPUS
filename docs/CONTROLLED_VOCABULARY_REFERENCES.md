# Controlled vocabulary cho Manual Labeling

## 1. Kỹ thuật được áp dụng

Hệ thống sử dụng **rule-based controlled lexical variant generation** kết hợp **concept normalization**:

```text
canonical term trong ICD-10
        ↓ luật biến thể ngữ pháp an toàn
surface form trong văn bản
        ↓
cùng concept_id / concept_code / canonical_term
        ↓
exact longest-match và highlight trên offset gốc
```

Ví dụ:

```text
canonical_term: Các rối loạn sinh lực
surface_form:   rối loạn sinh lực
mapping:        cùng Concept ID
matched_by:     grammar_variant
```

Runtime tự sinh các dạng bỏ prefix được khai báo rõ trong artifact: `các X`/`những X` → `X` (grammar variant) và `Bệnh lý X` → `X` (headword variant). Phần còn lại phải có ít nhất hai token, không được tạo mapping đa Concept và không được làm thay đổi Concept ID. `một X`, word reordering, synonym tự do, fuzzy, embedding và semantic similarity không nằm trong lớp tự động này.

`grammar_variant` có `rule_score=0.98`, còn `headword_variant` có `rule_score=0.97`. Đây là điểm heuristic deterministic, không phải xác suất hay confidence đã calibration.

Các variant được sinh thành artifact `backend/core/Tu Dien Y Hoc/controlled_vocabulary_v1.json` bởi `backend/core/dictionary_builder.py`, với `icd10_v1.json` là Concept authority duy nhất. Artifact lưu thêm `rule_id`, `entity_type`, `status`, `source_concept_id` và provenance để dùng cho corpus NER + Concept ID; sau đó được kiểm tra qua `manifest_v1.json`/SHA-256 khi load. `backend/core/ner_engine.py` dùng chúng như một exact dictionary entry, giữ offset của văn bản gốc và ưu tiên longest-match. `dictionary_alias_candidates` chỉ lưu evidence/metrics từ corpus: grammar/headword variant có trạng thái `AUTO_APPROVED`; fuzzy/ambiguous có trạng thái `PENDING_REVIEW` và không được highlight.

## 2. Các bài báo áp dụng kỹ thuật liên quan

| Tài liệu | Kỹ thuật được báo cáo | Áp dụng vào hệ thống |
|---|---|---|
| Tsuruoka, McNaught & Ananiadou (2008), [Normalizing biomedical terms by minimizing ambiguity and variability](https://doi.org/10.1186/1471-2105-9-S3-S2) | Tự động phát hiện luật chuẩn hóa; tối ưu giảm variability nhưng hạn chế tăng ambiguity giữa term và Concept ID. | Dùng làm cơ sở cho ambiguity gate và yêu cầu một surface form chỉ được tự động map khi không xung đột Concept. |
| Kang et al. (2013), [Using rule-based natural language processing to improve disease normalization in biomedical text](https://doi.org/10.1136/amiajnl-2012-001173) | Rule-based phrase adjustment, term variation, boundary correction và filtering trước/sau dictionary normalization. | Áp dụng phiên bản tối giản: tạo variant ngữ pháp an toàn, boundary/token validation, không đưa POS/chunking model vào Manual Labeling. |
| Funk, Cohen, Hunter & Verspoor (2016), [Gene Ontology synonym generation rules lead to increased performance in biomedical concept recognition](https://doi.org/10.1186/s13326-016-0096-7) | Recursive syntactic rules và derivational rules để sinh synonym; đánh giá trên corpus gold và phân tích over-generation. | Chỉ lấy ý tưởng sinh variant có luật; giới hạn còn `các/những` để giữ precision và tránh sinh synonym tự do. |
| Aronson et al. (2010), [An overview of MetaMap](https://pmc.ncbi.nlm.nih.gov/articles/PMC2995713/) | Lexical lookup → variant generation → candidate identification → mapping/ranking trong UMLS. | Tham khảo kiến trúc trung gian; không dùng MetaMap/UMLS vì Concept ID của corpus này chỉ đến từ ICD-10. |
| Abbas et al. (2021), [Clinical concept extraction with lexical semantics to support automatic annotation](https://doi.org/10.3390/ijerph182010564) | Tách surface form, canonical concept và lexical semantics để hỗ trợ clinical concept extraction/annotation. | Áp dụng ở data model: surface form được map về canonical term và Concept ID; không dùng external ontology. |
| Tien et al. (2019), [Rule based English-Vietnamese bilingual terminology extraction from Vietnamese documents](https://doi.org/10.1145/3368926.3369664) | Rule-based terminology candidate generation và thống kê cụm trong văn bản tiếng Việt. | Áp dụng ở discovery/evidence: boundary, occurrence, document frequency, MI/context để phân tích candidate; không dùng thống kê để tự chọn Concept ID. |
| Vu et al. (2018), [VnCoreNLP](https://aclanthology.org/N18-5012/) | Word segmentation và xử lý ngôn ngữ tiếng Việt. | Chỉ áp dụng nguyên tắc boundary/token; matcher hiện tại giữ offset ký tự gốc và không nhúng toàn bộ mô hình VnCoreNLP. |
| Ngo et al. (2020), [Towards a UMLS-Integratable Vietnamese Medical Terminology](https://doi.org/10.1007/978-981-15-6168-9_32) | Xây dựng lexical resources tiếng Việt từ nguồn từ điển và corpus y khoa. | Tham khảo cách kết hợp nguồn lexical/corpus; hệ thống không nhập UMLS và giữ ICD-10 làm Concept authority cho corpus này. |

## 3. Bằng chứng thực nghiệm và giới hạn

Các nghiên cứu cho thấy rule-based variant generation có thể tăng khả năng nhận diện, nhưng phải đo trên gold corpus và kiểm soát over-generation. Funk et al. báo cáo F-measure GO tăng từ 0.498 lên 0.636 sau khi áp dụng các luật sinh synonym; Kang et al. báo cáo F-score exact-boundary của MetaMap tăng từ 61.0% lên 73.3% và Peregrine từ 63.9% lên 78.0% khi bổ sung rule-based NLP. Các số liệu này không được coi là kết quả của NCKH_CORPUS; hệ thống phải đo lại trên gold annotation của chính mình.

Vì người dùng đã bỏ bước Reviewer/Admin duyệt alias, chỉ các luật deterministic có phạm vi hẹp mới được tự động kích hoạt. Một alias mới không sinh ra từ luật an toàn, lỗi chính tả, surface form ambiguous hoặc quan hệ chỉ “gần nghĩa” vẫn không được tự động highlight. Những trường hợp đó chỉ xuất hiện ở `suggestions` để phục vụ đánh giá corpus, không trở thành nhãn cuối.

## 4. Cách đánh giá trong hệ thống

So sánh tối thiểu hai chế độ:

1. Baseline: canonical/alias có sẵn + exact + longest-match.
2. Controlled variant: baseline + `grammar_variant`/`headword_variant` + ambiguity/boundary gate.

Báo cáo riêng `Precision`, `Recall`, `F1`, `TP`, `FP`, `FN` cho canonical, JSON alias, grammar variant và fuzzy suggestion. Gold phải là annotation đã được xác nhận trong corpus; output tự sinh không được dùng làm gold của chính nó.
