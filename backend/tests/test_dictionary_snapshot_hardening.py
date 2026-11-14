"""Hardening tests for immutable dictionary snapshots and provenance."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from unittest.mock import patch

import core.ner_dict as ner_dict
from core.ner_engine import run_ner


class DictionarySnapshotHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        # Each test starts from the base JSON dictionary.  The production
        # loader still publishes a new generation; this prevents an alias
        # created by one test from leaking into the next test.
        if ner_dict.get_snapshot().metadata.get("explicit_aliases"):
            ner_dict.load_runtime_aliases([])

    def tearDown(self) -> None:
        if ner_dict.get_snapshot().metadata.get("explicit_aliases"):
            ner_dict.load_runtime_aliases([])

    def test_snapshot_indexes_and_metadata_are_immutable(self):
        snapshot = ner_dict.get_snapshot()
        with self.assertRaises(TypeError):
            snapshot.term_dict["__test__"] = {}  # type: ignore[index]
        with self.assertRaises(TypeError):
            snapshot.term_dict[next(iter(snapshot.term_dict))]["code"] = "X"  # type: ignore[index]
        with self.assertRaises(TypeError):
            snapshot.metadata["snapshot_generation"] = 999  # type: ignore[index]

    def test_reload_failure_keeps_the_published_snapshot(self):
        before = ner_dict.get_snapshot()
        with patch.object(
            ner_dict,
            "_build_snapshot",
            side_effect=ner_dict.DictionaryValidationError("checksum test failure"),
        ):
            with self.assertRaises(ner_dict.DictionaryValidationError):
                ner_dict.reload_ner_dictionary()
        after = ner_dict.get_snapshot()
        self.assertIs(after, before)
        self.assertEqual(after.generation, before.generation)
        status = ner_dict.get_dictionary_runtime_status()
        self.assertTrue(status["available"])
        self.assertIn("checksum test failure", status["last_load_error"])
        self.assertIn("checksum test failure", status["last_reload_error"])
        self.assertTrue(run_ner("Bệnh lý tăng huyết áp")[2])

    def test_first_load_failure_is_unavailable(self):
        with ner_dict._snapshot_lock:
            previous = ner_dict._snapshot
            ner_dict._snapshot = None
        try:
            with patch.object(
                ner_dict,
                "_build_snapshot",
                side_effect=ner_dict.DictionaryValidationError("schema test failure"),
            ):
                with self.assertRaises(ner_dict.DictionaryUnavailableError):
                    ner_dict.get_snapshot()
        finally:
            with ner_dict._snapshot_lock:
                ner_dict._snapshot = previous

    def test_alias_metadata_and_case_sensitivity_reach_runtime(self):
        generation_before = ner_dict.get_snapshot().generation
        ner_dict.load_runtime_aliases(
            [
                {
                    "surface_form": "THA",
                    "concept_id": "ICD10:I10:base",
                    "concept_code": "I10",
                    "canonical_term": "Bệnh lý tăng huyết áp",
                    "alias_type": "abbreviation",
                    "case_sensitive": True,
                    "allow_accentless": False,
                    "status": "APPROVED",
                }
            ]
        )
        snapshot = ner_dict.get_snapshot()
        self.assertGreater(snapshot.generation, generation_before)
        alias_info = snapshot.term_candidates["tha"][-1]
        self.assertEqual(alias_info["alias_type"], "abbreviation")
        self.assertTrue(alias_info["case_sensitive"])
        self.assertFalse(alias_info["allow_accentless"])

        _, _, entities, _ = run_ner("tha THA")
        self.assertEqual([item["text"] for item in entities if item["icd_code"] == "I10"], ["THA"])
        self.assertEqual(
            [item["matched_by"] for item in entities if item["icd_code"] == "I10"],
            ["abbreviation"],
        )

    def test_object_alias_defaults_to_abbreviation_contract(self):
        term, metadata = ner_dict._alias_values({"term": "THA"})
        self.assertEqual(term, "THA")
        self.assertEqual(metadata["alias_type"], "abbreviation")
        self.assertTrue(metadata["case_sensitive"])
        self.assertFalse(metadata["allow_accentless"])

    def test_allow_accentless_flag_is_honored_for_case_sensitive_alias(self):
        ner_dict.load_runtime_aliases(
            [
                {
                    "surface_form": "TĂNGHA",
                    "concept_id": "ICD10:I10:base",
                    "concept_code": "I10",
                    "canonical_term": "Bệnh lý tăng huyết áp",
                    "alias_type": "abbreviation",
                    "case_sensitive": True,
                    "allow_accentless": True,
                    "status": "APPROVED",
                }
            ]
        )
        _, _, entities, _ = run_ner("TANGHA")
        self.assertEqual([item["text"] for item in entities], ["TANGHA"])
        self.assertEqual(entities[0]["matched_by"], "accentless")

    def test_alias_cannot_replace_a_canonical_surface(self):
        ner_dict.load_runtime_aliases(
            [
                {
                    "surface_form": "Bệnh lý tăng huyết áp",
                    "concept_id": "ICD10:I10:base",
                    "concept_code": "I10",
                    "canonical_term": "Bệnh lý tăng huyết áp",
                    "alias_type": "abbreviation",
                    "case_sensitive": True,
                    "status": "APPROVED",
                }
            ]
        )
        snapshot = ner_dict.get_snapshot()
        info = snapshot.term_dict["bệnh lý tăng huyết áp"]
        self.assertEqual(info["alias_type"], "canonical")
        self.assertEqual(run_ner("Bệnh lý tăng huyết áp")[2][0]["matched_by"], "exact")

    def test_pending_and_rejected_aliases_do_not_change_runtime_hash(self):
        before = ner_dict.get_snapshot()
        ner_dict.load_runtime_aliases(
            [
                {
                    "surface_form": "PENDING_THA",
                    "concept_id": "ICD10:I10:base",
                    "concept_code": "I10",
                    "canonical_term": "Bệnh lý tăng huyết áp",
                    "alias_type": "abbreviation",
                    "status": "PENDING_REVIEW",
                },
                {
                    "surface_form": "REJECTED_THA",
                    "concept_id": "ICD10:I10:base",
                    "concept_code": "I10",
                    "canonical_term": "Bệnh lý tăng huyết áp",
                    "alias_type": "abbreviation",
                    "status": "REJECTED",
                },
            ]
        )
        after = ner_dict.get_snapshot()
        self.assertEqual(after.metadata["alias_rows_sha256"], before.metadata["alias_rows_sha256"])
        self.assertNotIn("pending_tha", after.term_dict)
        self.assertNotIn("rejected_tha", after.term_dict)

    def test_provenance_uses_relative_paths_and_stable_hashes(self):
        metadata = ner_dict.get_dictionary_metadata()
        self.assertFalse(Path(metadata["manifest_file"]).is_absolute())
        self.assertEqual(len(metadata["manifest_sha256"]), 64)
        self.assertEqual(len(metadata["alias_rows_sha256"]), 64)
        for artifact in metadata["artifacts"].values():
            self.assertFalse(Path(artifact["file"]).is_absolute())
            self.assertEqual(len(artifact["sha256"]), 64)
        for source in metadata["source_files"].values():
            self.assertFalse(Path(source["file"]).is_absolute())
            self.assertEqual(len(source["sha256"]), 64)

        manifest_hash = metadata["manifest_sha256"]
        alias_hash = metadata["alias_rows_sha256"]
        ner_dict.reload_ner_dictionary()
        reloaded = ner_dict.get_dictionary_metadata()
        self.assertEqual(reloaded["manifest_sha256"], manifest_hash)
        self.assertEqual(reloaded["alias_rows_sha256"], alias_hash)

    def test_missing_source_metadata_is_a_validation_error(self):
        with self.assertRaises(ner_dict.DictionaryValidationError):
            ner_dict._verify_source_snapshot({"source": {"file": "source.xlsx"}}, "icd10_v1.json")

    def test_ambiguous_term_is_suggestion_only(self):
        snapshot = ner_dict.get_snapshot()
        key = next(
            key
            for key, values in snapshot.term_candidates.items()
            if key not in snapshot.term_dict and len(values) > 1
        )
        _, _, entities, preprocessing = run_ner(key)
        self.assertEqual(entities, [])
        ambiguous = [item for item in preprocessing["suggestions"] if item["matched_by"] == "ambiguous"]
        self.assertTrue(ambiguous)
        self.assertTrue(ambiguous[0]["candidate_concepts"])
        self.assertEqual(ambiguous[0]["review_state"], "UNCERTAIN")

    def test_controlled_variant_exposes_heuristic_score_only(self):
        _, _, entities, _ = run_ner("thể khác của nhiễm actinomyces")
        match = next(item for item in entities if item["icd_code"] == "A428")
        self.assertEqual(match["matched_by"], "grammar_variant")
        self.assertEqual(match["rule_score"], 0.98)
        self.assertEqual(match["score_type"], "heuristic")
        self.assertFalse(match["score_calibrated"])

    def test_icd_chapter_is_carried_to_runtime_entity(self):
        snapshot = ner_dict.get_snapshot()
        info = next(
            value
            for value in snapshot.term_dict.values()
            if value.get("icd_chapter_code") and value.get("match_term")
        )
        _, _, entities, _ = run_ner(info["match_term"])
        match = next(item for item in entities if item["text"] == info["match_term"])
        self.assertEqual(match["icd_chapter_code"], info["icd_chapter_code"])
        self.assertEqual(match["icd_chapter_name"], info["icd_chapter_name"])

    def test_concurrent_annotation_and_reload_use_complete_snapshots(self):
        text = "Bệnh lý tăng huyết áp và đái tháo đường."

        def annotate() -> tuple[list[dict], int | None]:
            _, _, entities, log = run_ner(text)
            return entities, log["dictionary"].get("snapshot_generation")

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(annotate) for _ in range(12)]
            ner_dict.reload_ner_dictionary()
            results = [future.result() for future in futures]

        self.assertTrue(all(result[0] for result in results))
        self.assertTrue(all(isinstance(result[1], int) for result in results))


if __name__ == "__main__":
    unittest.main()
