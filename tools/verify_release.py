#!/usr/bin/env python3
"""Verify that one release tag matches every shipped version surface."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_PATTERN = re.compile(
    r"v(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def plugin_version(relative: str) -> str:
    """Return one plugin manifest's semantic version."""

    path = ROOT / relative
    value = json.loads(path.read_text(encoding="utf-8"))
    version = value.get("version")
    if not isinstance(version, str) or version == "":
        raise ValueError(f"plugin manifest has no version: {relative}")
    return version


def verify_release(release_ref: str) -> None:
    """Require the tag, manifests, workflow, and installs to agree."""

    match = RELEASE_PATTERN.fullmatch(release_ref)
    if match is None:
        raise ValueError("release ref must be a complete semantic version tag")
    version = release_ref.removeprefix("v")
    manifests = (
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
    )
    for relative in manifests:
        if plugin_version(relative) != version:
            raise ValueError(f"release ref does not match {relative}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if readme.count(f"release={release_ref}") != 2:
        raise ValueError("release ref does not match both standalone installs")
    workflow_pin = (
        "trycopilotai/auto-lint-pr/.github/workflows/" f"auto-lint-pr.yml@{release_ref}"
    )
    if readme.count(workflow_pin) != 1:
        raise ValueError("release ref does not match the reusable workflow pin")

    release_workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    if release_workflow.count(f"default: {release_ref}") != 1:
        raise ValueError("release ref does not match the release workflow default")


def main() -> int:
    """Parse a release ref and verify its complete version closure."""

    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--ref", required=True)
    arguments = argument_parser.parse_args()
    try:
        verify_release(arguments.ref)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error))
        return 1
    print(json.dumps({"release": arguments.ref, "status": "ok"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
