#!/usr/bin/env python3
"""Normalize VietBioNER and ViMedNer into a shared Gemini NER schema."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MAIN_LABELS = {
    "DISEASE",
    "SYMPTOM",
    "CAUSE",
    "DIAGNOSTIC_PROCEDURE",
    "TREATMENT",
}
OPTIONAL_LABELS = {"LOCATION", "DATETIME", "ORGANISATION"}

VIMED_MAPPING = {
    "ten_benh": "DISEASE",
    "trieu_chung_benh": "SYMPTOM",
    "nguyen_nhan_benh": "CAUSE",
    "bien_phap_chan_doan": "DIAGNOSTIC_PROCEDURE",
    "bien_phap_dieu_tri": "TREATMENT",
}

VIETBIO_MAPPING = {
    "DiagnosticProcedure": "DIAGNOSTIC_PROCEDURE",
    "Location": "LOCATION",
    "DateTime": "DATETIME",
    "Organisation": "ORGANISATION",
}

SYSTEM_INSTRUCTION = (
    "Trích xuất thực thể y khoa tiếng Việt theo 5 nhãn DISEASE, SYMPTOM, "
    "CAUSE, DIAGNOSTIC_PROCEDURE và TREATMENT. Chỉ trả về JSON hợp lệ."
)

RIGHT_ATTACHED = {".", ",", ";", ":", "!", "?", "%", ")", "]", "}", "…"}
LEFT_ATTACHED = {"(", "[", "{"}

DISEASE_PATTERNS = (
    r"\bbệnh\b",
    r"\bviêm\b",
    r"\bung thư\b",
    r"\bhội chứng\b",
    r"\brối loạn\b",
    r"\bnhiễm\b",
    r"\bu\s",
    r"\blao\b",
    r"\bhiv\b",
    r"\baids\b",
    r"\bđái tháo đường\b",
    r"\btăng huyết áp\b",
)

SYMPTOM_PATTERNS = (
    r"\bđau\b",
    r"\bho\b",
    r"\bkhó thở\b",
    r"\bmệt\b",
    r"\bchóng mặt\b",
    r"\bbuồn nôn\b",
    r"\bnôn\b",
    r"\bphù\b",
    r"\bngứa\b",
    r"\bchảy máu\b",
    r"\bsụt cân\b",
    r"\bsốt\b",
)

SYMPTOM_CONTEXT = re.compile(
    r"(?:triệu chứng|biểu hiện|dấu hiệu|than phiền|xuất hiện|ghi nhận)\s+(?:là|gồm|như)?\s*$",
    re.IGNORECASE,
)
DISEASE_CONTEXT = re.compile(
    r"(?:chẩn đoán|mắc|bị|điều trị|tiền sử|nghi mắc)\s+(?:bệnh)?\s*$",
    re.IGNORECASE,
)

MISSING_TARGET_CUES = re.compile(
    r"\b(?:nguyên nhân|gây|yếu tố nguy cơ|phơi nhiễm|hút thuốc|virus|vi khuẩn|"
    r"điều trị|thuốc|phẫu thuật|xạ trị|hóa trị|hoá trị|vắc xin|vaccine|liệu pháp)\b",
    re.IGNORECASE,
)

SUSPICIOUS_SPAN_WORDS = re.compile(
    r"\b(?:là|được|có thể|gây ra|bao gồm|chẳng hạn|giúp|để|sẽ|một)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TokenRow:
    token: str
    tag: str


def normalize_lookup(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold().strip()
    return re.sub(r"\s+", " ", text)


def read_conll(path: Path) -> tuple[list[list[TokenRow]], list[str]]:
    sentences: list[list[TokenRow]] = []
    current: list[TokenRow] = []
    issues: list[str] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                if current:
                    sentences.append(current)
                    current = []
                continue
            parts = line.rsplit(maxsplit=1)
            if len(parts) != 2:
                issues.append(f"{path}:{line_number}: malformed line: {line!r}")
                continue
            current.append(TokenRow(unicodedata.normalize("NFC", parts[0]), parts[1]))
    if current:
        sentences.append(current)
    return sentences, issues


def detokenize(rows: list[TokenRow]) -> tuple[str, list[tuple[int, int]]]:
    chunks: list[str] = []
    offsets: list[tuple[int, int]] = []
    cursor = 0
    previous = ""
    for row in rows:
        token = row.token
        needs_space = bool(chunks) and token not in RIGHT_ATTACHED and previous not in LEFT_ATTACHED
        if needs_space:
            chunks.append(" ")
            cursor += 1
        start = cursor
        chunks.append(token)
        cursor += len(token)
        offsets.append((start, cursor))
        previous = token
    return "".join(chunks), offsets


def split_tag(tag: str) -> tuple[str, str | None]:
    if tag == "O":
        return "O", None
    if "-" not in tag:
        return "?", tag
    prefix, label = tag.split("-", 1)
    return prefix, label


def collect_spans(
    rows: list[TokenRow],
    source: str,
) -> tuple[list[dict], list[str]]:
    text, offsets = detokenize(rows)
    spans: list[dict] = []
    issues: list[str] = []
    active_label: str | None = None
    active_start = -1
    active_end = -1

    def close_active() -> None:
        nonlocal active_label, active_start, active_end
        if active_label is not None:
            start = offsets[active_start][0]
            end = offsets[active_end][1]
            spans.append(
                {
                    "text": text[start:end],
                    "source_label": active_label,
                    "start": start,
                    "end": end,
                    "start_token": active_start,
                    "end_token": active_end + 1,
                }
            )
        active_label = None
        active_start = -1
        active_end = -1

    for index, row in enumerate(rows):
        prefix, label = split_tag(row.tag)
        if prefix == "O":
            close_active()
            continue
        if prefix not in {"B", "I"} or label is None:
            close_active()
            issues.append(f"unknown tag {row.tag!r} at token {index}")
            continue

        # VietBioNER is IO-style: a new I-X after O or another label starts a span.
        if prefix == "B" or active_label != label:
            if prefix == "I" and source == "vimedner":
                issues.append(f"orphan I-{label} at token {index}; treated as B-{label}")
            close_active()
            active_label = label
            active_start = index
        active_end = index
    close_active()
    return spans, issues


def load_medical_lexicons(path: Path | None) -> tuple[set[str], set[str]]:
    disease: set[str] = set()
    symptom: set[str] = set()
    if path is None or not path.exists():
        return disease, symptom
    records = json.loads(path.read_text(encoding="utf-8-sig"))
    for record in records:
        term = record.get("TÊN BỆNH") or record.get("ten_benh") or ""
        normalized = normalize_lookup(str(term))
        if not normalized:
            continue
        target = symptom if str(record.get("ENTITY_TYPE", "")).upper() == "SYMPTOM" else disease
        target.add(normalized)
        if normalized.startswith("bệnh "):
            target.add(normalized[5:])
    return disease, symptom


def classify_symptom_disease(
    entity_text: str,
    sentence: str,
    start: int,
    disease_lexicon: set[str],
    symptom_lexicon: set[str],
) -> tuple[str, float, str]:
    value = normalize_lookup(entity_text)
    no_prefix = value[5:] if value.startswith("bệnh ") else value
    in_disease = value in disease_lexicon or no_prefix in disease_lexicon
    in_symptom = value in symptom_lexicon or no_prefix in symptom_lexicon
    left_context = sentence[max(0, start - 80) : start]

    if in_disease and not in_symptom:
        return "DISEASE", 0.98, "icd_dictionary"
    if in_symptom and not in_disease:
        return "SYMPTOM", 0.98, "icd_symptom_dictionary"
    if SYMPTOM_CONTEXT.search(left_context):
        return "SYMPTOM", 0.92, "symptom_context"
    if DISEASE_CONTEXT.search(left_context):
        return "DISEASE", 0.92, "disease_context"

    disease_hit = any(re.search(pattern, value, re.IGNORECASE) for pattern in DISEASE_PATTERNS)
    symptom_hit = any(re.search(pattern, value, re.IGNORECASE) for pattern in SYMPTOM_PATTERNS)
    named_fever_disease = bool(re.search(r"\bsốt (?:xuất huyết|rét|thương hàn)\b", value))
    if named_fever_disease:
        return "DISEASE", 0.95, "named_fever_disease"
    if disease_hit and not symptom_hit:
        return "DISEASE", 0.88, "disease_lexical_pattern"
    if symptom_hit and not disease_hit:
        return "SYMPTOM", 0.88, "symptom_lexical_pattern"
    return "DISEASE", 0.50, "unresolved_default_requires_review"


def map_entities(
    source: str,
    text: str,
    spans: list[dict],
    disease_lexicon: set[str],
    symptom_lexicon: set[str],
    include_optional: bool,
) -> tuple[list[dict], list[dict]]:
    mapped: list[dict] = []
    reviews: list[dict] = []
    for span in spans:
        source_label = span["source_label"]
        confidence = 1.0
        reason = "direct_mapping"
        if source == "vimedner":
            label = VIMED_MAPPING.get(source_label)
        elif source_label == "Symptom_and_Disease":
            label, confidence, reason = classify_symptom_disease(
                span["text"], text, span["start"], disease_lexicon, symptom_lexicon
            )
        else:
            label = VIETBIO_MAPPING.get(source_label)

        if label is None:
            reviews.append({**span, "reason": "unmapped_source_label"})
            continue
        if label in OPTIONAL_LABELS and not include_optional:
            continue

        entity = {
            "text": span["text"],
            "label": label,
            "start": span["start"],
            "end": span["end"],
            "source_label": source_label,
            "confidence": confidence,
            "mapping_reason": reason,
        }
        mapped.append(entity)
        if confidence < 0.85:
            reviews.append({**entity, "reason": reason})
    return mapped, reviews


def audit_entities(source: str, entities: list[dict]) -> list[str]:
    flags: list[str] = []
    for entity in entities:
        token_count = len(entity["text"].split())
        if token_count > 12:
            flags.append(f"long_span:{entity['label']}:{token_count}")
        if source == "vimedner" and token_count >= 5 and SUSPICIOUS_SPAN_WORDS.search(entity["text"]):
            flags.append(f"clause_like_span:{entity['label']}")
    return sorted(set(flags))


def convert_split(
    source: str,
    split: str,
    path: Path,
    disease_lexicon: set[str],
    symptom_lexicon: set[str],
    include_optional: bool,
) -> tuple[list[dict], list[dict], list[str]]:
    sentences, file_issues = read_conll(path)
    examples: list[dict] = []
    reviews: list[dict] = []
    issues = list(file_issues)
    for index, rows in enumerate(sentences):
        sample_id = f"{source}:{split}:{index:06d}"
        text, _ = detokenize(rows)
        spans, span_issues = collect_spans(rows, source)
        entities, entity_reviews = map_entities(
            source,
            text,
            spans,
            disease_lexicon,
            symptom_lexicon,
            include_optional,
        )
        flags = audit_entities(source, entities)
        missing_schema_risk = source == "vietbioner" and bool(MISSING_TARGET_CUES.search(text))
        ambiguous = bool(entity_reviews)
        example = {
            "id": sample_id,
            "source": source,
            "split": split,
            "input_text": text,
            "entities": entities,
            "quality": {
                "ambiguous_mapping": ambiguous,
                "missing_schema_risk": missing_schema_risk,
                "audit_flags": flags,
            },
        }
        examples.append(example)
        for review in entity_reviews:
            reviews.append(
                {
                    "id": sample_id,
                    "input_text": text,
                    "entity": review,
                    "approved": False,
                    "final_label": None,
                }
            )
        issues.extend(f"{sample_id}: {message}" for message in span_issues)
    return examples, reviews, issues


def public_entities(example: dict) -> list[dict]:
    return [{"text": item["text"], "label": item["label"]} for item in example["entities"]]


def output_text(example: dict) -> str:
    return json.dumps({"entities": public_entities(example)}, ensure_ascii=False, separators=(",", ":"))


def vertex_record(example: dict, system_instruction: str) -> dict:
    return {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [
            {"role": "user", "parts": [{"text": f"Văn bản: {example['input_text']}"}]},
            {"role": "model", "parts": [{"text": output_text(example)}]},
        ],
    }


def write_jsonl(path: Path, records: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def validate_example(example: dict) -> None:
    text = example["input_text"]
    previous_end = -1
    for entity in example["entities"]:
        if entity["label"] not in MAIN_LABELS | OPTIONAL_LABELS:
            raise ValueError(f"{example['id']}: invalid label {entity['label']!r}")
        start, end = entity["start"], entity["end"]
        if not (0 <= start < end <= len(text)):
            raise ValueError(f"{example['id']}: invalid offsets {start}:{end}")
        if text[start:end] != entity["text"]:
            raise ValueError(f"{example['id']}: span text does not match source")
        if start < previous_end:
            raise ValueError(f"{example['id']}: overlapping or unsorted entities")
        previous_end = end


def deduplicate_splits(by_split: dict[str, list[dict]]) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """Keep test before dev before train so evaluation examples never leak into training."""
    result: dict[str, list[dict]] = {"train": [], "dev": [], "test": []}
    removed: dict[str, int] = {"train": 0, "dev": 0, "test": 0}
    seen: set[str] = set()
    for split in ("test", "dev", "train"):
        for example in by_split[split]:
            key = normalize_lookup(example["input_text"])
            if key in seen:
                removed[split] += 1
                continue
            seen.add(key)
            result[split].append(example)
    return result, removed


def safe_for_tuning(example: dict) -> bool:
    quality = example["quality"]
    if quality["ambiguous_mapping"] or quality["audit_flags"]:
        return False
    # VietBioNER does not exhaustively annotate CAUSE and TREATMENT.
    if example["source"] == "vietbioner" and quality["missing_schema_risk"]:
        return False
    return True


def entity_counts(examples: Iterable[dict]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for example in examples:
        counts.update(entity["label"] for entity in example["entities"])
    return dict(sorted(counts.items()))


def write_collection(
    output_dir: Path,
    name: str,
    by_split: dict[str, list[dict]],
    system_instruction: str,
) -> dict:
    summary: dict[str, dict] = {}
    for split, examples in by_split.items():
        target = output_dir / name
        for example in examples:
            validate_example(example)
        write_jsonl(target / f"{split}.internal.jsonl", examples)
        write_jsonl(target / f"{split}.jsonl", (vertex_record(item, system_instruction) for item in examples))
        summary[split] = {
            "examples": len(examples),
            "entities": entity_counts(examples),
        }
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dictionary", type=Path, default=None)
    parser.add_argument("--system-prompt", type=Path, default=None)
    parser.add_argument("--include-optional", action="store_true")
    return parser.parse_args()


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    root = args.root.resolve()
    raw = root / "raw"
    output = root / "processed"
    dictionary = args.dictionary
    if dictionary is None:
        candidate = root.parent / "data" / "icd10_dictionary.json"
        dictionary = candidate if candidate.exists() else None
    disease_lexicon, symptom_lexicon = load_medical_lexicons(dictionary)
    system_prompt_path = args.system_prompt or (root / "prompts" / "system_prompt_vi.txt")
    system_instruction = (
        system_prompt_path.read_text(encoding="utf-8-sig").strip()
        if system_prompt_path.exists()
        else SYSTEM_INSTRUCTION
    )

    source_paths = {
        "vietbioner": raw / "VietBioNER" / "data_supervised_learning",
        "vimedner": raw / "ViMedNer" / "data",
    }
    all_examples: dict[str, dict[str, list[dict]]] = {source: {} for source in source_paths}
    all_reviews: list[dict] = []
    all_issues: list[str] = []
    for source, base in source_paths.items():
        for split in ("train", "dev", "test"):
            path = base / f"{split}.txt"
            if not path.exists():
                raise FileNotFoundError(f"Missing source file: {path}")
            examples, reviews, issues = convert_split(
                source,
                split,
                path,
                disease_lexicon,
                symptom_lexicon,
                args.include_optional,
            )
            all_examples[source][split] = examples
            all_reviews.extend(reviews)
            all_issues.extend(issues)

    write_jsonl(output / "review" / "vietbioner_mapping_review.jsonl", all_reviews)
    (output / "reports").mkdir(parents=True, exist_ok=True)
    (output / "reports" / "conversion_issues.txt").write_text(
        "\n".join(all_issues) + ("\n" if all_issues else ""), encoding="utf-8"
    )

    safe: dict[str, list[dict]] = {}
    provisional: dict[str, list[dict]] = {}
    by_source_summary: dict[str, dict] = {}
    for split in ("train", "dev", "test"):
        vi_med = all_examples["vimedner"][split]
        vi_bio = all_examples["vietbioner"][split]
        # Safe output deliberately uses the corpus that exhaustively covers all five labels.
        safe[split] = [item for item in vi_med if safe_for_tuning(item)]
        provisional[split] = vi_med + vi_bio
    for source, split_data in all_examples.items():
        by_source_summary[source] = {
            split: {"examples": len(items), "entities": entity_counts(items)}
            for split, items in split_data.items()
        }

    safe, safe_duplicates = deduplicate_splits(safe)
    provisional, provisional_duplicates = deduplicate_splits(provisional)

    summary = {
        "schema": sorted(MAIN_LABELS | (OPTIONAL_LABELS if args.include_optional else set())),
        "dictionary": str(dictionary) if dictionary else None,
        "lexicon_sizes": {"disease": len(disease_lexicon), "symptom": len(symptom_lexicon)},
        "sources": by_source_summary,
        "collections": {
            "safe": write_collection(output, "safe", safe, system_instruction),
            "combined_provisional": write_collection(
                output, "combined_provisional", provisional, system_instruction
            ),
        },
        "exact_duplicates_removed": {
            "safe": safe_duplicates,
            "combined_provisional": provisional_duplicates,
        },
        "review_items": len(all_reviews),
        "conversion_issues": len(all_issues),
        "warnings": [
            "combined_provisional is not gold until VietBioNER mappings and missing CAUSE/TREATMENT are reviewed",
            "safe excludes suspicious ViMedNer spans and uses only examples with exhaustive five-label coverage",
        ],
    }
    (output / "reports" / "dataset_stats.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
