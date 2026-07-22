# -*- coding: utf-8 -*-
"""Test NER với tiếng Việt có dấu."""
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"d:\NCKH\demo_5")
os.chdir(r"d:\NCKH\demo_5")

from ner_engine import run_ner

tests = [
    "Bệnh nhân bị viêm phổi nặng và tăng huyết áp",
    "Chẩn đoán: Suy tim mạn tính, đái tháo đường típ 2",
    "Yêu thống, Hạc tất phong, Chứng tý",
    "Viêm dạ dày và tá tràng kèm Hoàng đản mạn tính",
    "Bệnh nhân đột quỵ, nhồi máu cơ tim cấp, tăng huyết áp vô căn",
    "Rối loạn chuyển hóa lipoprotein và tình trạng tăng lipid máu",
    "Di chứng nhồi máu não (Bán thân bất toại)",
]

print("=" * 65)
for text in tests:
    h, c, r = run_ner(text)
    print(f"\nINPUT: {text}")
    if r:
        for e in r:
            print(f"  [{e['entity_type']}] '{e['text']}' -> {e['icd_code']} [{e.get('matched_by','?')}]")
    else:
        print("  (no match)")
print("\n" + "=" * 65)
print(f"Tests done. Dict size: {len(__import__('ner_dict').fuzzy_dict)}")
