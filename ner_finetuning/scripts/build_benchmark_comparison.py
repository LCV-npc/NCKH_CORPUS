#!/usr/bin/env python3
"""Build an honest benchmark table from published references and local frozen-Test metrics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


LABELS = ["DISEASE", "SYMPTOM", "CAUSE", "DIAGNOSTIC", "TREATMENT"]
VIMEDNER = {
    "name": "ViMedNER XLM-R-large (published)",
    "precision": None,
    "recall": None,
    "micro_f1": 0.725,
    "macro_f1": 0.640,
    "per_label": {"DISEASE": 0.842, "SYMPTOM": 0.641, "CAUSE": 0.373, "DIAGNOSTIC": 0.707, "TREATMENT": 0.636},
    "scope": "Published ViMedNER test",
}
VIETBIONER = {
    "name": "VietBioNER PhoBERT (published)",
    "precision": 0.7749,
    "recall": 0.8183,
    "micro_f1": 0.7960,
    "macro_f1": None,
    "per_label": {},
    "scope": "Published VietBioNER test; different label schema",
}


def percent(value) -> str:
    return "N/A" if value is None else f"{100 * value:.2f}"


def local_row(name: str, path: Path, system: str | None) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if system:
        payload = payload[system]
    exact = payload["exact"]
    return {
        "name": name,
        "precision": exact["micro"]["precision"],
        "recall": exact["micro"]["recall"],
        "micro_f1": exact["micro"]["f1"],
        "macro_f1": exact["macro"]["f1"],
        "per_label": {label: exact["per_label"][label]["f1"] for label in LABELS},
        "scope": "Local frozen test",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supervised-metrics", type=Path, required=True)
    parser.add_argument("--supervised-name", default="Our best supervised model")
    parser.add_argument("--gemini-metrics", type=Path, required=True)
    parser.add_argument("--gemini-system", help="System key when Gemini metrics.json contains multiple systems")
    parser.add_argument("--gemini-name", default="Our Gemini prompt pipeline")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = [
        VIMEDNER,
        VIETBIONER,
        local_row(args.supervised_name, args.supervised_metrics, None),
        local_row(args.gemini_name, args.gemini_metrics, args.gemini_system),
    ]
    lines = [
        "# Benchmark comparison",
        "",
        "| System | Precision | Recall | Micro-F1 | Macro-F1 | DISEASE F1 | SYMPTOM F1 | CAUSE F1 | DIAGNOSTIC F1 | TREATMENT F1 | Evaluation scope |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        fields = [percent(row["per_label"].get(label)) for label in LABELS]
        lines.append(
            f"| {row['name']} | {percent(row['precision'])} | {percent(row['recall'])} | "
            f"{percent(row['micro_f1'])} | {percent(row['macro_f1'])} | " + " | ".join(fields) + f" | {row['scope']} |"
        )
    supervised = rows[2]
    goals = {
        "precision_ge_78": supervised["precision"] >= 0.78,
        "recall_ge_82": supervised["recall"] >= 0.82,
        "micro_f1_ge_80": supervised["micro_f1"] >= 0.80,
        "beats_vimedner_micro_f1": supervised["micro_f1"] > VIMEDNER["micro_f1"],
        "beats_vietbioner_reported_f1": supervised["micro_f1"] > VIETBIONER["micro_f1"],
    }
    lines.extend([
        "",
        "## Target audit",
        "",
        *[f"- `{name}`: {'PASS' if passed else 'NOT YET'}" for name, passed in goals.items()],
        "",
        "> Important: published rows were measured on their own original test sets. They are reference baselines, not a statistically controlled head-to-head comparison. The fair internal comparison is between local systems on the same frozen local Test set.",
        "",
    ])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    args.output.with_suffix(".json").write_text(json.dumps({"rows": rows, "goals": goals}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
