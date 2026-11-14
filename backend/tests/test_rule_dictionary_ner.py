import html
import re

from core.ner_engine import run_ner
from core.ner_dict import get_snapshot, load_runtime_aliases


def entities(text):
    return run_ner(text, enable_tone_restore=False, enable_noun_phrase=False)[2]


def test_offsets_always_slice_the_original_text():
    text = "Bệnh nhân tăng huyết áp, đái tháo đường và bệnh gút."
    found = entities(text)
    assert found
    assert all(text[item["start"]:item["end"]] == item["text"] for item in found)


def test_output_is_deterministic():
    text = "Tăng huyết áp kèm đái tháo đường và tăng huyết áp."
    first = run_ner(text)
    second = run_ner(text)
    assert first == second


def test_case_sensitive_abbreviation_does_not_match_common_lowercase_word():
    load_runtime_aliases([
        {
            "surface_form": "THA",
            "concept_id": "ICD10:I10:base",
            "concept_code": "I10",
            "canonical_term": "Bệnh lý tăng huyết áp",
            "alias_type": "abbreviation",
            "case_sensitive": True,
        }
    ])
    try:
        found = entities("Người bệnh tha thuốc nhưng có THA.")
        matches = [item for item in found if item["icd_code"] == "I10"]
        assert [item["text"] for item in matches] == ["THA"]
        assert matches[0]["matched_by"] == "abbreviation"
    finally:
        load_runtime_aliases([])


def test_entities_in_parentheses_are_kept():
    found = entities("Bệnh nền (tăng huyết áp) đang được theo dõi.")
    assert any(item["text"] == "tăng huyết áp" for item in found)


def test_controlled_multiword_accentless_alias_matches():
    found = entities("Benh nhan tang huyet ap.")
    assert any(item["text"] == "tang huyet ap" and item["icd_code"] == "I10" for item in found)


def test_controlled_grammar_variant_is_generated_from_json():
    canonical = "Các thể khác của nhiễm actinomyces"
    variant = "thể khác của nhiễm actinomyces"
    found = entities(f"Người bệnh có {variant}.")
    match = next(item for item in found if item["text"] == variant)
    assert match["icd_code"] == "A428"
    assert match["concept_id"] == "ICD10:A428:base"
    assert match["canonical_term"] == canonical
    assert match["matched_by"] == "grammar_variant"


def test_pipeline_never_returns_noun_phrase_or_fuzzy_entities():
    _, _, found, log = run_ner("Cơ chế mới lạ chưa có trong từ điển.", enable_noun_phrase=True)
    assert all(item["matched_by"] not in {"noun_phrase", "fuzzy"} for item in found)
    assert log["np_count"] == 0


def test_highlight_html_preserves_plain_source_and_escapes_markup():
    text = "<b>Tăng huyết áp</b> & đái tháo đường"
    highlighted, _, found, _ = run_ner(text)
    plain = re.sub(r"<[^>]+>", "", highlighted)
    assert html.unescape(plain) == text
    assert "&lt;b&gt;" in highlighted and "&amp;" in highlighted
    assert all(text[item["start"]:item["end"]] == item["text"] for item in found)


def test_approved_runtime_alias_maps_to_same_icd_concept():
    alias = "Rối loạn trương lực cơ khác của trẻ sơ sinh"
    load_runtime_aliases([
        {
            "surface_form": alias,
            "concept_id": "P948",
            "canonical_term": "Các rối loạn trương lực cơ khác của trẻ sơ sinh",
            "concept_code": "P948",
            "alias_type": "grammar_variant",
        }
    ])
    try:
        found = entities(f"Có {alias}.")
        assert any(
            item["text"] == alias
            and item["icd_code"] == "P948"
            and item["matched_by"] == "grammar_variant"
            for item in found
        )
    finally:
        load_runtime_aliases([])


def test_confirmed_entity_exposes_stable_concept_id():
    found = entities("Bệnh lý tăng huyết áp.")
    match = next(item for item in found if item["icd_code"] == "I10")
    assert match["concept_id"] == "ICD10:I10:base"
    assert match["concept_id_source"] == "ICD10"
    assert match["confidence"] == 1.0


def test_fuzzy_candidate_is_review_only_and_never_highlighted():
    highlighted, _, found, log = run_ner("Bệnh tim do tăng huyet áp.")
    assert found == []
    assert "<mark" not in highlighted
    assert any(item["matched_by"] == "fuzzy_candidate" for item in log["suggestions"])
    assert all(item["review_state"] == "UNCERTAIN" for item in log["suggestions"])


def test_ambiguous_surface_form_is_not_auto_labeled():
    snapshot = get_snapshot()
    key = next(
        key
        for key, values in snapshot.term_candidates.items()
        if key not in snapshot.term_dict and len(key.split()) >= 2 and len(values) > 1
    )
    _, _, found, log = run_ner(key)
    assert found == []
    assert any(item["matched_by"] == "ambiguous" for item in log["suggestions"])
