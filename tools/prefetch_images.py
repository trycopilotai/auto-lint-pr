#!/usr/bin/env python3
"""Resolve authenticated lint image references before token-free execution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auto_lint_pr  # noqa: E402


def languages(value: str) -> list[str]:
    """Parse the reusable workflow's comma-separated language input."""

    result = []
    for item in value.split(","):
        item = item.strip()
        if item != "":
            result.append(item)
    return result


def main() -> int:
    """Verify the controller binding and print exact image references."""

    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--lint-root", required=True)
    argument_parser.add_argument("--manifest", required=True)
    argument_parser.add_argument("--dependency", required=True)
    argument_parser.add_argument("--allowed-signers", required=True)
    argument_parser.add_argument("--languages", default="")
    arguments = argument_parser.parse_args()
    try:
        manifest = auto_lint_pr.verify_lint_release(
            Path(arguments.lint_root),
            Path(arguments.manifest),
            Path(arguments.dependency),
            Path(arguments.allowed_signers),
        )
        binding = manifest.get("verified_dependency")
        if not isinstance(binding, dict):
            raise auto_lint_pr.DependencyError(
                "verified lint dependency binding is missing"
            )
        digests = binding.get("images")
        if not isinstance(digests, dict):
            raise auto_lint_pr.DependencyError(
                "verified lint image digest set is missing"
            )
        selected = auto_lint_pr.selected_image_digests(
            digests,
            languages(arguments.languages),
        )
    except auto_lint_pr.AutoLintError as error:
        print(str(error), file=sys.stderr)
        return 1
    for image in sorted(selected):
        print(f"{image}@{selected[image]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
