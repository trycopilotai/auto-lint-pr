from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
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
            "VERIFICATION_PATH": "/verified.json",
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
        self.assertIn("--dependency", command)
        self.assertIn("--allowed-signers", command)
        self.assertIn(
            str(ROOT / ".github" / "lint-release-allowed-signers"),
            command,
        )

    def test_modified_scope_does_not_add_all(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_MODIFIED": "true",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertIn("--modified", command)
        self.assertNotIn("--all", command)

    def test_paths_are_split_and_passed_as_positional_arguments(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_PATHS": "src/example.py 'docs/example file.md'",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertIn("src/example.py", command)
        self.assertIn("docs/example file.md", command)
        self.assertNotIn("--all", command)
        self.assertNotIn("--modified", command)

    def test_files_from0_is_passed_as_a_selection(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_FILES_FROM0": "/selection/files",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertEqual(
            "/selection/files",
            command[command.index("--files-from0") + 1],
        )
        self.assertNotIn("--all", command)
        self.assertNotIn("--modified", command)

    def test_print_width_maps_to_the_cli_option(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_PRINT_WIDTH": "120",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertEqual(
            "120",
            command[command.index("--print-width") + 1],
        )

    def test_print_width_is_stripped_before_forwarding(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_PRINT_WIDTH": " 120 ",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertEqual(
            "120",
            command[command.index("--print-width") + 1],
        )

    def test_whitespace_only_print_width_is_omitted(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_PRINT_WIDTH": "   ",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertNotIn("--print-width", command)

    def test_empty_print_width_is_omitted(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_PRINT_WIDTH": "",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertNotIn("--print-width", command)

    def test_docker_false_selects_the_local_backend(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "false",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("prepare")

        self.assertIn("--local", command)

    def test_publish_does_not_include_hook_or_selection(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_HOOK": "make generate",
            "INPUT_MODIFIED": "true",
            "INPUT_PRINT_WIDTH": "120",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("publish")

        self.assertNotIn("--hook", command)
        self.assertNotIn("--modified", command)
        self.assertNotIn("--print-width", command)
        self.assertIn("--verification", command)
        self.assertNotIn("--restore", command)

    def test_verify_restores_before_publication(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_HOOK": "make generate",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            command = ENTRYPOINT.command("verify")

        self.assertIn("--restore", command)
        self.assertIn("--verification", command)
        self.assertNotIn("--hook", command)

    def test_selection_inputs_are_mutually_exclusive(self) -> None:
        environment = {
            "GITHUB_ACTION_PATH": str(ROOT),
            "GITHUB_REPOSITORY": "owner/repository",
            "LINT_ROOT": "/lint",
            "STATE_PATH": "/state.json",
            "VERIFICATION_PATH": "/verified.json",
            "INPUT_BASE": "main",
            "INPUT_CWD": ".",
            "INPUT_DOCKER": "true",
            "INPUT_MODIFIED": "true",
            "INPUT_PATHS": "src/example.py",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(ValueError):
                ENTRYPOINT.command("prepare")

    def test_action_cwd_must_stay_inside_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            environment = {
                "GITHUB_ACTION_PATH": str(ROOT),
                "GITHUB_REPOSITORY": "owner/repository",
                "INPUT_BASE": "main",
                "INPUT_CWD": str(outside),
                "INPUT_WORKSPACE_ROOT": str(workspace),
                "LINT_ROOT": "/lint",
                "STATE_PATH": "/state.json",
            }

            with mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, "INPUT_WORKSPACE_ROOT"):
                    ENTRYPOINT.command("prepare")

    def test_action_cwd_accepts_a_nested_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            nested = workspace / "nested"
            nested.mkdir(parents=True)
            environment = {
                "GITHUB_ACTION_PATH": str(ROOT),
                "GITHUB_REPOSITORY": "owner/repository",
                "INPUT_BASE": "main",
                "INPUT_CWD": str(nested),
                "INPUT_WORKSPACE_ROOT": str(workspace),
                "LINT_ROOT": "/lint",
                "STATE_PATH": "/state.json",
            }

            with mock.patch.dict(os.environ, environment, clear=True):
                command = ENTRYPOINT.command("prepare")

            self.assertEqual(str(nested.resolve()), command[command.index("--cwd") + 1])


if __name__ == "__main__":
    unittest.main()
