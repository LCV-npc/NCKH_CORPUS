import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from ner_dict import load_ner_dictionary, fuzzy_dict
load_ner_dictionary()
from ner_engine import run_ner

text_vn  = "tỷ lệ đột quỵ hoặc thiếu máu não thoáng qua là 21,9%"
text_raw = "ty le dot quy hoac thieu mau nao thoang qua la 21,9%"

_, _, raw1 = run_ner(text_vn)
print("=== KET QUA CO DAU ===")
if raw1:
    for e in raw1:
        print(f"  [{e['entity_type']}] \"{e['text']}\" -> {e['icd_code']}")
else:
    print("  (KHONG CO KET QUA)")

_, _, raw2 = run_ner(text_raw)
print("=== KET QUA KHONG DAU ===")
if raw2:
    for e in raw2:
        print(f"  [{e['entity_type']}] \"{e['text']}\" -> {e['icd_code']}")
else:
    print("  (KHONG CO KET QUA)")

# Kiểm tra thêm từ bài báo gốc
text_full = "Kết quả cho thấy tỷ lệ đột quỵ hoặc thiếu máu não thoáng qua là 21,9%."
_, _, raw3 = run_ner(text_full)
print("=== KET QUA BAI BAO GOC ===")
if raw3:
    for e in raw3:
        print(f"  [{e['entity_type']}] \"{e['text']}\" -> {e['icd_code']}")
else:
    print("  (KHONG CO KET QUA)")
