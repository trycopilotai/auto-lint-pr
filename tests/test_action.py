from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    specification = importlib.util.spec_from_file_location(
        "action_entrypoint_under_test",
        ROOT / "action_entrypoint.py",
    )
    if specification is None:
        raise RuntimeError("could not create module specification")
    if specification.loader is None:
        raise RuntimeError("module specification has no loader")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


ENTRYPOINT = load_module()


class ActionCommandTest(unittest.TestCase):
    def test_prepare_uses_all_and_typed_inputs(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_LANGUAGES": "python,markdown",
            "INPUT_LABELS": "formatting,automation",
            "INPUT_REVIEWERS": "reviewer-one",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertIn("--all", command)
        self.assertEqual(2, command.count("--language"))
        self.assertEqual(2, command.count("--label"))
        self.assertEqual(1, command.count("--reviewer"))

    def test_modified_scope_does_not_add_all(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_MODIFIED": "true",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertIn("--modified", command)
        self.assertNotIn("--all", command)

    def test_publish_does_not_include_hook_or_selection(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_HOOK": "make generate",
            "INPUT_MODIFIED": "true",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("publish")

        self.assertNotIn("--hook", command)
        self.assertNotIn("--modified", command)

    def test_selection_inputs_are_mutually_exclusive(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_MODIFIED": "true",
            "INPUT_PATHS": "src/example.py",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(ValueError):
                ENTRYPOINT.command("prepare")


if __name__ == "__main__":
    unittest.main()
