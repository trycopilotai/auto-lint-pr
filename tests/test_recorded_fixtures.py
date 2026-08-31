"""Bind the publish validators to recorded GitHub responses.

The committer refusal fixed by commit c9ae717 survived the unit
suite because a hand-written stub asserted a committer GitHub
does not actually write: the stub's shape was right and its
values were wrong. These tests compare the validators and the
identity constants against a response recorded from a real
createCommitOnBranch publication, so a stub or a rule can no
longer drift from what GitHub actually says without a test
noticing.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPECIFICATION = importlib.util.spec_from_file_location(
    "auto_lint_pr_recorded",
    ROOT / "auto_lint_pr.py",
)
AUTO_LINT_PR = importlib.util.module_from_spec(SPECIFICATION)
SPECIFICATION.loader.exec_module(AUTO_LINT_PR)

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "github" / "signed-commit.json"


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class RecordedIdentityTest(unittest.TestCase):
    def test_recorded_commit_passes_branch_validation(self) -> None:
        record = load_fixture()
        AUTO_LINT_PR.validate_branch_commits([record])
        AUTO_LINT_PR.validate_branch_commits(
            [record],
            remedy=AUTO_LINT_PR.CREATED_COMMIT_REMEDY,
        )

    def test_recorded_identities_match_the_module_constants(self) -> None:
        record = load_fixture()
        self.assertEqual(
            AUTO_LINT_PR.BOT_LOGIN,
            record["author"]["login"],
        )
        self.assertEqual(
            AUTO_LINT_PR.BOT_EMAIL,
            record["commit"]["author"]["email"],
        )
        self.assertEqual(
            AUTO_LINT_PR.GITHUB_SIGNER_LOGIN,
            record["committer"]["login"],
        )
        self.assertEqual(
            AUTO_LINT_PR.GITHUB_SIGNER_EMAIL,
            record["commit"]["committer"]["email"],
        )
        self.assertIs(True, record["commit"]["verification"]["verified"])
        self.assertIs(True, record["github_signature"]["isValid"])
        self.assertIs(True, record["github_signature"]["wasSignedByGitHub"])

    def test_bot_committer_variant_is_still_accepted(self) -> None:
        record = copy.deepcopy(load_fixture())
        record["committer"]["login"] = AUTO_LINT_PR.BOT_LOGIN
        record["commit"]["committer"]["email"] = AUTO_LINT_PR.BOT_EMAIL
        AUTO_LINT_PR.validate_branch_commits([record])

    def test_human_committer_variant_is_refused(self) -> None:
        record = copy.deepcopy(load_fixture())
        record["committer"]["login"] = "person"
        record["commit"]["committer"]["email"] = "person@example.com"
        with self.assertRaisesRegex(
            AUTO_LINT_PR.SafetyError,
            "non-bot committer",
        ):
            AUTO_LINT_PR.validate_branch_commits([record])


class RecordingTest(unittest.TestCase):
    def test_recording_excludes_environment_and_file_contents(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh"],
            returncode=0,
            stdout='{"ok": true}',
            stderr="",
        )
        payload = {
            "input": {
                "fileChanges": {
                    "additions": [{"path": "a.txt", "contents": "c2VjcmV0"}]
                }
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    AUTO_LINT_PR,
                    "RECORD_DIRECTORY",
                    Path(directory),
                ),
                mock.patch.object(AUTO_LINT_PR, "RECORD_SEQUENCE", 0),
                mock.patch.object(
                    AUTO_LINT_PR.subprocess,
                    "run",
                    return_value=completed,
                ),
                mock.patch.object(
                    AUTO_LINT_PR,
                    "trusted_gh",
                    return_value=Path("/usr/bin/true"),
                ),
            ):
                result = AUTO_LINT_PR.gh_api(
                    ["graphql"],
                    {"GH_TOKEN": "token-value"},
                    input_value=payload,
                )
                records = sorted(Path(directory).glob("*.json"))
            self.assertEqual({"ok": True}, result)
            self.assertEqual(1, len(records))
            text = records[0].read_text(encoding="utf-8")
            record = json.loads(text)
            self.assertEqual(["graphql"], record["arguments"])
            self.assertEqual(
                "<omitted>",
                record["input"]["input"]["fileChanges"]["additions"][0]["contents"],
            )
            self.assertNotIn("token-value", text)
            self.assertNotIn("environment", record)
            self.assertNotIn("c2VjcmV0", text)

    def test_recording_is_off_by_default(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh"],
            returncode=0,
            stdout="null",
            stderr="",
        )
        with (
            mock.patch.object(
                AUTO_LINT_PR.subprocess,
                "run",
                return_value=completed,
            ),
            mock.patch.object(
                AUTO_LINT_PR,
                "trusted_gh",
                return_value=Path("/usr/bin/true"),
            ),
            mock.patch.object(
                AUTO_LINT_PR,
                "record_api_exchange",
                wraps=AUTO_LINT_PR.record_api_exchange,
            ) as recorder,
        ):
            AUTO_LINT_PR.gh_api(["graphql"], {"GH_TOKEN": "token"})
        recorder.assert_called_once()
        self.assertIsNone(AUTO_LINT_PR.RECORD_DIRECTORY)


if __name__ == "__main__":
    sys.exit(unittest.main())
