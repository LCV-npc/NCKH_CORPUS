from pathlib import Path

from core.ner_evaluation import evaluate_system
from core.ner_experiment import (
    CURRENT_LABEL_MAP,
    NerExperimentManager,
    align_surface_entities,
    entities_to_bio,
    resolve_overlaps,
    tokenize,
)


def test_current_mapping_six_to_five_schema():
    assert CURRENT_LABEL_MAP["Bệnh lý"] == "DISEASE"
    assert CURRENT_LABEL_MAP["Triệu chứng"] == "SYMPTOM"
    assert CURRENT_LABEL_MAP["Điều trị"] == "TREATMENT"
    assert CURRENT_LABEL_MAP["Xét nghiệm"] == "DIAGNOSTIC_PROCEDURE"
    assert CURRENT_LABEL_MAP["Hình ảnh"] == "DIAGNOSTIC_PROCEDURE"
    assert CURRENT_LABEL_MAP["Sinh lý"] is None


def test_vietnamese_punctuation_offsets_and_bio_round_trip():
    text = "Bệnh nhân sốt, đau ngực; chụp X-quang."
    entities = [
        {"text": "sốt", "label": "SYMPTOM", "start": 10, "end": 13},
        {"text": "đau ngực", "label": "SYMPTOM", "start": 15, "end": 23},
        {"text": "chụp X-quang", "label": "DIAGNOSTIC_PROCEDURE", "start": 25, "end": 37},
    ]
    tokens, issues = entities_to_bio(text, entities)
    assert issues == []
    assert "".join(text[token["start"]:token["end"]] for token in tokens if token["tag"] == "B-SYMPTOM") == "sốtđau"
    assert [token["tag"] for token in tokens if token["text"] in {"chụp", "X-quang"}] == [
        "B-DIAGNOSTIC_PROCEDURE", "I-DIAGNOSTIC_PROCEDURE"
    ]


def test_repeated_surface_is_aligned_in_order():
    text = "Sốt giảm rồi sốt trở lại."
    entities, issues = align_surface_entities(text, [
        {"text": "sốt", "label": "SYMPTOM"},
        {"text": "sốt", "label": "SYMPTOM"},
    ])
    assert issues == []
    assert [(item["start"], item["end"]) for item in entities] == [(0, 3), (13, 16)]


def test_longest_span_wins_and_conflict_is_logged():
    kept, issues = resolve_overlaps([
        {"text": "ung thư", "label": "DISEASE", "start": 0, "end": 7},
        {"text": "ung thư phổi", "label": "DISEASE", "start": 0, "end": 12},
    ])
    assert [item["text"] for item in kept] == ["ung thư phổi"]
    assert issues[0]["type"] == "OVERLAP_DROPPED"


def test_resume_completed_ids_does_not_repeat_articles(tmp_path: Path):
    prediction = tmp_path / "predictions.jsonl"
    prediction.write_text('{"article_id":1}\n{"article_id":2}\n', encoding="utf-8")
    assert NerExperimentManager()._completed_ids(prediction) == {1, 2}


def test_self_evaluation_is_perfect():
    entities = [{"text": "viêm phổi", "label": "DISEASE", "start": 0, "end": 10}]
    gold = [{"article_id": 1, "abstract": "viêm phổi", "entities": entities}]
    predicted = [{"article_id": 1, "entities": entities}]
    result, errors = evaluate_system(gold, predicted, relaxed=False)
    assert result["micro"]["f1"] == 1.0
    assert result["micro"]["tp"] == 1
    assert errors == []


def test_error_analysis_classifies_boundary_label_fp_and_fn():
    gold = [{"article_id": 1, "entities": [
        {"text": "đau ngực", "label": "SYMPTOM", "start": 0, "end": 8},
        {"text": "lao", "label": "DISEASE", "start": 20, "end": 23},
        {"text": "sốt", "label": "SYMPTOM", "start": 30, "end": 33},
    ]}]
    predicted = [{"article_id": 1, "entities": [
        {"text": "đau", "label": "SYMPTOM", "start": 0, "end": 3},
        {"text": "lao", "label": "SYMPTOM", "start": 20, "end": 23},
        {"text": "ho", "label": "SYMPTOM", "start": 40, "end": 42},
    ]}]
    _, errors = evaluate_system(gold, predicted, relaxed=False)
    kinds = {item["type"] for item in errors}
    assert {"BOUNDARY_ERROR", "LABEL_ERROR", "FP", "FN"} <= kinds

