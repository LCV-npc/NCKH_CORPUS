import html
import re

from core.ner_engine import run_ner


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
    found = entities("Người bệnh tha thuốc nhưng có THA.")
    matches = [item for item in found if item["icd_code"] == "I10"]
    assert [item["text"] for item in matches] == ["THA"]
    assert matches[0]["matched_by"] == "abbreviation"


def test_entities_in_parentheses_are_kept():
    found = entities("Bệnh nền (tăng huyết áp) đang được theo dõi.")
    assert any(item["text"] == "tăng huyết áp" for item in found)


def test_controlled_multiword_accentless_alias_matches():
    found = entities("Benh nhan tang huyet ap.")
    assert any(item["text"] == "tang huyet ap" and item["icd_code"] == "I10" for item in found)


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
