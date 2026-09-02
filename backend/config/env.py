"""Shared environment loading and backend-relative path resolution."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND_ROOT / ".env"


def load_backend_env() -> bool:
    """Load the one runtime .env file without overriding process variables."""
    return load_dotenv(dotenv_path=ENV_FILE, override=False)


def backend_path_from_env(name: str, default_relative: str) -> Path:
    """Resolve an optional path variable relative to ``backend/``."""
    raw_value = os.getenv(name, "").strip()
    path = Path(raw_value).expanduser() if raw_value else Path(default_relative)
    if not path.is_absolute():
        path = BACKEND_ROOT / path
    return path.resolve()
