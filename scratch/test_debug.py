import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, 'd:/NCKH/demo_5')
from ner_engine import NEREngine
from ner_dict import fuzzy_dict, BLOCK_PHRASES
import unicodedata

engine = NEREngine()

# Debug: tại sao "thiếu máu não thoáng qua" không match?
text = "thiếu máu não thoáng qua"
cleaned = engine._preprocess(text)
tokens = engine._tokenize(cleaned)
print("Tokens:", [(t.word, t.start, t.end) for t in tokens])

for n in range(len(tokens), 0, -1):
    chunk = tokens[:n]
    phrase = cleaned[chunk[0].start:chunk[-1].end].strip()
    phrase_lower = phrase.lower()
    chunk_words = [t.word.lower() for t in chunk]
    key = unicodedata.normalize('NFC', phrase_lower)
    in_dict = key in fuzzy_dict
    skip = engine._should_skip_ngram(chunk_words, phrase, phrase_lower, text, chunk[0].start, chunk[-1].end, n)
    print(f"  n={n} phrase={repr(phrase_lower):40s} in_dict={in_dict} skip={skip}")

# Debug: tổn thương phổi
print("\n--- tổn thương phổi ---")
text2 = "tổn thương phổi"
cleaned2 = engine._preprocess(text2)
tokens2 = engine._tokenize(cleaned2)
print("Tokens:", [(t.word, t.start, t.end) for t in tokens2])
for n in range(len(tokens2), 0, -1):
    chunk = tokens2[:n]
    phrase = cleaned2[chunk[0].start:chunk[-1].end].strip()
    phrase_lower = phrase.lower()
    chunk_words = [t.word.lower() for t in chunk]
    key = unicodedata.normalize('NFC', phrase_lower)
    in_dict = key in fuzzy_dict
    in_block = phrase_lower in BLOCK_PHRASES
    skip = engine._should_skip_ngram(chunk_words, phrase, phrase_lower, text2, chunk[0].start, chunk[-1].end, n)
    print(f"  n={n} phrase={repr(phrase_lower):30s} in_dict={in_dict} in_block={in_block} skip={skip}")
