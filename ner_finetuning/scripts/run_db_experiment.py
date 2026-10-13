#!/usr/bin/env python3
"""Run or resume the versioned three-system DB experiment from the command line."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from config.env import load_backend_env  # noqa: E402

load_backend_env()

from core.ner_experiment import ner_experiment_manager  # noqa: E402


def db_config() -> dict[str, str]:
    password = os.getenv("DB_PASSWORD", "")
    if not password:
        raise RuntimeError("Thiếu DB_PASSWORD trong backend/.env")
    return {
        "user": os.getenv("DB_USER", "root"),
        "password": password,
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "database": os.getenv("DB_NAME", "yhoc_corpus"),
        "charset": "utf8mb4",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--note", default="CLI experiment")
    parser.add_argument("--resume")
    parser.add_argument("--parent")
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--poll", type=float, default=2.0)
    args = parser.parse_args()
    ner_experiment_manager.configure_db(db_config())
    if args.resume:
        state = ner_experiment_manager.resume(args.resume)
        if not state:
            raise RuntimeError(f"Không tìm thấy run {args.resume}")
    else:
        state = ner_experiment_manager.start(args.limit, args.note, args.parent)
    print(f"run_id={state.run_id}", flush=True)
    while state.status in {"queued", "running", "stopping"}:
        progress = " ".join(
            f"{name}={info.get('completed', 0)}/{info.get('total', 0)}"
            for name, info in state.systems.items()
        )
        print(f"status={state.status} {progress}", flush=True)
        time.sleep(args.poll)
    print(f"status={state.status}", flush=True)
    if args.promote and state.status in {"completed", "completed_with_errors"}:
        ner_experiment_manager.promote(state.run_id)
        print("promoted=true", flush=True)
    return 0 if state.status in {"completed", "completed_with_errors"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
