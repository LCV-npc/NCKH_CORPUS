import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, 'd:/NCKH/demo_5')
from ner_engine import NEREngine
from ner_dict import fuzzy_dict, BLOCK_PHRASES, STOP_WORDS
import unicodedata

engine = NEREngine()

# 1. Debug "thiếu máu não thoáng qua"
print("=== thiếu máu não thoáng qua ===")
text = "thiếu máu não thoáng qua"
cleaned = engine._preprocess(text)
print("After preprocess:", repr(cleaned))
tokens = engine._tokenize(cleaned)
print("Tokens:", [t.word for t in tokens])
for n in range(len(tokens), 0, -1):
    chunk = tokens[:n]
    phrase = cleaned[chunk[0].start:chunk[-1].end].strip()
    phrase_lower = phrase.lower()
    chunk_words = [t.word.lower() for t in chunk]
    key = unicodedata.normalize('NFC', phrase_lower)
    in_dict = key in fuzzy_dict
    skip = engine._should_skip_ngram(chunk_words, phrase, phrase_lower, text, chunk[0].start, chunk[-1].end, n)
    print(f"  n={n} phrase={repr(phrase_lower):40s} in_dict={in_dict} skip={skip} first={chunk_words[0]!r} last={chunk_words[-1]!r}")

# 2. Debug đột quỵ não cấp
print("\n=== đột quỵ não cấp ===")
text2 = "bệnh nhân có đột quỵ não cấp và tiền sử tai biến mạch máu não"
cleaned2 = engine._preprocess(text2)
tokens2 = engine._tokenize(cleaned2)
# Tìm "đột quỵ não" trong tokens
for i, t in enumerate(tokens2):
    if t.word.lower() == 'đột':
        print(f"  Found 'đột' at token {i}: {[(tokens2[i+k].word if i+k < len(tokens2) else '') for k in range(5)]}")
        for n in range(4, 0, -1):
            if i + n > len(tokens2): continue
            chunk = tokens2[i:i+n]
            phrase = cleaned2[chunk[0].start:chunk[-1].end].strip()
            phrase_lower = phrase.lower()
            chunk_words = [t.word.lower() for t in chunk]
            key = unicodedata.normalize('NFC', phrase_lower)
            in_dict = key in fuzzy_dict
            skip = engine._should_skip_ngram(chunk_words, phrase, phrase_lower, text2, chunk[0].start, chunk[-1].end, n)
            print(f"    n={n} {repr(phrase_lower):30s} in_dict={in_dict} skip={skip}")

# 3. "tổn thương" sau dấu phẩy vẫn match - OK? 
print("\n=== tổn thương phổi — xem full run ===")
from ner_engine import run_ner
_, _, raw3 = run_ner("tổn thương phổi")
for e in raw3:
    print(f"  -> {repr(e['text'])} | {e['icd_code']} | start={e['start']} end={e['end']}")
