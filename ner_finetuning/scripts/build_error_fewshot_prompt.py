#!/usr/bin/env python3
"""Build a bounded next-round prompt from one or more Dev error logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from error_driven_utils import build_prompt, read_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-prompt", required=True, type=Path)
    parser.add_argument("--errors", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--selection-manifest", type=Path)
    parser.add_argument("--max-examples", type=int, default=20)
    parser.add_argument("--max-prompt-chars", type=int, default=24000)
    args = parser.parse_args()
    rows: list[dict] = []
    for path in args.errors:
        rows.extend(read_jsonl(path))
    prompt, selected = build_prompt(
        args.base_prompt.read_text(encoding="utf-8-sig"), rows,
        max_examples=args.max_examples, max_chars=args.max_prompt_chars,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(prompt, encoding="utf-8")
    manifest_path = args.selection_manifest or args.output.with_suffix(".selection.json")
    manifest = [{key: item[key] for key in ("pattern_key", "frequency", "latest_round", "error_type", "label")} for item in selected]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "examples": len(selected), "prompt_chars": len(prompt)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
