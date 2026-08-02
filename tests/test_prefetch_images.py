from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import prefetch_images  # noqa: E402


class PrefetchImagesTest(unittest.TestCase):
    def test_languages_are_trimmed(self) -> None:
        self.assertEqual(
            ["python", "requirements"],
            prefetch_images.languages(" python, ,requirements "),
        )

    def test_main_prints_only_selected_exact_references(self) -> None:
        manifest = {
            "verified_dependency": {
                "images": {
                    "ghcr.io/trycopilotai/lint-python": "sha256:" + "a" * 64,
                    "ghcr.io/trycopilotai/lint-requirements": "sha256:" + "b" * 64,
                }
            }
        }
        arguments = [
            "prefetch_images.py",
            "--lint-root",
            "/lint",
            "--manifest",
            "/controller/manifest.json",
            "--dependency",
            "/controller/dependency.json",
            "--allowed-signers",
            "/controller/allowed-signers",
            "--languages",
            "requirements",
        ]
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", arguments),
            mock.patch.object(
                prefetch_images.auto_lint_pr,
                "verify_lint_release",
                return_value=manifest,
            ) as verify,
            contextlib.redirect_stdout(output),
        ):
            status = prefetch_images.main()

        self.assertEqual(0, status)
        self.assertEqual(
            "ghcr.io/trycopilotai/lint-requirements@sha256:" + "b" * 64 + "\n",
            output.getvalue(),
        )
        verify.assert_called_once_with(
            Path("/lint"),
            Path("/controller/manifest.json"),
            Path("/controller/dependency.json"),
            Path("/controller/allowed-signers"),
        )


if __name__ == "__main__":
    unittest.main()
