from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ActionMetadataTest(unittest.TestCase):
    def test_token_is_only_in_publish_step(self) -> None:
        text = (ROOT / "action.yml").read_text(encoding="utf-8")
        prepare, publish = text.split(
            "    - name: Publish exact prepared delta",
            maxsplit=1,
        )

        self.assertNotIn("${{ inputs.token }}", prepare)
        self.assertIn('GH_TOKEN: ""', prepare)
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
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("secrets.checkout_token", text)
        self.assertIn('token: "${{ github.token }}"', text)
        self.assertNotIn("secrets.token", text)
        self.assertNotIn("pull_request_target", text)

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


class InstallMetadataTest(unittest.TestCase):
    def test_product_installs_pin_the_release_tag(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(text.count("release=v0.1.0"), 2)
        self.assertIn(
            "/auto-lint-pr/archive/refs/tags/$release.tar.gz",
            text,
        )


if __name__ == "__main__":
    unittest.main()
