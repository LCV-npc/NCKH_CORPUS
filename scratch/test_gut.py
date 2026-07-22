import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, 'd:/NCKH/demo_5')
from ner_engine import run_ner

text = (
    "Gút là một bệnh lý viêm khớp do lắng đọng tinh thể urat trong bối cảnh tăng acid uric máu kéo dài. "
    "Nghiên cứu này nhằm khảo sát đặc điểm đa hình rs72552713 của gen ABCG2 và đánh giá mối liên quan "
    "giữa biến thể này với nồng độ acid uric máu ở bệnh nhân gút. "
    "Nghiên cứu mô tả cắt ngang có phân tích được thực hiện trên 150 bệnh nhân gút điều trị tại Bệnh viện."
)

_, _, raw = run_ner(text)
print("Entities found:")
for e in raw:
    print("  -", repr(e["text"]), "|", e["entity_type"], "|", e["icd_code"])
