#!/usr/bin/env python3
"""Translate composite-action inputs into the Python CLI."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def boolean_input(name: str, default: bool = False) -> bool:
    default_value = "false"
    if default:
        default_value = "true"
    value = os.environ.get(name, default_value).strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no", ""}:
        return False
    raise ValueError(f"{name} must be true or false")


def comma_values(name: str) -> list[str]:
    values: list[str] = []
    for value in os.environ.get(name, "").split(","):
        value = value.strip()
        if value != "":
            values.append(value)
    return values


def action_cwd() -> str:
    """Keep action phases inside the trusted consumer checkout."""

    boundary = Path(os.environ.get("INPUT_WORKSPACE_ROOT", ".")).resolve()
    cwd = Path(os.environ.get("INPUT_CWD", ".")).resolve()
    if not cwd.is_relative_to(boundary):
        raise ValueError("INPUT_CWD must stay within INPUT_WORKSPACE_ROOT")
    return str(cwd)


def action_workspace_root() -> str:
    """Return the canonical boundary independently supplied to the CLI."""

    boundary = Path(os.environ.get("INPUT_WORKSPACE_ROOT", ".")).resolve()
    return str(boundary)


def command(phase: str) -> list[str]:
    action_path = Path(os.environ["GITHUB_ACTION_PATH"])
    arguments = [
        sys.executable,
        str(action_path / "auto_lint_pr.py"),
        phase,
        "--cwd",
        action_cwd(),
        "--workspace-root",
        action_workspace_root(),
        "--base",
        os.environ.get("INPUT_BASE", "main"),
        "--repository",
        os.environ["GITHUB_REPOSITORY"],
        "--state",
        os.environ["STATE_PATH"],
    ]
    if phase == "prepare":
        arguments.extend(
            [
                "--lint-root",
                os.environ["LINT_ROOT"],
                "--manifest",
                str(action_path / "lint-release-manifest.json"),
                "--dependency",
                str(action_path / "lint-dependency.json"),
                "--allowed-signers",
                str(action_path / ".github" / "lint-release-allowed-signers"),
            ]
        )
    if phase in {"verify", "publish"}:
        arguments.extend(
            [
                "--verification",
                os.environ["VERIFICATION_PATH"],
            ]
        )
    if phase == "verify":
        arguments.append("--restore")
        return arguments
    for label in comma_values("INPUT_LABELS"):
        arguments.extend(["--label", label])
    for reviewer in comma_values("INPUT_REVIEWERS"):
        arguments.extend(["--reviewer", reviewer])
    title = os.environ.get("INPUT_TITLE", "")
    if title != "":
        arguments.extend(["--title", title])
    body = os.environ.get("INPUT_BODY", "")
    if body != "":
        arguments.extend(["--body", body])
    if phase == "publish":
        return arguments

    if not boolean_input("INPUT_DOCKER", default=True):
        arguments.append("--local")
    for language in comma_values("INPUT_LANGUAGES"):
        arguments.extend(["--language", language])
    hook = os.environ.get("INPUT_HOOK", "")
    if hook != "":
        arguments.extend(["--hook", hook])
    print_width = os.environ.get("INPUT_PRINT_WIDTH", "")
    if print_width != "":
        arguments.extend(["--print-width", print_width])
    paths = os.environ.get("INPUT_PATHS", "")
    files_from0 = os.environ.get("INPUT_FILES_FROM0", "")
    modified = boolean_input("INPUT_MODIFIED")
    selected = 0
    if paths != "":
        selected += 1
    if files_from0 != "":
        selected += 1
    if modified:
        selected += 1
    if selected > 1:
        raise ValueError("selection inputs are mutually exclusive")
    if paths != "":
        arguments.extend(shlex.split(paths))
    elif files_from0 != "":
        arguments.extend(["--files-from0", files_from0])
    elif modified:
        arguments.append("--modified")
    else:
        arguments.append("--all")
    return arguments


def main(argv: list[str]) -> int:
    phases = {"prepare", "verify", "publish"}
    if len(argv) != 2 or argv[1] not in phases:
        print("usage: action_entrypoint.py prepare|verify|publish", file=sys.stderr)
        return 2
    try:
        completed = subprocess.run(command(argv[1]), check=False)
    except (KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
