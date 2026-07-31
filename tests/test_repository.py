from __future__ import annotations

import html
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
        self.assertRegex(
            allowed,
            r"^trycopilotai-release ssh-ed25519 " r"[A-Za-z0-9+/]+={0,2}\n$",
        )
        self.assertNotIn("origin", allowed.casefold())


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
    def test_readme_has_icon_and_one_badge_per_workflow(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn('src="assets/icon.svg"', text)
        for workflow in (
            "auto-lint-pr.yml",
            "ci.yml",
            "release.yml",
        ):
            badge = f"actions/workflows/{workflow}/badge.svg"
            self.assertEqual(1, text.count(badge))

    def test_demo_is_transcript_derived_and_accessible(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        transcript = (ROOT / "evidence" / "demo-transcript.txt").read_text(
            encoding="utf-8"
        )
        demo = (ROOT / "assets" / "demo.svg").read_text(encoding="utf-8")

        self.assertIn("assets/demo.svg", readme)
        self.assertIn("assets/poster.svg", readme)
        self.assertIn("Reconstructed", readme)
        lines = transcript.rstrip("\n").splitlines()
        for line in lines:
            self.assertIn(html.escape(line), demo)
        self.assertEqual(len(lines), demo.count("@keyframes reveal-"))
        self.assertIn("prefers-reduced-motion: reduce", demo)
        self.assertIn("animation: none", demo)

    def test_comparison_metric_and_article_are_present(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        article = ROOT / "docs" / "exact-delta-boundary.md"

        self.assertIn("Reviewed 2026-07-31", readme)
        self.assertIn(
            "peter-evans/create-pull-request/blob/"
            "7ec5aae3c91d101b005af46adc760d265911886a/README.md",
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
