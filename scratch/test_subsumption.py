import sys, io
sys.path.insert(0, 'd:/NCKH/demo_5')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import ner_engine

engine = ner_engine.NEREngine()

# ── Test _resolve_overlaps directly ─────────────────────────────────────────
print("=== Test _resolve_overlaps: longer fuzzy wins over shorter exact ===")
cands = [
    {
        "entity_text":     "benh gan",
        "start_token_idx": 0,
        "end_token_idx":   1,
        "char_start":      0,
        "char_end":        8,
        "score":           100.0,
        "matched_by":      "exact",
        "icd_code":        "K70",
        "icd_label_vn":    "",
        "entity_type":     "DISEASE",
        "is_dagger":       False,
    },
    {
        "entity_text":     "benh gan nhiem mo",
        "start_token_idx": 0,
        "end_token_idx":   3,
        "char_start":      0,
        "char_end":        17,
        "score":           92.0,
        "matched_by":      "fuzzy",
        "icd_code":        "K76.0",
        "icd_label_vn":    "",
        "entity_type":     "DISEASE",
        "is_dagger":       False,
    },
]
chosen = engine._resolve_overlaps(cands)
print(f"  Candidates : {[c['entity_text'] for c in cands]}")
print(f"  Chosen     : {[c['entity_text'] for c in chosen]}")

assert len(chosen) == 1, f"FAIL: expected 1 result, got {len(chosen)}"
assert chosen[0]["entity_text"] == "benh gan nhiem mo", (
    f"FAIL: expected 'benh gan nhiem mo', got {chosen[0]['entity_text']!r}"
)
print("  PASSED: 4-token fuzzy candidate wins over 2-token exact candidate")

# ── Test non-overlapping candidates are both kept ────────────────────────────
print()
print("=== Test _resolve_overlaps: non-overlapping candidates both kept ===")
cands2 = [
    {
        "entity_text":     "viem gan",
        "start_token_idx": 0,
        "end_token_idx":   1,
        "char_start":      0,
        "char_end":        8,
        "score":           100.0,
        "matched_by":      "exact",
        "icd_code":        "K73",
        "icd_label_vn":    "",
        "entity_type":     "DISEASE",
        "is_dagger":       False,
    },
    {
        "entity_text":     "xo gan",
        "start_token_idx": 3,
        "end_token_idx":   4,
        "char_start":      12,
        "char_end":        18,
        "score":           100.0,
        "matched_by":      "exact",
        "icd_code":        "K74",
        "icd_label_vn":    "",
        "entity_type":     "DISEASE",
        "is_dagger":       False,
    },
]
chosen2 = engine._resolve_overlaps(cands2)
print(f"  Candidates : {[c['entity_text'] for c in cands2]}")
print(f"  Chosen     : {[c['entity_text'] for c in chosen2]}")
assert len(chosen2) == 2, f"FAIL: expected 2 results, got {len(chosen2)}"
print("  PASSED: both non-overlapping candidates kept")

# ── Test tie-break: same length, exact wins over fuzzy ──────────────────────
print()
print("=== Test _resolve_overlaps: same-length tie-break, exact wins ===")
cands3 = [
    {
        "entity_text":     "tang huyet ap",
        "start_token_idx": 0,
        "end_token_idx":   2,
        "char_start":      0,
        "char_end":        13,
        "score":           100.0,
        "matched_by":      "exact",
        "icd_code":        "I10",
        "icd_label_vn":    "",
        "entity_type":     "DISEASE",
        "is_dagger":       False,
    },
    {
        "entity_text":     "tang huyet ap",
        "start_token_idx": 0,
        "end_token_idx":   2,
        "char_start":      0,
        "char_end":        13,
        "score":           95.0,
        "matched_by":      "fuzzy",
        "icd_code":        "I10X",
        "icd_label_vn":    "",
        "entity_type":     "DISEASE",
        "is_dagger":       False,
    },
]
chosen3 = engine._resolve_overlaps(cands3)
print(f"  Candidates : {[(c['entity_text'], c['matched_by']) for c in cands3]}")
print(f"  Chosen     : {[(c['entity_text'], c['matched_by']) for c in chosen3]}")
assert len(chosen3) == 1, f"FAIL: expected 1 result, got {len(chosen3)}"
assert chosen3[0]["matched_by"] == "exact", (
    f"FAIL: expected exact to win, got {chosen3[0]['matched_by']!r}"
)
print("  PASSED: exact wins tie-break over fuzzy")

print()
print("=== ALL TESTS PASSED ===")
