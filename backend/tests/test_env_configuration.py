"""Keep the documented environment contract synchronized with source usage."""

from __future__ import annotations

import re
import os
import unittest
from unittest.mock import patch

from config.env import BACKEND_ROOT, backend_path_from_env


ENV_CALL_RE = re.compile(
    r"(?:os\.)?getenv\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]"
    r"|_csv\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]"
    r"|os\.environ(?:\.get\(\s*['\"]|\[\s*['\"])([A-Z][A-Z0-9_]+)"
    r"|backend_path_from_env\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]"
)


def _example_keys() -> list[str]:
    keys = []
    for line in (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match:
            keys.append(match.group(1))
    return keys


def _runtime_keys() -> set[str]:
    keys: set[str] = set()
    excluded_roots = {".venv", "venv", "tests"}
    for path in BACKEND_ROOT.rglob("*.py"):
        if path.relative_to(BACKEND_ROOT).parts[0] in excluded_roots:
            continue
        for match in ENV_CALL_RE.finditer(path.read_text(encoding="utf-8")):
            keys.update(group for group in match.groups() if group)
    return keys


class EnvironmentConfigurationTests(unittest.TestCase):
    def test_env_example_documents_every_runtime_variable(self):
        example_keys = _example_keys()
        self.assertEqual(len(example_keys), len(set(example_keys)), ".env.example contains duplicate keys")
        self.assertEqual(_runtime_keys() - set(example_keys), set())

    def test_relative_storage_path_is_resolved_from_backend(self):
        with patch.dict(os.environ, {"TEST_CORPUS_PATH": "somewhere/output"}):
            self.assertEqual(
                backend_path_from_env("TEST_CORPUS_PATH", "unused"),
                (BACKEND_ROOT / "somewhere" / "output").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
