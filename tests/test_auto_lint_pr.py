from __future__ import annotations

import base64
import contextlib
import hashlib
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


def blob_record(payload: bytes, mode: str = "100644") -> dict[str, str]:
    return {
        "mode": mode,
        "sha": AUTO_LINT_PR.git_blob_object_id(payload),
        "type": "blob",
    }


def write_lint_fixture(root: Path) -> Path:
    initialize_repository(root)
    (root / ".gitignore").write_text(
        "sitecustomize.py\n",
        encoding="utf-8",
    )
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
                "head": {
                    "ref": "auto-lint/main",
                    "repo": {"full_name": "owner/repository"},
                },
                "number": 12,
            }
        ]

        selected = AUTO_LINT_PR.select_existing_pull_request(
            pull_requests,
            base="main",
            branch="auto-lint/main",
            repository_name="owner/repository",
        )

        self.assertEqual(12, selected["number"])

    def test_mismatched_existing_pull_request_is_rejected(self) -> None:
        pull_requests = [
            {
                "base": {"ref": "release"},
                "head": {
                    "ref": "auto-lint/main",
                    "repo": {"full_name": "owner/repository"},
                },
                "number": 12,
            }
        ]

        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.select_existing_pull_request(
                pull_requests,
                base="main",
                branch="auto-lint/main",
                repository_name="owner/repository",
            )

    def test_existing_pull_request_head_repository_must_match(self) -> None:
        pull_requests = [
            {
                "base": {"ref": "main"},
                "head": {
                    "ref": "auto-lint/main",
                    "repo": {"full_name": "other/repository"},
                },
                "number": 12,
            }
        ]

        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.select_existing_pull_request(
                pull_requests,
                base="main",
                branch="auto-lint/main",
                repository_name="owner/repository",
            )

    def test_bot_email_without_a_valid_signature_is_rejected(self) -> None:
        unsigned = {
            "author": {"login": AUTO_LINT_PR.BOT_LOGIN},
            "commit": {
                "author": {"email": AUTO_LINT_PR.BOT_EMAIL},
                "verification": {"verified": False},
            },
            "github_signature": {
                "isValid": False,
                "wasSignedByGitHub": True,
            },
            "sha": "unsigned",
        }

        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.validate_branch_commits([unsigned])

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

    def test_reused_branch_commits_must_be_signed_and_bot_owned(
        self,
    ) -> None:
        bot = {
            "author": {"login": AUTO_LINT_PR.BOT_LOGIN},
            "commit": {
                "author": {"email": AUTO_LINT_PR.BOT_EMAIL},
                "verification": {"verified": True},
            },
            "github_signature": {
                "isValid": True,
                "wasSignedByGitHub": True,
            },
            "sha": "bot",
        }
        unsigned = json.loads(json.dumps(bot))
        unsigned["commit"]["verification"]["verified"] = False
        signed_by_human = json.loads(json.dumps(bot))
        signed_by_human["github_signature"]["wasSignedByGitHub"] = False
        human = json.loads(json.dumps(bot))
        human["author"]["login"] = "human"

        AUTO_LINT_PR.validate_branch_commits([bot])
        for commits in ([unsigned], [signed_by_human], [bot, human]):
            with self.subTest(commits=commits):
                with self.assertRaises(AUTO_LINT_PR.SafetyError):
                    AUTO_LINT_PR.validate_branch_commits(commits)

    def test_signature_query_is_bound_to_repository_and_commit(
        self,
    ) -> None:
        oid = "a" * 40
        response = {
            "data": {
                "repository": {
                    "object": {
                        "oid": oid,
                        "signature": {
                            "isValid": True,
                            "wasSignedByGitHub": True,
                        },
                    }
                }
            }
        }
        with mock.patch.object(
            AUTO_LINT_PR,
            "gh_api",
            return_value=response,
        ) as api:
            signature = AUTO_LINT_PR.github_commit_signature(
                "owner/repository",
                oid,
                {"GH_TOKEN": "token"},
            )

        self.assertTrue(signature["wasSignedByGitHub"])
        request = api.call_args.kwargs["input_value"]
        self.assertEqual(
            {
                "name": "repository",
                "oid": oid,
                "owner": "owner",
            },
            request["variables"],
        )

    def test_existing_branch_tree_must_only_change_prepared_paths(
        self,
    ) -> None:
        base_tree = {
            "one.txt": {"mode": "100644", "sha": "base-one"},
            "two.txt": {"mode": "100644", "sha": "base-two"},
        }
        branch_tree = {
            "one.txt": {"mode": "100644", "sha": "branch-one"},
            "two.txt": {"mode": "100644", "sha": "branch-two"},
        }
        prepared_bytes = b"prepared\n"
        prepared = [
            {
                "content": base64.b64encode(prepared_bytes).decode("ascii"),
                "kind": "file",
                "mode": "100644",
                "path": "one.txt",
                "sha256": hashlib.sha256(prepared_bytes).hexdigest(),
            }
        ]

        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.validate_existing_branch_tree(
                base_tree,
                branch_tree,
                prepared,
            )

    def test_existing_branch_promotion_is_fast_forward_and_verified(self) -> None:
        with (
            mock.patch.object(
                AUTO_LINT_PR,
                "remote_tip",
                side_effect=["expected", "published"],
            ),
            mock.patch.object(AUTO_LINT_PR, "gh_api") as api,
        ):
            AUTO_LINT_PR.advance_remote_branch(
                "owner/repository",
                "auto-lint/main",
                "expected",
                "published",
                {"GH_TOKEN": "token"},
            )

        arguments = api.call_args.args[0]
        self.assertIn("repos/owner/repository/git/refs/heads/auto-lint/main", arguments)
        self.assertIn("sha=published", arguments)
        self.assertIn("force=false", arguments)


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

    def test_ignored_lint_checkout_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lint_root = root / "lint"
            manifest = write_lint_fixture(lint_root)
            (lint_root / "sitecustomize.py").write_text(
                "raise SystemExit(93)\n",
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

    def test_prepare_rejects_a_hook_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lint_root = root / "lint"
            manifest = write_lint_fixture(lint_root)
            consumer = root / "consumer"
            initialize_repository(consumer)
            (consumer / "sample.txt").write_text(
                "needs-formatting\n",
                encoding="utf-8",
            )
            commit_all(consumer)
            hook = root / "hook.py"
            hook.write_text(
                """\
import subprocess
from pathlib import Path

Path("hidden.txt").write_text("hidden\\n", encoding="utf-8")
subprocess.run(["git", "add", "hidden.txt"], check=True)
subprocess.run(["git", "commit", "-m", "hidden"], check=True)
""",
                encoding="utf-8",
            )
            hook_command = " ".join(
                [
                    shlex.quote(sys.executable),
                    shlex.quote(str(hook)),
                ]
            )
            arguments = prepare_arguments(
                consumer,
                lint_root,
                manifest,
                root / "state.json",
                ["--hook", hook_command],
            )

            with self.assertRaises(AUTO_LINT_PR.SafetyError):
                AUTO_LINT_PR.run_prepare(arguments)

    def test_mode_mutation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "consumer"
            initialize_repository(repository)
            sample = repository / "sample.txt"
            sample.write_text("before\n", encoding="utf-8")
            commit_all(repository)
            sample.write_text("prepared\n", encoding="utf-8")
            expected = AUTO_LINT_PR.delta_records(repository)

            sample.chmod(0o755)

            with self.assertRaises(AUTO_LINT_PR.SafetyError):
                AUTO_LINT_PR.assert_delta(repository, expected)

    def test_prepare_rejects_unrepresentable_file_modes(self) -> None:
        payload = b"prepared\n"
        for kind, mode in (("file", "100755"), ("symlink", "120000")):
            record = {
                "content": base64.b64encode(payload).decode("ascii"),
                "kind": kind,
                "mode": mode,
                "path": "sample",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            with self.subTest(kind=kind, mode=mode):
                with self.assertRaises(AUTO_LINT_PR.SafetyError):
                    AUTO_LINT_PR.validate_prepared_modes([record])

    def test_base_tree_rejects_unrepresentable_existing_mode(self) -> None:
        payload = b"prepared\n"
        record = {
            "content": base64.b64encode(payload).decode("ascii"),
            "kind": "file",
            "mode": "100644",
            "path": "sample",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        base_tree = {"sample": blob_record(b"before\n", mode="100755")}

        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.validate_base_tree(base_tree, [record])

    def test_prepare_rejects_executable_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "consumer"
            initialize_repository(repository)
            sample = repository / "sample"
            sample.write_text("before\n", encoding="utf-8")
            sample.chmod(0o755)
            commit_all(repository)
            sample.unlink()

            records = AUTO_LINT_PR.delta_records(repository)

            with self.assertRaises(AUTO_LINT_PR.SafetyError):
                AUTO_LINT_PR.validate_prepared_modes(records)

    def test_publication_uses_recorded_bytes_after_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "consumer"
            initialize_repository(repository)
            sample = repository / "sample.txt"
            sample.write_text("before\n", encoding="utf-8")
            commit_all(repository)
            sample.write_text("prepared\n", encoding="utf-8")
            expected = AUTO_LINT_PR.delta_records(repository)

            AUTO_LINT_PR.assert_delta(repository, expected)
            sample.write_text("changed-after-check\n", encoding="utf-8")
            changes = AUTO_LINT_PR.publication_file_changes(expected)

            encoded = changes["additions"][0]["contents"]
            self.assertEqual(b"prepared\n", base64.b64decode(encoded))

    def test_publication_does_not_run_git_filters_with_token(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "consumer"
            initialize_repository(repository)
            capture = root / "captured.txt"
            filter_script = root / "filter.py"
            filter_script.write_text(
                """\
import os
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    os.environ.get("GH_TOKEN", "ABSENT"),
    encoding="utf-8",
)
sys.stdout.buffer.write(sys.stdin.buffer.read())
""",
                encoding="utf-8",
            )
            (repository / ".gitattributes").write_text(
                "sample.txt filter=probe\n",
                encoding="utf-8",
            )
            sample = repository / "sample.txt"
            sample.write_text("before\n", encoding="utf-8")
            commit_all(repository)
            filter_command = " ".join(
                [
                    shlex.quote(sys.executable),
                    shlex.quote(str(filter_script)),
                    shlex.quote(str(capture)),
                ]
            )
            run_git(
                repository,
                "config",
                "filter.probe.clean",
                filter_command,
            )
            run_git(
                repository,
                "config",
                "filter.probe.required",
                "true",
            )
            sample.write_text("prepared\n", encoding="utf-8")
            expected = AUTO_LINT_PR.delta_records(repository)
            self.assertEqual("ABSENT", capture.read_text(encoding="utf-8"))
            capture.unlink()

            with mock.patch.dict(
                os.environ,
                {"GH_TOKEN": "publish-secret"},
                clear=False,
            ):
                AUTO_LINT_PR.publication_file_changes(expected)

            self.assertFalse(capture.exists())

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

    def test_publication_changes_contain_exact_prepared_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "consumer"
            initialize_repository(repository)
            first = repository / "first.txt"
            second = repository / "second.txt"
            first.write_text("before\n", encoding="utf-8")
            second.write_text("unchanged\n", encoding="utf-8")
            commit_all(repository)
            first.write_text("after\n", encoding="utf-8")

            records = AUTO_LINT_PR.delta_records(repository)
            changes = AUTO_LINT_PR.publication_file_changes(records)

            self.assertEqual(
                ["first.txt"],
                [record["path"] for record in changes["additions"]],
            )
            encoded = changes["additions"][0]["contents"]
            self.assertEqual(b"after\n", base64.b64decode(encoded))

    def test_invalid_prepared_payload_is_a_safety_error(self) -> None:
        record = {
            "content": "not-base64!",
            "kind": "file",
            "mode": "100644",
            "path": "sample.txt",
            "sha256": "0" * 64,
        }

        with self.assertRaises(AUTO_LINT_PR.SafetyError):
            AUTO_LINT_PR.publication_file_changes([record])

    def test_signed_commit_uses_exact_expected_head_and_bytes(self) -> None:
        payload = b"prepared\n"
        records = [
            {
                "content": base64.b64encode(payload).decode("ascii"),
                "kind": "file",
                "mode": "100644",
                "path": "sample.txt",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ]
        oid = "b" * 40
        signature = {
            "isValid": True,
            "wasSignedByGitHub": True,
        }
        mutation = {
            "data": {
                "createCommitOnBranch": {
                    "commit": {
                        "oid": oid,
                        "signature": signature,
                    },
                    "ref": {"target": {"oid": oid}},
                }
            }
        }
        metadata = {
            "author": {"login": AUTO_LINT_PR.BOT_LOGIN},
            "commit": {
                "author": {"email": AUTO_LINT_PR.BOT_EMAIL},
                "verification": {"verified": True},
            },
            "sha": oid,
        }
        with mock.patch.object(
            AUTO_LINT_PR,
            "gh_api",
            side_effect=[mutation, metadata],
        ) as api:
            actual, actual_signature = AUTO_LINT_PR.create_signed_commit(
                "owner/repository",
                "auto-lint/main",
                "a" * 40,
                records,
                "Apply automated formatting",
                {"GH_TOKEN": "token"},
            )
            AUTO_LINT_PR.verify_created_commit(
                "owner/repository",
                actual,
                actual_signature,
                {"GH_TOKEN": "token"},
            )

        self.assertEqual(oid, actual)
        mutation_input = api.call_args_list[0].kwargs["input_value"]
        commit_input = mutation_input["variables"]["input"]
        self.assertEqual("a" * 40, commit_input["expectedHeadOid"])
        self.assertEqual(
            {
                "branchName": "auto-lint/main",
                "repositoryNameWithOwner": "owner/repository",
            },
            commit_input["branch"],
        )
        encoded = commit_input["fileChanges"]["additions"][0]["contents"]
        self.assertEqual(payload, base64.b64decode(encoded))

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

    def test_publish_refuses_pull_request_target(self) -> None:
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

            with mock.patch.dict(
                os.environ,
                {"GITHUB_EVENT_NAME": "pull_request_target"},
                clear=True,
            ):
                with self.assertRaises(AUTO_LINT_PR.SafetyError):
                    AUTO_LINT_PR.run_publish(arguments)

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
            base_tree = {"sample.txt": blob_record(b"before\n")}
            published_tree = {"sample.txt": blob_record(b"after\n")}
            pull_request = {
                "base": {"ref": "main"},
                "head": {
                    "ref": "auto-lint/main",
                    "repo": {"full_name": "owner/repository"},
                    "sha": "signed-commit",
                },
                "number": 17,
            }
            with (
                mock.patch.object(
                    AUTO_LINT_PR,
                    "require_token",
                    return_value=environment,
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "open_pull_requests",
                    return_value=[],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tip",
                    side_effect=[head, None, "signed-commit"],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tree",
                    side_effect=[base_tree, published_tree],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "create_remote_branch",
                ) as create_branch,
                mock.patch.object(
                    AUTO_LINT_PR,
                    "create_signed_commit",
                    return_value=("signed-commit", {"isValid": True}),
                ) as create_commit,
                mock.patch.object(AUTO_LINT_PR, "verify_created_commit"),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "gh_api",
                    return_value=pull_request,
                ) as api,
            ):
                result = AUTO_LINT_PR.run_publish(arguments)

            self.assertTrue(result["changed"])
            self.assertEqual(17, result["pull_request"])
            create_branch.assert_called_once()
            create_commit.assert_called_once()
            request = api.call_args.args[0]
            self.assertIn("repos/owner/repository/pulls", request)

    def test_publish_cleans_new_branch_when_remote_tree_differs(
        self,
    ) -> None:
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
            base_tree = {"sample.txt": blob_record(b"before\n")}
            wrong_tree = {"sample.txt": blob_record(b"wrong\n")}
            with (
                mock.patch.object(
                    AUTO_LINT_PR,
                    "require_token",
                    return_value={"GH_TOKEN": "token"},
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "open_pull_requests",
                    return_value=[],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tip",
                    side_effect=[head, None],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tree",
                    side_effect=[base_tree, wrong_tree],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "create_remote_branch",
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "create_signed_commit",
                    return_value=("signed-commit", {"isValid": True}),
                ),
                mock.patch.object(AUTO_LINT_PR, "verify_created_commit"),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "delete_remote_branch",
                ) as delete_branch,
            ):
                with self.assertRaises(AUTO_LINT_PR.SafetyError):
                    AUTO_LINT_PR.run_publish(arguments)

            delete_branch.assert_called_once_with(
                "owner/repository",
                "auto-lint/main",
                {"GH_TOKEN": "token"},
            )

    def test_publish_cleans_staging_without_moving_existing_branch_on_failure(
        self,
    ) -> None:
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
            branch_tip = "existing-tip"
            pull = {
                "base": {"ref": "main"},
                "head": {
                    "ref": "auto-lint/main",
                    "repo": {"full_name": "owner/repository"},
                    "sha": branch_tip,
                },
                "number": 4,
            }
            base_tree = {"sample.txt": blob_record(b"before\n")}
            branch_tree = {"sample.txt": blob_record(b"stale\n")}
            wrong_tree = {"sample.txt": blob_record(b"wrong\n")}
            staging_branch = AUTO_LINT_PR.staging_branch_name(
                "auto-lint/main",
                branch_tip,
                AUTO_LINT_PR.delta_records(repository),
            )
            with (
                mock.patch.object(
                    AUTO_LINT_PR,
                    "require_token",
                    return_value={"GH_TOKEN": "token"},
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "open_pull_requests",
                    return_value=[pull],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tip",
                    side_effect=[head, branch_tip, None, "signed-commit"],
                ),
                mock.patch.object(AUTO_LINT_PR, "branch_commits"),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tree",
                    side_effect=[base_tree, branch_tree, wrong_tree],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "create_signed_commit",
                    return_value=("signed-commit", {"isValid": True}),
                ),
                mock.patch.object(AUTO_LINT_PR, "verify_created_commit"),
                mock.patch.object(AUTO_LINT_PR, "create_remote_branch"),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "advance_remote_branch",
                ) as advance_branch,
                mock.patch.object(
                    AUTO_LINT_PR,
                    "delete_remote_branch",
                ) as delete_branch,
            ):
                with self.assertRaises(AUTO_LINT_PR.SafetyError):
                    AUTO_LINT_PR.run_publish(arguments)

            advance_branch.assert_not_called()
            delete_branch.assert_called_once_with(
                "owner/repository",
                staging_branch,
                {"GH_TOKEN": "token"},
            )

    def test_publish_promotes_verified_staging_commit_to_existing_branch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "consumer"
            initialize_repository(repository)
            sample = repository / "sample.txt"
            sample.write_text("before\n", encoding="utf-8")
            head = commit_all(repository)
            sample.write_text("after\n", encoding="utf-8")
            delta = AUTO_LINT_PR.delta_records(repository)
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "base": "main",
                        "base_head": head,
                        "branch": "auto-lint/main",
                        "changed": True,
                        "cwd": str(repository),
                        "delta": delta,
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
            branch_tip = "existing-tip"
            pull_before = {
                "base": {"ref": "main"},
                "head": {
                    "ref": "auto-lint/main",
                    "repo": {"full_name": "owner/repository"},
                    "sha": branch_tip,
                },
                "number": 4,
            }
            pull_after = json.loads(json.dumps(pull_before))
            pull_after["head"]["sha"] = "signed-commit"
            base_tree = {"sample.txt": blob_record(b"before\n")}
            branch_tree = {"sample.txt": blob_record(b"stale\n")}
            published_tree = {"sample.txt": blob_record(b"after\n")}
            staging_branch = AUTO_LINT_PR.staging_branch_name(
                "auto-lint/main",
                branch_tip,
                delta,
            )
            with (
                mock.patch.object(
                    AUTO_LINT_PR,
                    "require_token",
                    return_value={"GH_TOKEN": "token"},
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "open_pull_requests",
                    side_effect=[[pull_before], [pull_after]],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tip",
                    side_effect=[head, branch_tip, None, "signed-commit"],
                ),
                mock.patch.object(AUTO_LINT_PR, "branch_commits"),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tree",
                    side_effect=[base_tree, branch_tree, published_tree],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "create_signed_commit",
                    return_value=("signed-commit", {"isValid": True}),
                ),
                mock.patch.object(AUTO_LINT_PR, "verify_created_commit"),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "create_remote_branch",
                ) as create_branch,
                mock.patch.object(
                    AUTO_LINT_PR,
                    "advance_remote_branch",
                ) as advance_branch,
                mock.patch.object(
                    AUTO_LINT_PR,
                    "delete_remote_branch",
                ) as delete_branch,
            ):
                result = AUTO_LINT_PR.run_publish(arguments)

            self.assertTrue(result["changed"])
            self.assertEqual(4, result["pull_request"])
            create_branch.assert_called_once_with(
                "owner/repository",
                staging_branch,
                branch_tip,
                {"GH_TOKEN": "token"},
            )
            advance_branch.assert_called_once_with(
                "owner/repository",
                "auto-lint/main",
                branch_tip,
                "signed-commit",
                {"GH_TOKEN": "token"},
            )
            delete_branch.assert_called_once_with(
                "owner/repository",
                staging_branch,
                {"GH_TOKEN": "token"},
            )

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
                    "repo": {"full_name": "owner/repository"},
                    "sha": "pull-tip",
                },
                "number": 4,
            }
            base_tree = {"sample.txt": blob_record(b"before\n")}
            with (
                mock.patch.object(
                    AUTO_LINT_PR,
                    "require_token",
                    return_value={"GH_TOKEN": "token"},
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "open_pull_requests",
                    return_value=[pull],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tip",
                    side_effect=[head, "remote-tip"],
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "remote_tree",
                    return_value=base_tree,
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "create_signed_commit",
                ) as create_commit,
            ):
                with self.assertRaises(AUTO_LINT_PR.SafetyError):
                    AUTO_LINT_PR.run_publish(arguments)

            create_commit.assert_not_called()


class OutputTest(unittest.TestCase):
    def test_action_output_is_appended(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            AUTO_LINT_PR.write_action_output("changed", "false")


if __name__ == "__main__":
    unittest.main()
