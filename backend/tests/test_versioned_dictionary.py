import hashlib
import json
from pathlib import Path

from core.ner_dict import DATA_DIR, DICT_DIR, MANIFEST_PATH, PROJECT_DIR
from core.ner_engine import run_ner


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_hashes_counts_and_versions_are_consistent():
    manifest = _load(MANIFEST_PATH)
    for key, spec in manifest["files"].items():
        path = (PROJECT_DIR / spec["path"]) if key == "custom" else (DICT_DIR / spec["path"])
        payload = _load(path)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == spec["sha256"]
        assert len(payload["entries"]) == spec["count"]
        assert payload["dictionary_version"] == manifest["dictionary_version"]


def test_versioned_artifacts_match_their_original_sources_in_data():
    """Never silently run NER against a snapshot older than data/ sources."""
    for filename, artifact in (
        ("icd10_v1.json", "phu-lu-c-1-danh-mu-c-icd-10-thay-the-dmdc-phie-n-ba-n-6.xlsx"),
        ("yhct_v1.json", "PhuLuc1.pdf"),
    ):
        payload = _load(DICT_DIR / filename)
        source_path = DATA_DIR / artifact
        assert source_path.is_file()
        assert payload["source"]["file"] == artifact
        assert hashlib.sha256(source_path.read_bytes()).hexdigest() == payload["source"]["sha256"]


def test_rule_engine_matches_terms_from_each_validated_dictionary_source():
    """Smoke test real source terms, not manually invented keyword lists."""
    fixtures = []
    for filename, expected_source, record_type in (
        ("icd10_v1.json", "icd10", None),
        ("yhct_v1.json", "yhct", "base"),
    ):
        entries = _load(DICT_DIR / filename)["entries"]
        entry = next(
            item for item in entries
            if item.get("active_for_ner")
            and not item.get("ambiguous")
            and (record_type is None or item.get("record_type") == record_type)
        )
        fixtures.append((entry["canonical_term"], entry["code"], expected_source))

    for term, expected_code, expected_source in fixtures:
        _, _, entities, _ = run_ner(term, enable_tone_restore=False, enable_noun_phrase=False)
        assert any(
            item["icd_code"] == expected_code
            and item["source"] == expected_source
            and item["text"] == term
            for item in entities
        )


def test_icd10_source_integrity_counts():
    payload = _load(DICT_DIR / "icd10_v1.json")
    assert payload["counts"] == {
        "source_rows": 12219,
        "unique_compact_codes": 12137,
        "dagger": 82,
        "asterisk": 776,
        "active_for_ner": 10653,
    }
    active = [entry for entry in payload["entries"] if entry["active_for_ner"]]
    assert {entry["category"] for entry in active} <= {"Bệnh Lý", "Triệu Chứng"}


def test_yhct_codes_are_complete_and_generic_other_forms_are_inactive():
    payload = _load(DICT_DIR / "yhct_v1.json")
    entries = payload["entries"]
    assert payload["counts"] == {"all": 228, "base": 38, "clinical_forms": 190}
    assert [int(item["common_code"]) for item in entries] == list(range(6500000, 6500228))
    for item in entries:
        if item["canonical_term"].casefold() == "thể khác":
            assert item["active_for_ner"] is False
            assert item["ambiguous"] is True


def test_ambiguous_custom_aliases_are_not_active():
    manifest = _load(MANIFEST_PATH)
    payload = _load(PROJECT_DIR / manifest["files"]["custom"]["path"])
    by_term = {entry["term"].casefold(): entry for entry in payload["entries"]}
    for term in ("giang mai", "u ác tính", "tổn thương"):
        assert by_term[term]["active_for_ner"] is False
        assert by_term[term]["ambiguous"] is True
