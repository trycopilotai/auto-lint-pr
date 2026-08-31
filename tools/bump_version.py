#!/usr/bin/env python3
"""Move the release version across every shipped surface.

One release version is pinned by the two plugin manifests,
the README install snippets, the release workflow default,
the repository verifier, and the version-closure test. This
tool derives the current version from the Claude plugin
manifest, rewrites every site to the requested version, and
refuses to write anything unless every site count matches.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
VALID_SENTINEL = "\x00bump-version-valid\x00"
INVALID_SENTINEL = "\x00bump-version-invalid\x00"


class BumpError(ValueError):
    """Raised when the version closure cannot be rewritten."""


def next_patch(version: str) -> str:
    """Return the next patch release after one version."""

    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def current_version() -> str:
    """Read the shipped version from the Claude plugin manifest."""

    path = ROOT / ".claude-plugin" / "plugin.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    version = value.get("version")
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise BumpError(f"plugin manifest has no semantic version: {path}")
    return version


def replace_counted(
    text: str,
    old: str,
    new: str,
    expected: int,
    description: str,
) -> str:
    """Replace an exact string after checking its site count."""

    actual = text.count(old)
    if actual != expected:
        raise BumpError(
            f"{description}: expected {expected} " f"site(s) of {old!r}, found {actual}"
        )
    return text.replace(old, new)


def build_test_source(old: str, new: str) -> str:
    """Rewrite the version-closure test literals.

    The valid-ref literal becomes the new version and the
    invalid-ref literal becomes the next patch after it, so the
    test keeps asserting that ``tools/verify_release.py``
    accepts the real version and rejects a different one. The
    two literals are swapped through sentinels because the new
    valid literal can equal the old invalid literal.
    """

    relative = "tests/test_repository.py"
    text = (ROOT / relative).read_text(encoding="utf-8")
    text = replace_counted(
        text,
        f'"v{next_patch(old)}"',
        INVALID_SENTINEL,
        1,
        f"{relative} invalid-ref literal",
    )
    text = replace_counted(
        text,
        f'"v{old}"',
        VALID_SENTINEL,
        1,
        f"{relative} valid-ref literal",
    )
    text = text.replace(INVALID_SENTINEL, f'"v{next_patch(new)}"')
    text = text.replace(VALID_SENTINEL, f'"v{new}"')
    text = replace_counted(
        text,
        f"release=v{old}",
        f"release=v{new}",
        1,
        f"{relative} standalone-install literal",
    )
    text = replace_counted(
        text,
        f"auto-lint-pr.yml@v{old}",
        f"auto-lint-pr.yml@v{new}",
        1,
        f"{relative} reusable-workflow literal",
    )
    return text


def build_sources(old: str, new: str) -> dict[str, str]:
    """Build every rewritten file, refusing on any count drift."""

    sources: dict[str, str] = {}
    for relative in (
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        sources[relative] = replace_counted(
            text,
            f'"version": "{old}"',
            f'"version": "{new}"',
            1,
            f"{relative} version field",
        )

    relative = "README.md"
    text = (ROOT / relative).read_text(encoding="utf-8")
    text = replace_counted(
        text,
        f"auto-lint-pr.yml@v{old}",
        f"auto-lint-pr.yml@v{new}",
        1,
        f"{relative} reusable-workflow pin",
    )
    sources[relative] = replace_counted(
        text,
        f"release=v{old}",
        f"release=v{new}",
        2,
        f"{relative} standalone installs",
    )

    relative = ".github/workflows/release.yml"
    text = (ROOT / relative).read_text(encoding="utf-8")
    sources[relative] = replace_counted(
        text,
        f"default: v{old}",
        f"default: v{new}",
        1,
        f"{relative} workflow_dispatch default",
    )

    relative = "tools/verify_repo.py"
    text = (ROOT / relative).read_text(encoding="utf-8")
    text = replace_counted(
        text,
        f'"{old}"',
        f'"{new}"',
        1,
        f"{relative} plugin-version literal",
    )
    sources[relative] = replace_counted(
        text,
        f"auto-lint-pr.yml@v{old}",
        f"auto-lint-pr.yml@v{new}",
        1,
        f"{relative} reusable-workflow pin",
    )

    sources["tests/test_repository.py"] = build_test_source(old, new)
    return sources


def bump(new: str) -> None:
    """Rewrite the release version closure to one new version."""

    if VERSION_PATTERN.fullmatch(new) is None:
        raise BumpError(f"version is not a semantic version: {new}")
    old = current_version()
    sources = build_sources(old, new)
    for relative, text in sources.items():
        (ROOT / relative).write_text(text, encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {"new": new, "old": old, "status": "ok"},
            sort_keys=True,
        )
    )


def main() -> int:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("version", help="new version, e.g. 0.1.1")
    arguments = argument_parser.parse_args()
    try:
        bump(arguments.version)
    except (BumpError, OSError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
