from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import verify_repo  # noqa: E402


class ActionMetadataTest(unittest.TestCase):
    def test_token_is_only_in_publish_step(self) -> None:
        text = (ROOT / "action.yml").read_text(encoding="utf-8")
        prepare, publish = text.split(
            "    - name: Publish exact verified delta",
            maxsplit=1,
        )

        self.assertNotIn("${{ inputs.token }}", prepare)
        self.assertIn('GH_TOKEN: ""', prepare)
        self.assertEqual(2, prepare.count('INPUT_TOKEN: ""'))
        self.assertIn(
            'GH_TOKEN: "${{ inputs.token }}"',
            publish,
        )

    def test_action_has_typed_transaction_inputs(self) -> None:
        text = (ROOT / "action.yml").read_text(encoding="utf-8")
        required = (
            "modified:",
            "paths:",
            "files-from0:",
            "languages:",
            "hook:",
            "labels:",
            "reviewers:",
            "state-path:",
            "verification-path:",
        )
        for value in required:
            self.assertIn(value, text)


class RepositoryPathTest(unittest.TestCase):
    def test_windows_git_symlink_placeholder_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills" / "auto-lint-pr").mkdir(parents=True)
            skill = root / "skill"
            skill.write_text("skills/auto-lint-pr\n", encoding="utf-8")

            verify_repo.verify_skill_entry(skill, "nt")

            with self.assertRaisesRegex(
                ValueError,
                "skill must be a symbolic link",
            ):
                verify_repo.verify_skill_entry(skill, "posix")


class WorkflowMetadataTest(unittest.TestCase):
    def test_reusable_workflow_has_safety_controls(self) -> None:
        path = ROOT / ".github" / "workflows" / "auto-lint-pr.yml"
        text = path.read_text(encoding="utf-8")

        self.assertIn("workflow_call:", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("pull-requests: write", text)
        self.assertIn("issues: write", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("secrets.checkout_token", text)
        self.assertIn('token: "${{ github.token }}"', text)
        self.assertIn(
            'repository: "${{ job.workflow_repository }}"',
            text,
        )
        self.assertIn('ref: "${{ job.workflow_sha }}"', text)
        self.assertNotIn("github.workflow_sha", text)
        self.assertNotIn("secrets.token", text)
        self.assertNotIn("pull_request_target", text)
        self.assertIn("needs: prepare", text)
        self.assertIn("actions/upload-artifact@", text)
        self.assertIn("actions/download-artifact@", text)
        prepare, publish = text.split("\n  publish:\n", maxsplit=1)
        self.assertNotIn('token: "${{ github.token }}"', prepare)
        self.assertIn('token: "${{ github.token }}"', publish)

    def test_readme_requires_caller_write_permissions(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        marker = "The calling job must grant the workflow's write permissions"
        self.assertIn(marker, text)
        reusable = text.split("## Reusable workflow", maxsplit=1)[1]
        self.assertIn("contents: write", reusable)
        self.assertIn("issues: write", reusable)
        self.assertIn("pull-requests: write", reusable)
        self.assertIn("Allow GitHub Actions to create and approve", reusable)
        self.assertIn("pull requests", reusable)

    def test_ci_exercises_token_free_auto_lint_transaction(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("python3 auto_lint_pr.py prepare", text)
        self.assertIn("--lint-root ../lint", text)
        self.assertIn("--cwd fixtures/integration", text)
        self.assertIn("--language requirements", text)
        self.assertIn("--local", text)
        self.assertIn(
            "python3 ../verification-source/auto_lint_pr.py verify",
            text,
        )
        self.assertIn("--restore", text)
        self.assertIn("verification-source", text)
        self.assertIn("auto-lint-pr-verified.json", text)
        self.assertIn("state_sha256", text)
        self.assertIn("token-free verification receipt", text)
        self.assertIn('GITHUB_TOKEN: ""', text)
        self.assertIn('GH_TOKEN: ""', text)
        self.assertIn(
            '"fixtures/integration/requirements.txt"',
            text,
        )
        self.assertNotIn("uses: trycopilotai/lint@", text)

    def test_external_actions_use_commit_references(self) -> None:
        pattern = re.compile(r"uses:\s+([^@\s]+)@([^\s]+)")
        workflows = (ROOT / ".github" / "workflows").glob("*.yml")
        matches = 0
        for path in workflows:
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                matches += 1
                self.assertRegex(match.group(2), r"^[0-9a-f]{40}$")

        self.assertGreater(matches, 0)

    def test_release_verifies_the_ssh_signed_tag(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        allowed = (ROOT / ".github" / "release-allowed-signers").read_text(
            encoding="utf-8"
        )

        self.assertIn("gpg.format=ssh", workflow)
        self.assertIn(
            "gpg.ssh.allowedSignersFile=.github/release-allowed-signers",
            workflow,
        )
        self.assertIn('verify-tag "$RELEASE_REF"', workflow)
        self.assertIn("release-commit:", workflow)
        self.assertIn("needs.verify.outputs.release-commit", workflow)
        self.assertIn('ref: "${{ needs.verify.outputs.release-commit }}"', workflow)
        self.assertIn('"$RELEASE_COMMIT"', workflow)
        self.assertIn("python3 tools/verify_release.py --ref", workflow)
        self.assertIn('"$RELEASE_REF"', workflow)
        self.assertRegex(
            allowed,
            r"^trycopilotai-release ssh-ed25519 " r"[A-Za-z0-9+/]+={0,2}\n$",
        )
        self.assertEqual(1, len(allowed.splitlines()))
        self.assertEqual(3, len(allowed.rstrip("\n").split(" ")))

    def test_release_version_closure_matches_v010(self) -> None:
        valid = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "verify_release.py"),
                "--ref",
                "v0.1.0",
            ],
            cwd=ROOT,
            check=False,
        )
        invalid = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "verify_release.py"),
                "--ref",
                "v0.1.1",
            ],
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(0, valid.returncode)
        self.assertNotEqual(0, invalid.returncode)


class InstallMetadataTest(unittest.TestCase):
    def test_product_installs_pin_the_release_tag(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(text.count("release=v0.1.0"), 2)
        self.assertIn(
            "/auto-lint-pr/archive/refs/tags/$release.tar.gz",
            text,
        )

    def test_reusable_workflow_install_is_executable(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("<full-commit-sha>", text)
        self.assertIn(
            "trycopilotai/auto-lint-pr/.github/workflows/" "auto-lint-pr.yml@v0.1.0",
            text,
        )


class LaunchSurfaceTest(unittest.TestCase):
    def test_readme_has_icon_and_runnable_workflow_badges(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn('src="assets/icon.svg"', text)
        for workflow in (
            "ci.yml",
            "release.yml",
        ):
            badge = f"actions/workflows/{workflow}/badge.svg"
            self.assertEqual(1, text.count(badge))
        reusable_badge = "actions/workflows/auto-lint-pr.yml/badge.svg"
        self.assertNotIn(reusable_badge, text)

    def test_demo_is_transcript_derived_and_accessible(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        transcript = (ROOT / "evidence" / "demo-transcript.txt").read_text(
            encoding="utf-8"
        )
        demo = (ROOT / "assets" / "demo.svg").read_text(encoding="utf-8")

        self.assertIn("assets/demo.svg", readme)
        self.assertIn("assets/poster.svg", readme)
        self.assertIn("<picture>", readme)
        self.assertIn(
            'media="(prefers-reduced-motion: reduce)"',
            readme,
        )
        self.assertIn('srcset="assets/poster.svg"', readme)
        self.assertIn("Reconstructed", readme)
        lines = transcript.rstrip("\n").splitlines()
        for line in lines:
            self.assertIn(html.escape(line), demo)
        self.assertEqual(len(lines), demo.count("@keyframes reveal-"))
        self.assertIn("prefers-reduced-motion: reduce", demo)
        self.assertIn("animation: none", demo)

        manifest = json.loads(
            (ROOT / "evidence" / "demo-manifest.json").read_text(encoding="utf-8")
        )
        run = manifest["run"]
        self.assertEqual("generate_demo.py", run["agent"])
        self.assertEqual("1.0.0", run["agent_version"])
        self.assertEqual("2026-08-02", run["date"])
        self.assertFalse(run["edited"])
        self.assertEqual("./scripts/demo.sh", run["invocation"])
        self.assertRegex(run["input_commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(run["lint_commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(run["output_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(run["protocol_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            manifest["output"]["sha256"],
            run["output_sha256"],
        )
        self.assertEqual(
            manifest["skill"]["sha256"],
            run["protocol_sha256"],
        )

    def test_comparison_metric_and_article_are_present(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        article = ROOT / "docs" / "exact-delta-boundary.md"

        self.assertIn("Reviewed 2026-08-02", readme)
        self.assertIn(
            "peter-evans/create-pull-request/blob/"
            "11fa467881691ac900904a2eea702c5ea848ad13/README.md",
            readme,
        )
        self.assertIn(
            "Launch success is one external repository completing",
            readme,
        )
        self.assertTrue(article.is_file())
        self.assertIn(
            "This is a draft about that boundary.",
            article.read_text(encoding="utf-8"),
        )

    def test_issue_infrastructure_uses_documented_labels(self) -> None:
        labels = (ROOT / ".github" / "labels.yml").read_text(encoding="utf-8")
        documents = [
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("README.md", "CONTRIBUTING.md", "SECURITY.md")
        ]
        for label in (
            "good first issue",
            "transaction-boundary",
            "consumer-integration",
        ):
            self.assertEqual(1, labels.count(f"name: {label}"))
            for document in documents:
                self.assertIn(f"`{label}`", document)
        for name in (
            "config.yml",
            "consumer-integration.yml",
            "transaction-bug.yml",
        ):
            self.assertTrue((ROOT / ".github" / "ISSUE_TEMPLATE" / name).is_file())


if __name__ == "__main__":
    unittest.main()
