#!/usr/bin/env python3
"""Build provisional Gold for pdf-001..pdf-010 and evaluate three saved systems."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from core.ner_experiment import LABELS, bio_text, entities_to_bio, tokenize  # noqa: E402


SOURCE_RUN_ID = "20260924-030109-053ff15b"
SOURCE_RUN = ROOT / "text" / "pdf_2021_experiment" / "runs" / SOURCE_RUN_ID
PILOT_ID = "pilot10-20260924-v1"
PILOT_DIR = ROOT / "text" / "pdf_2021_experiment" / "pilot10_gold" / "runs" / PILOT_ID
SYSTEM_FIELDS = {
    "ai_dictionary": "ai_dictionary_entities",
    "gold_lexicon": "gold_lexicon_entities",
    "combined": "combined_entities",
}
IOU_THRESHOLD = 0.5


# Pass 1 is deliberately authored from the abstract text, before consulting the
# prediction union. Every occurrence is annotated unless a longer curated span
# occupies the same characters (flat NER, longest span wins).
GOLD_TERMS: dict[str, list[tuple[str, str]]] = {
    "pdf-001": [
        ("tổn thương một hoặc nhiều dây", "DISEASE"),
        ("tổn thương thần kinh", "DISEASE"),
        ("Tổn thương hỗn hợp vận động và cảm giác", "DISEASE"),
        ("tổn thương rõ hỗn hợp myelin - sợi trục", "DISEASE"),
        ("tổn thương", "DISEASE"),
        ("giảm tốc độ dẫn truyền", "SYMPTOM"),
        ("giảm biên độ đáp ứng", "SYMPTOM"),
        ("mất chi phối thần kinh cơ", "SYMPTOM"),
        ("thăm dò chẩn đoán điện", "DIAGNOSTIC_PROCEDURE"),
        ("đo tốc độ dẫn truyền thần kinh", "DIAGNOSTIC_PROCEDURE"),
        ("khám lâm sàng thần kinh", "DIAGNOSTIC_PROCEDURE"),
        ("thang điểm Quick DASH", "DIAGNOSTIC_PROCEDURE"),
        ("chẩn đoán điện", "DIAGNOSTIC_PROCEDURE"),
        ("ghi điện cực kim", "DIAGNOSTIC_PROCEDURE"),
        ("hỏi bệnh", "DIAGNOSTIC_PROCEDURE"),
        ("khâu nối dây thần kinh", "TREATMENT"),
        ("nối không vi phẫu", "TREATMENT"),
        ("nối vi phẫu", "TREATMENT"),
        ("nối dây thần kinh", "TREATMENT"),
        ("nối thần kinh", "TREATMENT"),
    ],
    "pdf-002": [
        ("viêm khớp dạng thấp", "DISEASE"),
        ("viêm phổi kẽ", "DISEASE"),
        ("viêm với kẽ", "DISEASE"),
        ("nồng độ anti CCP huyết thanh", "DIAGNOSTIC_PROCEDURE"),
        ("nồng độ RF", "DIAGNOSTIC_PROCEDURE"),
    ],
    "pdf-003": [
        ("bệnh thận mạn", "DISEASE"),
        ("suy dinh dưỡng", "DISEASE"),
        ("thiếu máu", "DISEASE"),
        ("lọc máu có chu kỳ", "TREATMENT"),
        ("bộ công cụ NRS 2002", "DIAGNOSTIC_PROCEDURE"),
        ("sắt huyết thanh", "DIAGNOSTIC_PROCEDURE"),
        ("transferrin", "DIAGNOSTIC_PROCEDURE"),
        ("BMI", "DIAGNOSTIC_PROCEDURE"),
    ],
    "pdf-004": [
        ("thiếu năng lượng trường diễn", "DISEASE"),
        ("thừa cân – béo phì", "DISEASE"),
        ("thiếu vi chất dinh dưỡng", "DISEASE"),
        ("CED", "DISEASE"),
        ("TC-BP", "DISEASE"),
    ],
    "pdf-005": [
        ("thang đánh giá lo âu HADS (Hospital anxiety and depression scale)", "DIAGNOSTIC_PROCEDURE"),
        ("sàng lọc lo âu", "DIAGNOSTIC_PROCEDURE"),
        ("biện pháp sàng lọc", "DIAGNOSTIC_PROCEDURE"),
        ("đái tháo đường type 2", "DISEASE"),
        ("đái tháo đường", "DISEASE"),
        ("lo âu", "DISEASE"),
    ],
    "pdf-006": [
        ("bệnh bụi phổi silic", "DISEASE"),
        ("Bụi phổi silic", "DISEASE"),
        ("đồng nhiễm lao", "DISEASE"),
        ("đồng nhiễm các vi khuẩn khác", "DISEASE"),
        ("rối loạn chức năng hô hấp kiểu hạn chế và hỗn hợp", "DISEASE"),
        ("rối loạn chức năng hô hấp", "DISEASE"),
        ("suy giảm chức năng hô hấp", "SYMPTOM"),
        ("nốt mờ nhỏ", "SYMPTOM"),
        ("đám mờ lớn loại C", "SYMPTOM"),
        ("khó thở", "SYMPTOM"),
        ("ran nổ", "SYMPTOM"),
        ("ran ẩm", "SYMPTOM"),
        ("bụi silic", "CAUSE"),
    ],
    "pdf-007": [
        ("hội chứng chuyển hóa", "DISEASE"),
        ("loãng xương", "DISEASE"),
        ("Loãng xương", "DISEASE"),
        ("béo trung tâm", "DISEASE"),
    ],
    "pdf-008": [],
    "pdf-009": [
        ("Nhiễm khuẩn và ký sinh vật", "DISEASE"),
        ("Bệnh hô hấp", "DISEASE"),
        ("Một số bệnh xuất phát trong thời kỳ chu sinh", "DISEASE"),
        ("bệnh viêm phổi", "DISEASE"),
        ("viêm phổi", "DISEASE"),
        ("viêm phế quản và tiểu phế quản cấp", "DISEASE"),
        ("viêm phế quản", "DISEASE"),
        ("viêm tiểu phế quản cấp", "DISEASE"),
    ],
    "pdf-010": [
        ("bệnh phổi than thể biến chứng", "DISEASE"),
        ("bệnh bụi phổi than", "DISEASE"),
        ("đám mờ nhóm 1", "SYMPTOM"),
        ("đám mờ nhỏ kích thước p/p", "SYMPTOM"),
        ("nồng độ bụi cộng dồn", "CAUSE"),
    ],
}


NEEDS_REVIEW = [
    {"article_id": "pdf-001", "text": "tổn thương", "question": "Có nên mở rộng từng mention bằng cơ quan/dây thần kinh liên quan?", "provisional_decision": "INCLUDED_WITH_LONGEST_SPAN_RULE"},
    {"article_id": "pdf-002", "text": "hút thuốc lá", "question": "Yếu tố liên quan có đủ tiêu chí CAUSE hay chỉ là association?", "provisional_decision": "EXCLUDED_PENDING_HUMAN_REVIEW"},
    {"article_id": "pdf-002", "text": "viêm với kẽ", "question": "Giữ nguyên typo nguồn như một mention của viêm phổi kẽ?", "provisional_decision": "INCLUDED_AS_DISEASE_VERBATIM"},
    {"article_id": "pdf-003", "text": "sắt huyết thanh / transferrin / BMI", "question": "Schema coi tên chỉ số là diagnostic procedure hay chỉ tên xét nghiệm?", "provisional_decision": "INCLUDED_AS_DIAGNOSTIC_PROCEDURE"},
    {"article_id": "pdf-004", "text": "thừa cân – béo phì", "question": "Giữ một span phối hợp hay tách hai tình trạng?", "provisional_decision": "INCLUDED_AS_ONE_COORDINATED_SPAN"},
    {"article_id": "pdf-005", "text": "lo âu", "question": "Trong nghiên cứu này là DISEASE hay SYMPTOM?", "provisional_decision": "INCLUDED_AS_DISEASE"},
    {"article_id": "pdf-005", "text": "điều trị đái tháo đường nội trú", "question": "Mô tả hình thức điều trị chung có được gán TREATMENT?", "provisional_decision": "EXCLUDED_PENDING_HUMAN_REVIEW"},
    {"article_id": "pdf-006", "text": "đồng nhiễm các vi khuẩn khác", "question": "Mention không nêu tác nhân cụ thể có đủ tiêu chí DISEASE?", "provisional_decision": "INCLUDED_AS_DISEASE"},
    {"article_id": "pdf-007", "text": "béo trung tâm", "question": "DISEASE hay dấu hiệu/thành phần hội chứng?", "provisional_decision": "INCLUDED_AS_DISEASE"},
    {"article_id": "pdf-009", "text": "Nhiễm khuẩn và ký sinh vật / Bệnh hô hấp", "question": "Nhóm chương ICD tổng quát có được gán DISEASE?", "provisional_decision": "INCLUDED_AS_DISEASE"},
    {"article_id": "pdf-010", "text": "hút thuốc lá", "question": "Không có phát biểu quan hệ nhân quả trực tiếp; có gán CAUSE?", "provisional_decision": "EXCLUDED_PENDING_HUMAN_REVIEW"},
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8", newline="\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def all_mentions(text: str, phrase: str, label: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    cursor = 0
    haystack = text.casefold()
    needle = phrase.casefold()
    while True:
        start = haystack.find(needle, cursor)
        if start < 0:
            break
        result.append({"text": text[start:start + len(phrase)], "label": label, "start": start, "end": start + len(phrase)})
        cursor = start + len(phrase)
    return result


def flat_longest(entities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(entities, key=lambda item: (-(item["end"] - item["start"]), item["start"], LABELS.index(item["label"])))
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for item in ordered:
        key = (item["start"], item["end"], item["label"])
        if key in seen:
            continue
        seen.add(key)
        conflict = next((old for old in kept if item["start"] < old["end"] and old["start"] < item["end"]), None)
        if conflict:
            dropped.append({"type": "FLAT_OVERLAP_DROPPED", "entity": item, "kept": conflict})
        else:
            kept.append(item)
    return sorted(kept, key=lambda item: (item["start"], item["end"], item["label"])), dropped


def build_gold(articles: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for article in articles:
        article_id = article["article_id"]
        text = article["abstract"]
        candidates: list[dict[str, Any]] = []
        for phrase, label in GOLD_TERMS[article_id]:
            matches = all_mentions(text, phrase, label)
            if not matches:
                raise ValueError(f"{article_id}: không tìm thấy phrase {phrase!r}")
            candidates.extend(matches)
        entities, dropped = flat_longest(candidates)
        for entity in entities:
            if text[entity["start"]:entity["end"]] != entity["text"]:
                raise ValueError(f"{article_id}: offset không khôi phục được {entity}")
        audit.extend({"article_id": article_id, **item} for item in dropped)
        rows.append({
            "id": article_id,
            "article_id": article_id,
            "title": article["title"],
            "input_text": text,
            "abstract_sha256": article["abstract_sha256"],
            "entities": entities,
            "gold_status": "PROVISIONAL_AI_ASSISTED",
            "annotation_passes": ["BLIND_TEXT_FIRST_PASS", "CONSISTENCY_AND_PREDICTION_UNION_AUDIT"],
        })
    return rows, audit


def iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    intersection = max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))
    union = max(left["end"], right["end"]) - min(left["start"], right["start"])
    return intersection / union if intersection and union else 0.0


def maximum_pairs(predicted: list[dict[str, Any]], gold: list[dict[str, Any]], relaxed: bool) -> list[tuple[int, int]]:
    edges: list[list[tuple[float, int]]] = []
    for pred in predicted:
        values: list[tuple[float, int]] = []
        for index, target in enumerate(gold):
            if pred["label"] != target["label"]:
                continue
            score = iou(pred, target)
            valid = score >= IOU_THRESHOLD if relaxed else (pred["start"], pred["end"]) == (target["start"], target["end"])
            if valid:
                values.append((score, index))
        edges.append(sorted(values, reverse=True))
    right_match = [-1] * len(gold)

    def augment(pred_index: int, seen: set[int]) -> bool:
        for _, gold_index in edges[pred_index]:
            if gold_index in seen:
                continue
            seen.add(gold_index)
            if right_match[gold_index] < 0 or augment(right_match[gold_index], seen):
                right_match[gold_index] = pred_index
                return True
        return False

    for pred_index in range(len(predicted)):
        augment(pred_index, set())
    return [(pred_index, gold_index) for gold_index, pred_index in enumerate(right_match) if pred_index >= 0]


def metric(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def evaluate_system(gold_rows: list[dict[str, Any]], prediction_rows: list[dict[str, Any]], relaxed: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    predictions = {row["article_id"]: row["entities"] for row in prediction_rows}
    counts = {label: [0, 0, 0] for label in LABELS}
    errors: list[dict[str, Any]] = []
    per_article: list[dict[str, Any]] = []
    for gold_row in gold_rows:
        article_id = gold_row["article_id"]
        gold = gold_row["entities"]
        predicted = predictions.get(article_id, [])
        pairs = maximum_pairs(predicted, gold, relaxed)
        used_pred = {pred_index for pred_index, _ in pairs}
        used_gold = {gold_index for _, gold_index in pairs}
        per_article.append({"article_id": article_id, "tp": len(pairs), "fp": len(predicted) - len(pairs), "fn": len(gold) - len(pairs)})
        for label in LABELS:
            tp = sum(predicted[pred_index]["label"] == label for pred_index, _ in pairs)
            pred_total = sum(item["label"] == label for item in predicted)
            gold_total = sum(item["label"] == label for item in gold)
            counts[label][0] += tp
            counts[label][1] += pred_total - tp
            counts[label][2] += gold_total - tp

        unmatched_pred = [index for index in range(len(predicted)) if index not in used_pred]
        unmatched_gold = [index for index in range(len(gold)) if index not in used_gold]
        claimed_pred: set[int] = set()
        claimed_gold: set[int] = set()
        overlap_candidates: list[tuple[float, int, int]] = []
        for pred_index in unmatched_pred:
            for gold_index in unmatched_gold:
                score = iou(predicted[pred_index], gold[gold_index])
                if score > 0:
                    overlap_candidates.append((score, pred_index, gold_index))
        for score, pred_index, gold_index in sorted(overlap_candidates, reverse=True):
            if pred_index in claimed_pred or gold_index in claimed_gold:
                continue
            pred, target = predicted[pred_index], gold[gold_index]
            error_type = "BOUNDARY_ERROR" if pred["label"] == target["label"] else "LABEL_ERROR"
            errors.append({"article_id": article_id, "type": error_type, "predicted": pred, "gold": target, "iou": score})
            claimed_pred.add(pred_index)
            claimed_gold.add(gold_index)
        for pred_index in unmatched_pred:
            if pred_index not in claimed_pred:
                errors.append({"article_id": article_id, "type": "FP", "predicted": predicted[pred_index], "gold": None, "iou": 0.0})
        for gold_index in unmatched_gold:
            if gold_index not in claimed_gold:
                errors.append({"article_id": article_id, "type": "FN", "predicted": None, "gold": gold[gold_index], "iou": 0.0})

    per_label = {label: metric(*counts[label]) for label in LABELS}
    totals = [sum(counts[label][index] for label in LABELS) for index in range(3)]
    macro = {key: sum(per_label[label][key] for label in LABELS) / len(LABELS) for key in ("precision", "recall", "f1")}
    return {"per_label": per_label, "micro": metric(*totals), "macro": macro, "per_article": per_article}, errors


def write_bio_and_brat(gold_rows: list[dict[str, Any]]) -> None:
    bio_chunks: list[str] = []
    brat_dir = PILOT_DIR / "brat"
    brat_dir.mkdir(parents=True, exist_ok=True)
    for row in gold_rows:
        tokens, issues = entities_to_bio(row["input_text"], row["entities"])
        if issues:
            raise ValueError(f"{row['article_id']}: BIO issues: {issues}")
        bio_chunks.append(bio_text(tokens))
        (brat_dir / f"{row['article_id']}.txt").write_text(row["input_text"], encoding="utf-8")
        ann = [f"T{index}\t{entity['label']} {entity['start']} {entity['end']}\t{entity['text']}" for index, entity in enumerate(row["entities"], start=1)]
        (brat_dir / f"{row['article_id']}.ann").write_text("\n".join(ann) + ("\n" if ann else ""), encoding="utf-8")
    (PILOT_DIR / "gold.provisional.txt").write_text("".join(bio_chunks), encoding="utf-8", newline="\n")


def prediction_union_audit(gold_rows: list[dict[str, Any]], predictions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    gold_by_id = {row["article_id"]: row["entities"] for row in gold_rows}
    review_text = {(row["article_id"], row["text"].casefold()) for row in NEEDS_REVIEW}
    audit: list[dict[str, Any]] = []
    for article_id, prediction in predictions.items():
        if article_id not in gold_by_id:
            continue
        union: dict[tuple[int, int, str], dict[str, Any]] = {}
        for system, field in SYSTEM_FIELDS.items():
            for entity in prediction[field]:
                key = (entity["start"], entity["end"], entity["label"])
                if key not in union:
                    union[key] = {**entity, "systems": []}
                union[key]["systems"].append(system)
        gold = gold_by_id[article_id]
        gold_keys = {(entity["start"], entity["end"], entity["label"]) for entity in gold}
        for key, entity in sorted(union.items()):
            if key in gold_keys:
                continue
            overlaps = [target for target in gold if entity["start"] < target["end"] and target["start"] < entity["end"]]
            if any(target["label"] == entity["label"] for target in overlaps):
                category = "BOUNDARY_CANDIDATE"
            elif overlaps:
                category = "LABEL_CANDIDATE"
            else:
                category = "MISSING_FROM_GOLD_CANDIDATE"
            needs_human = (article_id, entity["text"].casefold()) in review_text
            audit.append({
                "article_id": article_id,
                "category": category,
                "prediction": entity,
                "overlapping_gold": overlaps,
                "audit_status": "NEEDS_HUMAN_REVIEW" if needs_human else "REVIEWED_NOT_ADDED",
                "reason": "Listed in needs_review.jsonl" if needs_human else "Excluded by provisional guideline or represented by the selected flat span",
            })
    return audit


def main() -> int:
    PILOT_DIR.mkdir(parents=True, exist_ok=True)
    articles = read_jsonl(SOURCE_RUN / "input" / "articles.jsonl")[:10]
    if [row["article_id"] for row in articles] != [f"pdf-{index:03d}" for index in range(1, 11)]:
        raise ValueError("Pilot không còn là pdf-001..pdf-010")
    predictions = {row["article_id"]: row for row in read_jsonl(SOURCE_RUN / "predictions.jsonl")}
    gold_rows, overlap_audit = build_gold(articles)
    write_jsonl(PILOT_DIR / "input.10.jsonl", articles)
    write_jsonl(PILOT_DIR / "gold.provisional.jsonl", gold_rows)
    write_jsonl(PILOT_DIR / "annotation_overlap_audit.jsonl", overlap_audit)
    write_jsonl(PILOT_DIR / "needs_review.jsonl", NEEDS_REVIEW)
    write_bio_and_brat(gold_rows)
    write_jsonl(PILOT_DIR / "prediction_union_audit.jsonl", prediction_union_audit(gold_rows, predictions))

    guideline = """# Guideline Gold sơ bộ pilot 10 bài

Gold sử dụng NER phẳng với năm nhãn DISEASE, SYMPTOM, CAUSE, DIAGNOSTIC_PROCEDURE và TREATMENT.

- Gán mọi lần xuất hiện của một thực thể, kể cả mention bị phủ định hoặc nằm trong mô tả nghiên cứu.
- Giữ nguyên văn bản và offset nguồn; không sửa typo/OCR trong span.
- Chọn span dài nhất biểu đạt trọn khái niệm. Không tạo hai entity chồng lấn.
- DISEASE: bệnh, hội chứng hoặc tình trạng bệnh lý đang được nhắc đến.
- SYMPTOM: triệu chứng, dấu hiệu lâm sàng hoặc bất thường chức năng/hình ảnh.
- CAUSE: tác nhân hay phơi nhiễm được mô tả có quan hệ gây bệnh; association chưa rõ được đưa vào Needs Review.
- DIAGNOSTIC_PROCEDURE: khám, thang đo, xét nghiệm, chỉ số xét nghiệm hoặc thăm dò chẩn đoán cụ thể.
- TREATMENT: thuốc, thủ thuật hoặc phương pháp điều trị cụ thể; không gán từ “điều trị” đứng chung chung.
- Tên cơ sở, tác giả, thời gian, thiết kế nghiên cứu và thuật ngữ quản lý y tế không thuộc schema.

Trạng thái `PROVISIONAL_AI_ASSISTED` phải được giữ cho đến khi người dùng/chuyên gia xử lý hết Needs Review.
"""
    (PILOT_DIR / "annotation_guideline.md").write_text(guideline, encoding="utf-8")

    prediction_dir = PILOT_DIR / "predictions"
    prediction_rows: dict[str, list[dict[str, Any]]] = {}
    for system, field in SYSTEM_FIELDS.items():
        rows = [{"id": article["article_id"], "article_id": article["article_id"], "entities": predictions[article["article_id"]][field]} for article in articles]
        prediction_rows[system] = rows
        write_jsonl(prediction_dir / f"{system}.jsonl", rows)

    results: dict[str, Any] = {}
    all_errors: list[dict[str, Any]] = []
    for system, rows in prediction_rows.items():
        exact, exact_errors = evaluate_system(gold_rows, rows, relaxed=False)
        relaxed, relaxed_errors = evaluate_system(gold_rows, rows, relaxed=True)
        results[system] = {"exact": exact, "relaxed": relaxed}
        all_errors.extend({"system": system, "mode": "exact", **error} for error in exact_errors)
        all_errors.extend({"system": system, "mode": "relaxed", **error} for error in relaxed_errors)
        article_dir = PILOT_DIR / "article_reports" / system
        article_dir.mkdir(parents=True, exist_ok=True)
        for article in articles:
            article_errors = [error for error in exact_errors if error["article_id"] == article["article_id"]]
            (article_dir / f"{article['article_id']}.json").write_text(json.dumps(article_errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    self_test, self_errors = evaluate_system(gold_rows, [{"article_id": row["article_id"], "entities": row["entities"]} for row in gold_rows], relaxed=False)
    if self_test["micro"]["f1"] != 1.0 or self_errors:
        raise AssertionError("Self-test gold=prediction không đạt F1=1")

    (PILOT_DIR / "metrics.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_jsonl(PILOT_DIR / "errors.jsonl", all_errors)
    with (PILOT_DIR / "metrics.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["system", "mode", "label", "tp", "fp", "fn", "precision", "recall", "f1"])
        for system, modes in results.items():
            for mode, result in modes.items():
                for label, values in result["per_label"].items():
                    writer.writerow([system, mode, label, *[values[key] for key in ("tp", "fp", "fn", "precision", "recall", "f1")]])
                writer.writerow([system, mode, "MICRO", *[result["micro"][key] for key in ("tp", "fp", "fn", "precision", "recall", "f1")]])
                writer.writerow([system, mode, "MACRO", "", "", "", result["macro"]["precision"], result["macro"]["recall"], result["macro"]["f1"]])
    with (PILOT_DIR / "errors.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["system", "mode", "article_id", "type", "predicted", "gold", "iou"])
        for row in all_errors:
            writer.writerow([row["system"], row["mode"], row["article_id"], row["type"], json.dumps(row.get("predicted"), ensure_ascii=False), json.dumps(row.get("gold"), ensure_ascii=False), row.get("iou")])

    gold_counts = Counter(entity["label"] for row in gold_rows for entity in row["entities"])
    manifest = {
        "pilot_id": PILOT_ID,
        "source_run_id": SOURCE_RUN_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gold_status": "PROVISIONAL_AI_ASSISTED",
        "article_ids": [row["article_id"] for row in articles],
        "article_checksums": {row["article_id"]: row["abstract_sha256"] for row in articles},
        "labels": list(LABELS),
        "flat_ner": True,
        "overlap_rule": "LONGEST_SPAN_THEN_LABEL_ORDER",
        "exact_match": "same start, end and label",
        "relaxed_match": f"same label and IoU >= {IOU_THRESHOLD}",
        "gold_entities_by_label": {label: gold_counts[label] for label in LABELS},
        "gold_entity_total": sum(gold_counts.values()),
        "needs_review_count": len(NEEDS_REVIEW),
        "self_test_exact_micro_f1": self_test["micro"]["f1"],
        "input_checksum": sha256_text("".join(row["abstract_sha256"] for row in articles)),
    }
    (PILOT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Pilot 10 bài: Gold sơ bộ và đánh giá NER", "",
        "> Gold này do AI hỗ trợ và còn danh sách Needs Review; chưa phải Gold chuyên gia dùng cho số liệu công bố.", "",
        f"- Tổng Gold entity: **{manifest['gold_entity_total']}**", f"- Trường hợp cần xác nhận: **{len(NEEDS_REVIEW)}**", "",
        "| Hệ thống | Chế độ | Macro-F1 | Micro-P | Micro-R | Micro-F1 | TP | FP | FN |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for system, modes in results.items():
        for mode, result in modes.items():
            micro = result["micro"]
            lines.append(f"| {system} | {mode} | {result['macro']['f1']:.4f} | {micro['precision']:.4f} | {micro['recall']:.4f} | {micro['f1']:.4f} | {micro['tp']} | {micro['fp']} | {micro['fn']} |")
    (PILOT_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"pilot_dir": str(PILOT_DIR), "manifest": manifest, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
