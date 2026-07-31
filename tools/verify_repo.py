#!/usr/bin/env python3
"""Verify repository invariants outside the unit surfaces."""

from __future__ import annotations

import ast
import hashlib
import html
import json
import re
import struct
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINT_COMMIT = "3d5d4ee7b83b2c6442039b8a72a571c729ffcead"
LINT_MANIFEST = "lint-release-manifest.json"
LINT_REF = "refs/tags/v0.1.4"
LINT_REPOSITORY = "https://github.com/trycopilotai/lint"


def repository_files() -> list[Path]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    files: list[Path] = []
    seen: set[str] = set()
    for value in completed.stdout.split(b"\0"):
        if value == b"":
            continue
        relative = value.decode("utf-8")
        if relative in seen:
            continue
        seen.add(relative)
        path = ROOT / relative
        if path.is_file():
            files.append(path)
    return files


def verify_required_paths() -> None:
    required = (
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        ".github/actionlint.yaml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/consumer-integration.yml",
        ".github/ISSUE_TEMPLATE/transaction-bug.yml",
        ".github/labels.yml",
        ".github/release-allowed-signers",
        ".github/workflows/auto-lint-pr.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        "CONTRIBUTING.md",
        "LICENSE",
        "Makefile",
        "README.md",
        "SECURITY.md",
        "action.yml",
        "action_entrypoint.py",
        "assets/icon.svg",
        "auto_lint_pr.py",
        "docs/exact-delta-boundary.md",
        "lint-dependency.json",
        "lint-release-manifest.json",
        "skills/auto-lint-pr/SKILL.md",
    )
    for relative in required:
        if not (ROOT / relative).is_file():
            raise ValueError(f"required file is missing: {relative}")

    skill = ROOT / "skill"
    if not skill.is_symlink():
        raise ValueError("skill must be a symbolic link")
    expected = (ROOT / "skills" / "auto-lint-pr").resolve()
    if skill.resolve() != expected:
        raise ValueError("skill symbolic link has the wrong target")


def verify_python(files: list[Path]) -> None:
    for path in files:
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.IfExp):
                raise ValueError(f"ternary expression: {path}:{node.lineno}")


def verify_plugins() -> None:
    for relative in (
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
    ):
        path = ROOT / relative
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("name") != "auto-lint-pr":
            raise ValueError(f"wrong plugin name: {relative}")
        if value.get("version") != "0.1.0":
            raise ValueError(f"wrong plugin version: {relative}")


def verify_lint_manifest() -> None:
    path = ROOT / LINT_MANIFEST
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1:
        raise ValueError("lint release manifest has the wrong schema")
    source = value.get("source")
    if not isinstance(source, dict):
        raise ValueError("lint release manifest has no source")
    if source.get("commit") != LINT_COMMIT:
        raise ValueError("lint release manifest has the wrong commit")
    digest = source.get("sha256")
    if not isinstance(digest, str):
        raise ValueError("lint release archive has no checksum")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("lint release archive checksum is invalid")
    images = value.get("images")
    if not isinstance(images, dict):
        raise ValueError("lint release manifest has no image digests")
    if len(images) != 26:
        raise ValueError("lint release manifest must bind 26 images")
    for image, image_digest in images.items():
        if not image.startswith("ghcr.io/trycopilotai/lint-"):
            raise ValueError(f"unexpected lint image: {image}")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
            raise ValueError(f"invalid lint image digest: {image}")
    if value.get("release") != "0.1.4":
        raise ValueError("lint release manifest has the wrong release")


def verify_lint_dependency() -> None:
    path = ROOT / "lint-dependency.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "commit": LINT_COMMIT,
        "manifest": LINT_MANIFEST,
        "manifest_sha256": sha256(ROOT / LINT_MANIFEST),
        "ref": LINT_REF,
        "repository": LINT_REPOSITORY,
        "schema": 1,
    }
    if value != expected:
        raise ValueError("lint dependency ledger does not match the release")


def verify_actions(files: list[Path]) -> None:
    reference = re.compile(r"uses:\s+([^@\s]+)@([^\s]+)")
    full_sha = re.compile(r"[0-9a-f]{40}")
    workflows = [
        path for path in files if path.parent == ROOT / ".github" / "workflows"
    ]
    if len(workflows) != 3:
        raise ValueError("expected three GitHub Actions workflows")
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        if "pull_request_target" in text:
            raise ValueError(f"pull_request_target is not allowed: {path}")
        if "persist-credentials: true" in text:
            raise ValueError(f"persistent checkout credentials are not allowed: {path}")
        for match in reference.finditer(text):
            if full_sha.fullmatch(match.group(2)) is None:
                raise ValueError(
                    f"action is not commit-pinned: {path}: " f"{match.group(1)}"
                )

    combined = "\n".join(path.read_text(encoding="utf-8") for path in workflows)
    if LINT_COMMIT not in combined:
        raise ValueError("workflows do not pin the lint release commit")
    reusable = (ROOT / ".github" / "workflows" / "auto-lint-pr.yml").read_text(
        encoding="utf-8"
    )
    if "secrets.checkout_token" not in reusable:
        raise ValueError("reusable workflow has no private checkout token")
    if 'token: "${{ github.token }}"' not in reusable:
        raise ValueError("publication does not use the repository token")
    if 'repository: "${{ job.workflow_repository }}"' not in reusable:
        raise ValueError("reusable workflow does not check out its own repository")
    if 'ref: "${{ job.workflow_sha }}"' not in reusable:
        raise ValueError("reusable workflow does not use its own revision")
    if "github.workflow_sha" in reusable:
        raise ValueError("reusable workflow uses the caller workflow revision")
    if "secrets.token" in reusable:
        raise ValueError("one credential cannot serve checkout and publication")
    if "\n  publish:\n" not in reusable:
        raise ValueError("reusable workflow has no isolated publish job")
    prepare, publish = reusable.split("\n  publish:\n", maxsplit=1)
    if 'token: "${{ github.token }}"' in prepare:
        raise ValueError("prepare job receives the publication token")
    if 'token: "${{ github.token }}"' not in publish:
        raise ValueError("publish job does not receive the repository token")
    if "actions/upload-artifact@" not in prepare:
        raise ValueError("prepare job does not upload its state artifact")
    if "actions/download-artifact@" not in publish:
        raise ValueError("publish job does not download its state artifact")
    if "needs: prepare" not in publish:
        raise ValueError("publish job is not ordered after prepare")
    if "issues: write" not in publish:
        raise ValueError("publish job cannot apply labels")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    for required in (
        "gpg.format=ssh",
        "gpg.ssh.allowedSignersFile=.github/release-allowed-signers",
        'verify-tag "$RELEASE_REF"',
    ):
        if required not in release:
            raise ValueError("release workflow does not verify the signed tag")
    allowed = (ROOT / ".github" / "release-allowed-signers").read_text(encoding="utf-8")
    allowed_pattern = re.compile(
        r"trycopilotai-release ssh-ed25519 " r"[A-Za-z0-9+/]+={0,2}\n"
    )
    if allowed_pattern.fullmatch(allowed) is None:
        raise ValueError("release signer allowlist is invalid")
    if "origin" in allowed.casefold():
        raise ValueError("release signer allowlist exposes an internal label")


def verify_action_boundary() -> None:
    text = (ROOT / "action.yml").read_text(encoding="utf-8")
    marker = "    - name: Publish exact verified delta"
    if marker not in text:
        raise ValueError("action publish step is missing")
    prepare, publish = text.split(marker, maxsplit=1)
    if "${{ inputs.token }}" in prepare:
        raise ValueError("prepare step references the write token")
    if 'GH_TOKEN: ""' not in prepare:
        raise ValueError("prepare step does not clear GH_TOKEN")
    if 'GITHUB_TOKEN: ""' not in prepare:
        raise ValueError("prepare step does not clear GITHUB_TOKEN")
    if prepare.count('INPUT_TOKEN: ""') != 2:
        raise ValueError("token-free steps do not clear the action token input")
    if "    - name: Restore and verify exact prepared delta" not in prepare:
        raise ValueError("action does not verify the delta before token injection")
    if 'GH_TOKEN: "${{ inputs.token }}"' not in publish:
        raise ValueError("publish step does not receive the write token")
    implementation = (ROOT / "auto_lint_pr.py").read_text(encoding="utf-8")
    for name in ("GITHUB_ACTION_PATH", "GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH"):
        if f'"{name}"' not in implementation:
            raise ValueError(f"token-free child environment retains {name}")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    setting = "Allow GitHub Actions to create and approve"
    if setting not in readme:
        raise ValueError("README omits the pull-request creation setting")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_launch_surface() -> None:
    """Verify the recipient-visible launch surface."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if 'src="assets/icon.svg"' not in readme:
        raise ValueError("README does not display the project icon")
    for workflow in (
        "auto-lint-pr.yml",
        "ci.yml",
        "release.yml",
    ):
        badge = f"actions/workflows/{workflow}/badge.svg"
        if readme.count(badge) != 1:
            raise ValueError(f"README badge count is wrong: {workflow}")
    for required in (
        "assets/demo.svg",
        "assets/poster.svg",
        "Reconstructed",
        "Reviewed 2026-07-31",
        "peter-evans/create-pull-request/blob/"
        "7ec5aae3c91d101b005af46adc760d265911886a/README.md",
        "Launch success is one external repository completing",
    ):
        if required not in readme:
            raise ValueError(f"README launch surface is missing: {required}")
    if "<full-commit-sha>" in readme:
        raise ValueError("README retains an unresolved commit placeholder")
    if (
        "trycopilotai/auto-lint-pr/.github/workflows/" "auto-lint-pr.yml@v0.1.0"
    ) not in readme:
        raise ValueError("README reusable workflow install is not pinned")

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
        if labels.count(f"name: {label}") != 1:
            raise ValueError(f"label definition is wrong: {label}")
        for document in documents:
            if f"`{label}`" not in document:
                raise ValueError(f"label is undocumented: {label}")

    article = (ROOT / "docs" / "exact-delta-boundary.md").read_text(encoding="utf-8")
    if "This is a draft about that boundary." not in article:
        raise ValueError("technical article is not marked as a draft")


def verify_demo() -> None:
    path = ROOT / "evidence" / "demo-manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != 1:
        raise ValueError("demo manifest has the wrong schema")
    for key in (
        "output",
        "demo",
        "poster",
        "social_preview_source",
        "social_preview",
        "invocation",
        "generator",
        "skill",
    ):
        record = value.get(key)
        if not isinstance(record, dict):
            raise ValueError(f"demo manifest is missing: {key}")
        relative = record.get("path")
        expected = record.get("sha256")
        if not isinstance(relative, str):
            raise ValueError(f"demo path is invalid: {key}")
        if not isinstance(expected, str):
            raise ValueError(f"demo checksum is invalid: {key}")
        if sha256(ROOT / relative) != expected:
            raise ValueError(f"demo checksum is stale: {key}")

    preview = ROOT / value["social_preview"]["path"]
    payload = preview.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("social preview is not a PNG")
    width, height = struct.unpack(">II", payload[16:24])
    if (width, height) != (1280, 640):
        raise ValueError("social preview must be 1280 by 640")

    transcript = (ROOT / value["output"]["path"]).read_text(encoding="utf-8")
    demo = (ROOT / value["demo"]["path"]).read_text(encoding="utf-8")
    lines = transcript.rstrip("\n").splitlines()
    for line in lines:
        if html.escape(line) not in demo:
            raise ValueError("demo does not contain its transcript")
    if demo.count("@keyframes reveal-") != len(lines):
        raise ValueError("demo animation is not cumulative per line")
    if "prefers-reduced-motion: reduce" not in demo:
        raise ValueError("demo has no reduced-motion behavior")
    if "animation: none" not in demo:
        raise ValueError("demo cannot turn animation off")

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_demo.py"),
            "--check",
        ],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("demo evidence is not reproducible")


def main() -> int:
    files = repository_files()
    verify_required_paths()
    verify_python(files)
    verify_plugins()
    verify_lint_manifest()
    verify_lint_dependency()
    verify_actions(files)
    verify_action_boundary()
    verify_launch_surface()
    verify_demo()
    print(
        json.dumps(
            {
                "status": "ok",
                "tracked_files": len(files),
                "workflows": 3,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
