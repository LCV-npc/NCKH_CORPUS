import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, 'd:/NCKH/demo_5')
from ner_engine import run_ner

tests = [
    ("THA và tăng huyết áp nguyên phát", "THA, tăng huyết áp"),
    ("bệnh nhân có đột quỵ não cấp và tiền sử tai biến mạch máu não", "đột quỵ"),
    ("chẩn đoán thiếu máu não thoáng qua (TIA)", "TIA / G45.9"),
    ("tổn thương, tổn thương phổi, tổn thương não", "tổn thương đơn: OK | tổn thương + cơ quan: CHẶN"),
]

for text, desc in tests:
    _, _, raw = run_ner(text)
    print(f"\n[{desc}]")
    print(f"  Text: {text}")
    for e in raw:
        print(f"  -> {repr(e['text']):35s} | {e['entity_type']:12s} | {e['icd_code']}")
    if not raw:
        print("  -> (không có entity)")
