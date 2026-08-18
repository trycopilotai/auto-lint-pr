#!/usr/bin/env python3
"""Verify repository invariants outside the unit surfaces."""

from __future__ import annotations

import ast
import hashlib
import html
import json
import os
import re
import shlex
import struct
import subprocess
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINT_COMMIT = "7856512a84d96ff796cc10975e219b5b2a0836f3"
LINT_MANIFEST = "lint-release-manifest.json"
LINT_REF = "refs/tags/v0.1.7"
LINT_REPOSITORY = "https://github.com/trycopilotai/lint"
LINT_TAG_OBJECT = "db4807fc428cf5696fa5d6725ea048a9569613a6"
LINT_TREE = "f17bf3b94bcf0f7aef01774d3fe0215d36d9fdd9"
LINT_ALLOWED_SIGNERS_SHA256 = (
    "986887b24aba7609ef5086f1571de978bc2dbcd5f952efc21dbb127e9c1205f7"
)
LINT_LANGUAGE_IDS = {
    "bazel",
    "c",
    "cpp",
    "csharp",
    "css",
    "go",
    "html",
    "java",
    "javascript",
    "json",
    "julia",
    "kotlin",
    "less",
    "markdown",
    "objective-c",
    "objective-cpp",
    "plist",
    "python",
    "requirements",
    "rust",
    "scss",
    "shell",
    "swift",
    "toml",
    "tsx",
    "typescript",
    "xml",
    "yaml",
}


def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous checked-in trust documents."""

    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def load_trust_json(path: Path) -> dict[str, object]:
    """Load one trust document with duplicate-key rejection."""

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_pairs,
    )
    if not isinstance(value, dict):
        raise ValueError(f"trust document must be an object: {path.name}")
    return value


def verify_skill_entry(skill: Path, platform_name: str) -> None:
    expected_target = "skills/auto-lint-pr"
    expected = (skill.parent / expected_target).resolve()
    if skill.is_symlink():
        if skill.resolve() != expected:
            raise ValueError("skill symbolic link has the wrong target")
        return
    if platform_name == "nt" and skill.is_file():
        target = skill.read_text(encoding="utf-8").strip()
        if target == expected_target:
            return
    raise ValueError("skill must be a symbolic link")


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
        ".github/lint-release-allowed-signers",
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
        "tools/prefetch_images.py",
        "tools/verify_release.py",
    )
    for relative in required:
        if not (ROOT / relative).is_file():
            raise ValueError(f"required file is missing: {relative}")

    verify_skill_entry(ROOT / "skill", os.name)


def verify_python(files: list[Path]) -> None:
    for path in files:
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.IfExp):
                raise ValueError(f"ternary expression: {path}:{node.lineno}")


def readme_claim() -> str:
    """Return the README's first-screen claim as plain text."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^Turn pinned.*?(?=\n\n)", readme, re.S | re.M)
    if match is None:
        raise ValueError("README first-screen claim is missing")
    claim = re.sub(r"\[`?([^\]`]+)`?\]\([^)]*\)", r"\1", match.group(0))
    return " ".join(claim.split())


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
        if value.get("description") != readme_claim():
            raise ValueError(
                f"plugin description differs from the README claim: {relative}"
            )


def verify_lint_manifest(path: Path | None = None) -> None:
    if path is None:
        path = ROOT / LINT_MANIFEST
    value = load_trust_json(path)
    if value.get("schema_version") != 1:
        raise ValueError("lint release manifest must be repinned to final schema 1")
    expected_fields = {
        "images",
        "release",
        "schema_version",
        "source",
        "tools",
    }
    if set(value) != expected_fields:
        raise ValueError("lint release manifest has unexpected fields")
    source = value.get("source")
    if not isinstance(source, dict):
        raise ValueError("lint release manifest has no source")
    if source.get("commit") != LINT_COMMIT:
        raise ValueError("lint release manifest has the wrong commit")
    expected_source_fields = {"archive", "commit", "sha256"}
    if set(source) != expected_source_fields:
        raise ValueError("lint release source has unexpected fields")
    digest = source.get("sha256")
    if not isinstance(digest, str):
        raise ValueError("lint release archive has no checksum")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("lint release archive checksum is invalid")
    release = value.get("release")
    if not isinstance(release, str) or not release:
        raise ValueError("lint release manifest has no release")
    # LINT_REF is the single pinned source of truth and is rewritten
    # by the repin transaction. Deriving the expected release from it
    # keeps a second literal from drifting out of step with the pin.
    if LINT_REF != f"refs/tags/v{release}":
        raise ValueError("lint release manifest and ref do not match")
    if source.get("archive") != f"lint-{release}.tar.gz":
        raise ValueError("lint release archive name does not match")
    tools = value.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise ValueError("lint release manifest has no tool record")
    for tool, version in tools.items():
        if not isinstance(tool, str) or not isinstance(version, str):
            raise ValueError("lint release tool records must be strings")
    images = value.get("images")
    if not isinstance(images, dict):
        raise ValueError("lint release manifest has no image record")
    expected_images = {
        f"ghcr.io/trycopilotai/lint-{language}" for language in LINT_LANGUAGE_IDS
    }
    if set(images) != expected_images:
        raise ValueError("lint release manifest image coverage is incomplete")
    for image, image_digest in images.items():
        if not image.startswith("ghcr.io/trycopilotai/lint-"):
            raise ValueError(f"unexpected lint image: {image}")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
            raise ValueError(f"invalid lint image digest: {image}")


def verify_lint_dependency() -> None:
    path = ROOT / "lint-dependency.json"
    value = load_trust_json(path)
    expected = {
        "allowed_signers_sha256": LINT_ALLOWED_SIGNERS_SHA256,
        "commit": LINT_COMMIT,
        "manifest": LINT_MANIFEST,
        "manifest_sha256": sha256(ROOT / LINT_MANIFEST),
        "ref": LINT_REF,
        "repository": LINT_REPOSITORY,
        "schema": 2,
        "signer": "trycopilotai-release",
        "tag_object": LINT_TAG_OBJECT,
        "tree": LINT_TREE,
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

    for name in ("auto-lint-pr.yml", "ci.yml"):
        workflow = ROOT / ".github" / "workflows" / name
        if LINT_REF not in workflow.read_text(encoding="utf-8"):
            raise ValueError(f"workflow does not pin the lint release ref: {name}")
    continuous_integration = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    for required in (
        "python3 auto_lint_pr.py prepare",
        "python3 ../verification-source/auto_lint_pr.py verify",
        "--lint-root ../lint",
        "--cwd fixtures/integration",
        "--language requirements",
        "--local",
        "--restore",
        "verification-source",
        "auto-lint-pr-verified.json",
        "state_sha256",
        "token-free verification receipt",
        'GITHUB_TOKEN: ""',
        'GH_TOKEN: ""',
        '"fixtures/integration/requirements.txt"',
        "pinned-lint-docker:",
        "Authenticated exact-digest prefetch",
        "tools/prefetch_images.py",
        'docker pull "$image"',
        "docker logout ghcr.io",
        "grep -F -q 'ghcr.io'",
        'DOCKER_CONFIG: "${{ runner.temp }}/docker-clean"',
        "formatter-must-not-receive",
        'state["lint_release"]["images"][name]',
    ):
        if required not in continuous_integration:
            raise ValueError(f"CI token-free fixture is missing: {required}")
    if "uses: trycopilotai/lint@" in continuous_integration:
        raise ValueError("CI bypasses the auto-lint-pr transaction")
    reusable = (ROOT / ".github" / "workflows" / "auto-lint-pr.yml").read_text(
        encoding="utf-8"
    )
    if "secrets.checkout_token" not in reusable:
        raise ValueError("reusable workflow has no private checkout token")
    if "secrets.registry_token" not in reusable:
        raise ValueError("reusable workflow has no private registry token")
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
    if "\n  verify:\n" not in reusable:
        raise ValueError("reusable workflow has no isolated verify job")
    if "\n  publish:\n" not in reusable:
        raise ValueError("reusable workflow has no isolated publish job")
    prepare, remaining = reusable.split("\n  verify:\n", maxsplit=1)
    verify, publish = remaining.split("\n  publish:\n", maxsplit=1)
    if "packages: read" not in prepare:
        raise ValueError("prepare job cannot read private lint images")
    if "Prefetch private images by exact digest" not in prepare:
        raise ValueError("prepare job does not prefetch private lint images")
    if "tools/prefetch_images.py" not in prepare:
        raise ValueError("prepare job bypasses the trusted image resolver")
    scoped_logout = 'DOCKER_CONFIG="$auth" "$docker_path" logout ghcr.io'
    if scoped_logout not in prepare:
        raise ValueError("prepare job does not use scoped registry logout")
    if "grep -F -q 'ghcr.io'" not in prepare:
        raise ValueError("prepare job could expose residual registry credentials")
    if 'DOCKER_CONFIG: "${{ runner.temp }}/docker-clean"' not in prepare:
        raise ValueError("formatter does not use a clean Docker configuration")
    if prepare.count("workspace-root: workspace") != 1:
        raise ValueError("prepare action has no checkout boundary")
    if 'token: "${{ github.token }}"' in prepare:
        raise ValueError("prepare job receives the publication token")
    if 'token: "${{ github.token }}"' in verify:
        raise ValueError("verify job receives the publication token")
    if 'token: "${{ github.token }}"' not in publish:
        raise ValueError("publish job does not receive the repository token")
    checkout_token = "secrets.checkout_token || github.token"
    if verify.count("actions/checkout@") != 2:
        raise ValueError("verify job checkout count is wrong")
    if verify.count(checkout_token) != 1:
        raise ValueError("verify job action checkout credential is wrong")
    if "phase: verify" not in verify:
        raise ValueError("verify job does not run the verification phase")
    if verify.count("workspace-root: workspace") != 1:
        raise ValueError("verify action has no checkout boundary")
    if "consumer-base.bundle" not in verify:
        raise ValueError("verify job does not package the exact consumer base")
    if "auto-lint-pr.tar.gz" not in verify:
        raise ValueError("verify job does not package the pinned action")
    if "actions/upload-artifact@" not in verify:
        raise ValueError("verify job does not upload publication inputs")
    if "actions/checkout@" in publish:
        raise ValueError("publish job performs a network checkout")
    if checkout_token in publish:
        raise ValueError("checkout credential enters the publish job")
    if "secrets.registry_token" in publish:
        raise ValueError("registry credential enters the publish job")
    if publish.count("workspace-root: workspace") != 1:
        raise ValueError("publish action has no checkout boundary")
    for required in (
        "action-archive-sha",
        "consumer-bundle-sha",
        "state-sha",
        "verification-sha",
        "auto-lint-pr-publication",
        'GH_TOKEN: ""',
        'GITHUB_TOKEN: ""',
        "phase: publish",
    ):
        if required not in publish:
            raise ValueError(f"publish package boundary is missing: {required}")
    if "actions/upload-artifact@" not in prepare:
        raise ValueError("prepare job does not upload its state artifact")
    if "actions/download-artifact@" not in publish:
        raise ValueError("publish job does not download publication inputs")
    if "- prepare" not in publish or "- verify" not in publish:
        raise ValueError("publish job is not ordered after verification")
    if "issues: write" not in publish:
        raise ValueError("publish job cannot apply labels")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    for required in (
        "gpg.format=ssh",
        "gpg.ssh.allowedSignersFile=",
        "cc4d422edf9f081ffcba6efa003b4340cd132167",
        "release-trust/.github/release-allowed-signers",
        "sparse-checkout-cone-mode: false",
        "working-directory: release-source",
        'verify-tag "$RELEASE_REF"',
        "release-commit:",
        "release-tag-object:",
        "needs.verify.outputs.release-commit",
        "needs.verify.outputs.release-tag-object",
        'ref: "${{ needs.verify.outputs.release-commit }}"',
        '"$RELEASE_COMMIT"',
        '"$RELEASE_TAG_OBJECT"',
        'rev-parse "$RELEASE_REF^{tag}"',
        'test "$remote_tag_object" = "$RELEASE_TAG_OBJECT"',
        "python3 tools/verify_release.py --ref",
    ):
        if required not in release:
            raise ValueError("release workflow does not verify the signed tag")
    allowed = (ROOT / ".github" / "release-allowed-signers").read_text(encoding="utf-8")
    allowed_pattern = re.compile(
        r"trycopilotai-release ssh-ed25519 " r"[A-Za-z0-9+/]+={0,2}\n"
    )
    if allowed_pattern.fullmatch(allowed) is None:
        raise ValueError("release signer allowlist is invalid")
    if len(allowed.splitlines()) != 1:
        raise ValueError("release signer allowlist must have one record")
    if len(allowed.rstrip("\n").split(" ")) != 3:
        raise ValueError("release signer allowlist has extra fields")
    lint_allowed_path = ROOT / ".github" / "lint-release-allowed-signers"
    lint_allowed = lint_allowed_path.read_text(encoding="utf-8")
    if allowed_pattern.fullmatch(lint_allowed) is None:
        raise ValueError("lint release signer allowlist is invalid")
    if sha256(lint_allowed_path) != LINT_ALLOWED_SIGNERS_SHA256:
        raise ValueError("lint release signer allowlist checksum is wrong")


def verify_action_boundary() -> None:
    text = (ROOT / "action.yml").read_text(encoding="utf-8")
    if "workspace-root:" not in text:
        raise ValueError("action does not declare a checkout boundary")
    if text.count("INPUT_WORKSPACE_ROOT:") != 3:
        raise ValueError("action phases do not receive the checkout boundary")
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
    entrypoint = (ROOT / "action_entrypoint.py").read_text(encoding="utf-8")
    if "INPUT_CWD must stay within INPUT_WORKSPACE_ROOT" not in entrypoint:
        raise ValueError("action entrypoint does not enforce the checkout boundary")
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
        "ci.yml",
        "release.yml",
    ):
        badge = f"actions/workflows/{workflow}/badge.svg"
        if readme.count(badge) != 1:
            raise ValueError(f"README badge count is wrong: {workflow}")
    reusable_badge = "actions/workflows/auto-lint-pr.yml/badge.svg"
    if reusable_badge in readme:
        raise ValueError("README badges a reusable-only workflow")
    for required in (
        "assets/demo.svg",
        "assets/poster.svg",
        "<picture>",
        'media="(prefers-reduced-motion: reduce)"',
        'srcset="assets/poster.svg"',
        "Reconstructed",
        "Reviewed 2026-08-02",
        "peter-evans/create-pull-request/blob/"
        "11fa467881691ac900904a2eea702c5ea848ad13/README.md",
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


# Prose that defers work until a future public launch. The
# repository is public, so each of these is false on a
# recipient-visible surface.
LAUNCH_STATE_PHRASES = (
    "after public launch",
    "separately approved public launch",
    "deferred public launch",
    "visibility private",
    "the private repository plan",
)

RECIPIENT_VISIBLE_DOCUMENTS = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/publishing.md",
    "docs/exact-delta-boundary.md",
    "skills/auto-lint-pr/SKILL.md",
)


ISO_DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def verify_public_wording() -> None:
    """Reject launch-state contradictions on reader-visible prose.

    The 60-column Markdown codec wraps these phrases across
    lines, so each document is matched with its whitespace
    collapsed rather than line by line.
    """

    for name in RECIPIENT_VISIBLE_DOCUMENTS:
        path = ROOT / name
        if not path.exists():
            raise ValueError(f"recipient-visible document is missing: {name}")
        collapsed = " ".join(path.read_text(encoding="utf-8").split()).lower()
        for phrase in LAUNCH_STATE_PHRASES:
            if phrase in collapsed:
                raise ValueError(f"{name} defers a launch that happened: {phrase}")


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
        "implementation",
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

    run = value.get("run")
    if not isinstance(run, dict):
        raise ValueError("demo manifest has no run provenance")
    expected_fields = {
        "agent",
        "agent_version",
        "date",
        "edited",
        "fixture_consumer_commit",
        "fixture_lint_commit",
        "input_commit",
        "invocation",
        "output_sha256",
        "protocol_sha256",
    }
    if set(run) != expected_fields:
        raise ValueError("demo run provenance is incomplete or inconsistent")
    for key in (
        "agent",
        "agent_version",
        "invocation",
    ):
        if not isinstance(run[key], str) or run[key] == "":
            raise ValueError(f"demo run value is invalid: {key}")
    if run["agent"] == "generate_demo.py":
        raise ValueError("demo run agent identifies the generator")
    date_value = run["date"]
    if (
        not isinstance(date_value, str)
        or ISO_DATE_PATTERN.fullmatch(date_value) is None
    ):
        raise ValueError("demo run date is invalid")
    try:
        date.fromisoformat(date_value)
    except ValueError as error:
        raise ValueError("demo run date is not a real date") from error
    try:
        invocation_parts = shlex.split(run["invocation"])
    except ValueError as error:
        raise ValueError("demo invocation is not parseable") from error
    if "--evidence-date" not in invocation_parts:
        raise ValueError("demo invocation does not record --evidence-date")
    date_index = invocation_parts.index("--evidence-date")
    if (
        date_index + 1 >= len(invocation_parts)
        or invocation_parts[date_index + 1] != date_value
    ):
        raise ValueError("demo run date disagrees with its invocation")
    if run["edited"] is not False:
        raise ValueError("demo run edited flag is invalid")
    for key in (
        "fixture_consumer_commit",
        "fixture_lint_commit",
        "input_commit",
    ):
        if not isinstance(run[key], str):
            raise ValueError(f"demo run commit is invalid: {key}")
        if re.fullmatch(r"[0-9a-f]{40}", run[key]) is None:
            raise ValueError(f"demo run commit is invalid: {key}")
    invocation = shlex.split(run["invocation"])
    for option, key in (
        ("--agent", "agent"),
        ("--agent-version", "agent_version"),
        ("--input-commit", "input_commit"),
    ):
        try:
            recorded = invocation[invocation.index(option) + 1]
        except (IndexError, ValueError) as error:
            raise ValueError(f"demo invocation is missing: {option}") from error
        if recorded != run[key]:
            raise ValueError(f"demo invocation disagrees with: {option}")
    evidence_commit = subprocess.run(
        [
            "git",
            "log",
            "-1",
            "--format=%H",
            "--",
            str(path.relative_to(ROOT)),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    expected_input_commit = subprocess.run(
        ["git", "rev-parse", f"{evidence_commit}^"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    if run["input_commit"] != expected_input_commit:
        raise ValueError("demo input commit is not the evidence predecessor")
    if run["output_sha256"] != value["output"]["sha256"]:
        raise ValueError("demo output checksum provenance is inconsistent")
    if run["protocol_sha256"] != value["skill"]["sha256"]:
        raise ValueError("demo protocol checksum provenance is inconsistent")

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
    verify_public_wording()
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
