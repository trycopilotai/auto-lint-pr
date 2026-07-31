from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    specification = importlib.util.spec_from_file_location(
        "auto_lint_pr_under_test",
        ROOT / "auto_lint_pr.py",
    )
    if specification is None:
        raise RuntimeError("could not create module specification")
    if specification.loader is None:
        raise RuntimeError("module specification has no loader")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


AUTO_LINT_PR = load_module()


def run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def initialize_repository(repository: Path) -> None:
    repository.mkdir()
    run_git(repository, "init", "-q", "-b", "main")
    run_git(repository, "config", "user.name", "Fixture Author")
    run_git(
        repository,
        "config",
        "user.email",
        "fixture@example.invalid",
    )


def commit_all(repository: Path, message: str = "fixture") -> str:
    run_git(repository, "add", "-A")
    run_git(repository, "commit", "-qm", message)
    return run_git(repository, "rev-parse", "HEAD")


def write_lint_fixture(root: Path) -> Path:
    initialize_repository(root)
    script = root / "lint.py"
    script.write_text(
        """\
import os
import sys
from pathlib import Path

for name in (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
):
    if os.environ.get(name):
        raise SystemExit(91)

if os.environ.get("FAKE_LINT_FAIL") == "true":
    raise SystemExit(7)

arguments = sys.argv[1:]
cwd = Path(arguments[arguments.index("--cwd") + 1])
path = cwd / "sample.txt"
if path.read_text(encoding="utf-8") == "needs-formatting\\n":
    path.write_text("formatted\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    commit = commit_all(root)
    manifest = {
        "images": {},
        "release": "fixture",
        "schema_version": 1,
        "source": {
            "archive": "lint-fixture.tar.gz",
            "commit": commit,
            "sha256": AUTO_LINT_PR.sha256(script),
        },
        "tools": {},
    }
    manifest_path = root.parent / "lint-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return manifest_path


def prepare_arguments(
    consumer: Path,
    lint_root: Path,
    manifest: Path,
    state: Path,
    extra: list[str] | None = None,
):
    values = [
        "prepare",
        "--lint-root",
        str(lint_root),
        "--manifest",
        str(manifest),
        "--cwd",
        str(consumer),
        "--state",
        str(state),
    ]
    if extra is not None:
        values.extend(extra)
    return AUTO_LINT_PR.parser().parse_args(values)


class CommandTest(unittest.TestCase):
    def test_default_lint_command_is_docker_write_all(self) -> None:
        arguments = AUTO_LINT_PR.parser().parse_args(
            [
                "prepare",
                "--lint-root",
                "/lint",
            ]
        )

        command = AUTO_LINT_PR.lint_command(arguments)

        self.assertIn("--docker", command)
        self.assertIn("--write", command)
        self.assertIn("--all", command)
        self.assertNotIn("--modified", command)

    def test_local_backend_is_explicit(self) -> None:
        arguments = AUTO_LINT_PR.parser().parse_args(
            [
                "prepare",
                "--lint-root",
                "/lint",
                "--local",
            ]
        )

        command = AUTO_LINT_PR.lint_command(arguments)

        self.assertNotIn("--docker", command)

    def test_modified_scope_does_not_add_all(self) -> None:
        arguments = AUTO_LINT_PR.parser().parse_args(
            [
                "prepare",
                "--lint-root",
                "/lint",
                "--modified",
            ]
        )

        command = AUTO_LINT_PR.lint_command(arguments)

        self.assertIn("--modified", command)
        self.assertNotIn("--all", command)

    def test_paths_and_languages_pass_through(self) -> None:
        arguments = AUTO_LINT_PR.parser().parse_args(
            [
                "prepare",
                "--lint-root",
                "/lint",
                "--language",
                "python",
                "--language",
                "markdown",
                "src/a.py",
                "docs/a.md",
            ]
        )

        command = AUTO_LINT_PR.lint_command(arguments)

        self.assertEqual(2, command.count("--language"))
        self.assertIn("src/a.py", command)
        self.assertIn("docs/a.md", command)
        self.assertNotIn("--all", command)

    def test_repository_name_is_strict(self) -> None:
        self.assertEqual(
            "owner/repository",
            AUTO_LINT_PR.normalize_repository("owner/repository"),
        )
        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.normalize_repository("../repository")


class IsolationTest(unittest.TestCase):
    def test_formatter_environment_removes_write_credentials(self) -> None:
        environment = {
            "GITHUB_TOKEN": "github-token",
            "GH_TOKEN": "gh-token",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "id-token",
            "ACTIONS_RUNTIME_TOKEN": "runtime-token",
            "PATH": "/bin",
        }

        isolated = AUTO_LINT_PR.token_free_environment(environment)

        self.assertEqual("/bin", isolated["PATH"])
        self.assertNotIn("GITHUB_TOKEN", isolated)
        self.assertNotIn("GH_TOKEN", isolated)
        self.assertNotIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", isolated)
        self.assertNotIn("ACTIONS_RUNTIME_TOKEN", isolated)


class BranchSafetyTest(unittest.TestCase):
    def test_base_branch_slug_is_stable(self) -> None:
        self.assertEqual(
            "auto-lint/feature-one",
            AUTO_LINT_PR.branch_name("Feature/One"),
        )

    def test_existing_pull_request_must_match_base_and_head(self) -> None:
        pull_requests = [
            {
                "base": {"ref": "main"},
                "head": {"ref": "auto-lint/main"},
                "number": 12,
            }
        ]

        selected = AUTO_LINT_PR.select_existing_pull_request(
            pull_requests,
            base="main",
            branch="auto-lint/main",
        )

        self.assertEqual(12, selected["number"])

    def test_mismatched_existing_pull_request_is_rejected(self) -> None:
        pull_requests = [
            {
                "base": {"ref": "release"},
                "head": {"ref": "auto-lint/main"},
                "number": 12,
            }
        ]

        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.select_existing_pull_request(
                pull_requests,
                base="main",
                branch="auto-lint/main",
            )

    def test_non_bot_tip_is_rejected(self) -> None:
        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.require_bot_tip(
                "developer@example.invalid",
            )

        AUTO_LINT_PR.require_bot_tip(AUTO_LINT_PR.BOT_EMAIL)

    def test_pull_request_tip_must_match_remote_tip(self) -> None:
        pull_request = {"head": {"sha": "expected"}}
        AUTO_LINT_PR.require_matching_pull_tip(
            pull_request,
            "expected",
        )
        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.require_matching_pull_tip(
                pull_request,
                "different",
            )


class TransactionTest(unittest.TestCase):
    def test_prepare_formats_with_credentials_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lint_root = root / "lint"
            manifest = write_lint_fixture(lint_root)
            consumer = root / "consumer"
            initialize_repository(consumer)
            sample = consumer / "sample.txt"
            sample.write_text("needs-formatting\n", encoding="utf-8")
            commit_all(consumer)
            state = root / "state.json"
            arguments = prepare_arguments(
                consumer,
                lint_root,
                manifest,
                state,
            )
            environment = {
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "id-token",
                "ACTIONS_RUNTIME_TOKEN": "runtime-token",
                "GH_TOKEN": "gh-token",
                "GITHUB_TOKEN": "github-token",
            }

            with mock.patch.dict(os.environ, environment, clear=False):
                result = AUTO_LINT_PR.run_prepare(arguments)

            self.assertTrue(result["changed"])
            self.assertEqual(
                "formatted\n",
                sample.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                ["sample.txt"],
                [record["path"] for record in result["delta"]],
            )

    def test_no_change_prepare_records_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lint_root = root / "lint"
            manifest = write_lint_fixture(lint_root)
            consumer = root / "consumer"
            initialize_repository(consumer)
            (consumer / "sample.txt").write_text(
                "formatted\n",
                encoding="utf-8",
            )
            commit_all(consumer)
            state = root / "state.json"
            arguments = prepare_arguments(
                consumer,
                lint_root,
                manifest,
                state,
            )

            result = AUTO_LINT_PR.run_prepare(arguments)

            self.assertFalse(result["changed"])
            self.assertEqual([], result["delta"])

    def test_modified_lint_checkout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lint_root = root / "lint"
            manifest = write_lint_fixture(lint_root)
            (lint_root / "lint.py").write_text(
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            consumer = root / "consumer"
            initialize_repository(consumer)
            (consumer / "sample.txt").write_text(
                "formatted\n",
                encoding="utf-8",
            )
            commit_all(consumer)
            arguments = prepare_arguments(
                consumer,
                lint_root,
                manifest,
                root / "state.json",
            )

            with self.assertRaises(AUTO_LINT_PR.DependencyError):
                AUTO_LINT_PR.run_prepare(arguments)

    def test_consumer_hook_runs_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lint_root = root / "lint"
            manifest = write_lint_fixture(lint_root)
            consumer = root / "consumer"
            initialize_repository(consumer)
            (consumer / "sample.txt").write_text(
                "formatted\n",
                encoding="utf-8",
            )
            commit_all(consumer)
            hook = root / "hook.py"
            hook.write_text(
                """\
import os
from pathlib import Path

for name in (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
):
    if os.environ.get(name):
        raise SystemExit(92)

Path("generated.txt").write_text("generated\\n", encoding="utf-8")
""",
                encoding="utf-8",
            )
            hook_command = " ".join(
                [
                    shlex.quote(sys.executable),
                    shlex.quote(str(hook)),
                ]
            )
            state = root / "state.json"
            arguments = prepare_arguments(
                consumer,
                lint_root,
                manifest,
                state,
                ["--hook", hook_command],
            )
            environment = {
                "GH_TOKEN": "gh-token",
                "GITHUB_TOKEN": "github-token",
            }

            with mock.patch.dict(os.environ, environment, clear=False):
                result = AUTO_LINT_PR.run_prepare(arguments)

            self.assertTrue(result["changed"])
            self.assertEqual(
                ["generated.txt"],
                [record["path"] for record in result["delta"]],
            )

    def test_formatter_failure_stops_before_publish(self) -> None:
        with mock.patch.object(
            AUTO_LINT_PR,
            "run_prepare",
            side_effect=AUTO_LINT_PR.CommandError("formatter failed"),
        ):
            with mock.patch.object(
                AUTO_LINT_PR,
                "run_publish",
            ) as publish:
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    status = AUTO_LINT_PR.main(["run", "--lint-root", "/unused"])

        self.assertEqual(1, status)
        self.assertEqual("formatter failed\n", error.getvalue())
        publish.assert_not_called()

    def test_commit_contains_exact_working_tree_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "consumer"
            initialize_repository(repository)
            first = repository / "first.txt"
            second = repository / "second.txt"
            first.write_text("before\n", encoding="utf-8")
            second.write_text("unchanged\n", encoding="utf-8")
            parent = commit_all(repository)
            first.write_text("after\n", encoding="utf-8")

            commit = AUTO_LINT_PR.commit_delta(
                repository,
                parent,
                "Apply automated formatting",
            )

            changed = run_git(
                repository,
                "diff",
                "--name-only",
                parent,
                commit,
            )
            author = run_git(
                repository,
                "show",
                "-s",
                "--format=%ae",
                commit,
            )
            self.assertEqual("first.txt", changed)
            self.assertEqual(AUTO_LINT_PR.BOT_EMAIL, author)

    def test_publish_no_change_does_not_require_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "consumer"
            initialize_repository(repository)
            (repository / "sample.txt").write_text(
                "formatted\n",
                encoding="utf-8",
            )
            head = commit_all(repository)
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "base": "main",
                        "base_head": head,
                        "branch": "auto-lint/main",
                        "changed": False,
                        "cwd": str(repository),
                        "delta": [],
                        "lint_commit": "pinned",
                        "repository": "owner/repository",
                        "schema": 1,
                    }
                ),
                encoding="utf-8",
            )
            arguments = AUTO_LINT_PR.parser().parse_args(
                [
                    "publish",
                    "--lint-root",
                    "/unused",
                    "--state",
                    str(state_path),
                ]
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                result = AUTO_LINT_PR.run_publish(arguments)

            self.assertFalse(result["changed"])
            self.assertIsNone(result["pull_request"])

    def test_publish_creates_pull_request_for_exact_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "consumer"
            initialize_repository(repository)
            sample = repository / "sample.txt"
            sample.write_text("before\n", encoding="utf-8")
            head = commit_all(repository)
            sample.write_text("after\n", encoding="utf-8")
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "base": "main",
                        "base_head": head,
                        "branch": "auto-lint/main",
                        "changed": True,
                        "cwd": str(repository),
                        "delta": AUTO_LINT_PR.delta_records(repository),
                        "lint_commit": "pinned",
                        "repository": "owner/repository",
                        "schema": 1,
                    }
                ),
                encoding="utf-8",
            )
            arguments = AUTO_LINT_PR.parser().parse_args(
                [
                    "publish",
                    "--lint-root",
                    "/unused",
                    "--state",
                    str(state_path),
                ]
            )
            environment = {"GH_TOKEN": "token"}
            push_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="",
                stderr="",
            )
            with mock.patch.object(
                AUTO_LINT_PR,
                "require_token",
                return_value=environment,
            ):
                with mock.patch.object(
                    AUTO_LINT_PR,
                    "open_pull_requests",
                    return_value=[],
                ):
                    with mock.patch.object(
                        AUTO_LINT_PR,
                        "remote_tip",
                        return_value=None,
                    ):
                        with mock.patch.object(
                            AUTO_LINT_PR,
                            "authenticated_git",
                            return_value=push_result,
                        ) as authenticated:
                            with mock.patch.object(
                                AUTO_LINT_PR,
                                "gh_api",
                                return_value={"number": 17},
                            ) as api:
                                result = AUTO_LINT_PR.run_publish(arguments)

            self.assertTrue(result["changed"])
            self.assertEqual(17, result["pull_request"])
            authenticated.assert_called_once()
            request = api.call_args.args[0]
            self.assertIn("repos/owner/repository/pulls", request)

    def test_publish_rejects_pull_request_tip_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "consumer"
            initialize_repository(repository)
            sample = repository / "sample.txt"
            sample.write_text("before\n", encoding="utf-8")
            head = commit_all(repository)
            sample.write_text("after\n", encoding="utf-8")
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "base": "main",
                        "base_head": head,
                        "branch": "auto-lint/main",
                        "changed": True,
                        "cwd": str(repository),
                        "delta": AUTO_LINT_PR.delta_records(repository),
                        "lint_commit": "pinned",
                        "repository": "owner/repository",
                        "schema": 1,
                    }
                ),
                encoding="utf-8",
            )
            arguments = AUTO_LINT_PR.parser().parse_args(
                [
                    "publish",
                    "--lint-root",
                    "/unused",
                    "--state",
                    str(state_path),
                ]
            )
            pull = {
                "base": {"ref": "main"},
                "head": {
                    "ref": "auto-lint/main",
                    "sha": "pull-tip",
                },
                "number": 4,
            }
            with mock.patch.object(
                AUTO_LINT_PR,
                "require_token",
                return_value={"GH_TOKEN": "token"},
            ):
                with mock.patch.object(
                    AUTO_LINT_PR,
                    "open_pull_requests",
                    return_value=[pull],
                ):
                    with mock.patch.object(
                        AUTO_LINT_PR,
                        "remote_tip",
                        return_value="remote-tip",
                    ):
                        with mock.patch.object(
                            AUTO_LINT_PR,
                            "authenticated_git",
                        ) as authenticated:
                            with self.assertRaises(AUTO_LINT_PR.SafetyError):
                                AUTO_LINT_PR.run_publish(arguments)

            authenticated.assert_not_called()


class OutputTest(unittest.TestCase):
    def test_action_output_is_appended(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            AUTO_LINT_PR.write_action_output("changed", "false")


if __name__ == "__main__":
    unittest.main()
