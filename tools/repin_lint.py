#!/usr/bin/env python3
"""Rewrite every pinned lint-release surface in one transaction.

The repository pins one lint release across five surfaces: the
dependency ledger, the copied release manifest, the copied
allowed-signers file, the constants block in
``tools/verify_repo.py``, and the workflow checkout refs. This
tool recomputes all of them from a lint checkout and a release
manifest asset, verifies the signed release tag with
``auto_lint_pr.verify_lint_release`` first, and refuses to
write anything unless every surface can be rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import auto_lint_pr  # noqa: E402
from tools import verify_repo  # noqa: E402


TAG_PATTERN = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
MANIFEST_NAME = "lint-release-manifest.json"
DEPENDENCY_NAME = "lint-dependency.json"
ALLOWED_SIGNERS_SOURCE = ".github/release-allowed-signers"
ALLOWED_SIGNERS_COPY = ".github/lint-release-allowed-signers"
LINT_REPOSITORY_SLUG = "trycopilotai/lint"
WORKFLOW_REF_COUNTS = (
    (".github/workflows/ci.yml", 2),
    (".github/workflows/auto-lint-pr.yml", 1),
)


class RepinError(ValueError):
    """Raised when the repin transaction cannot proceed."""


def run_git(repository: Path, *arguments: str) -> str:
    """Run one read-only Git command inside a repository."""

    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
        raise RepinError(
            f"{description}: expected {expected} " f"site(s) of {old!r}, found {actual}"
        )
    return text.replace(old, new)


def fetch_manifest_with_gh(tag: str, directory: Path) -> Path:
    """Download the release manifest asset with the gh CLI."""

    subprocess.run(
        [
            "gh",
            "release",
            "download",
            tag,
            "-R",
            LINT_REPOSITORY_SLUG,
            "--pattern",
            MANIFEST_NAME,
            "--dir",
            str(directory),
        ],
        check=True,
    )
    return directory / MANIFEST_NAME


def clone_at_tag(lint_root: Path, tag: str, directory: Path) -> Path:
    """Clone the lint checkout at one release tag, detached."""

    clone_root = directory / "lint"
    completed = subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--config",
            "advice.detachedHead=false",
            "--branch",
            tag,
            str(lint_root),
            str(clone_root),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise RepinError(
            f"cloning the lint checkout at {tag} failed: " f"{completed.stderr.strip()}"
        )
    return clone_root


def resolve_release(clone_root: Path, tag: str) -> dict[str, str]:
    """Resolve the tag's commit, tag object, and tree."""

    ref = f"refs/tags/{tag}"
    tag_object = subprocess.run(
        ["git", "-C", str(clone_root), "rev-parse", f"{ref}^{{tag}}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if tag_object.returncode != 0:
        raise RepinError(f"{ref} is not an annotated tag")
    commit = run_git(clone_root, "rev-parse", f"{ref}^{{commit}}")
    tree = run_git(clone_root, "rev-parse", f"{ref}^{{tree}}")
    head = run_git(clone_root, "rev-parse", "HEAD")
    if head != commit:
        raise RepinError("lint clone is not checked out at the release commit")
    return {
        "commit": commit,
        "ref": ref,
        "tag_object": tag_object.stdout.strip(),
        "tree": tree,
    }


def build_dependency(
    release: dict[str, str],
    manifest_bytes: bytes,
    signers_bytes: bytes,
) -> dict[str, object]:
    """Build the dependency ledger for one resolved release."""

    return {
        "allowed_signers_sha256": sha256_bytes(signers_bytes),
        "commit": release["commit"],
        "manifest": MANIFEST_NAME,
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "ref": release["ref"],
        "repository": verify_repo.LINT_REPOSITORY,
        "schema": 2,
        "signer": "trycopilotai-release",
        "tag_object": release["tag_object"],
        "tree": release["tree"],
    }


def render_dependency(dependency: dict[str, object]) -> bytes:
    return (json.dumps(dependency, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build_verify_repo_source(tag: str, release: dict[str, str]) -> str:
    """Rewrite the constants block by exact string replacement."""

    source = (ROOT / "tools" / "verify_repo.py").read_text(encoding="utf-8")
    replacements = (
        (verify_repo.LINT_COMMIT, release["commit"], "LINT_COMMIT"),
        (verify_repo.LINT_REF, release["ref"], "LINT_REF"),
        (verify_repo.LINT_TAG_OBJECT, release["tag_object"], "LINT_TAG_OBJECT"),
        (verify_repo.LINT_TREE, release["tree"], "LINT_TREE"),
        (
            verify_repo.LINT_ALLOWED_SIGNERS_SHA256,
            release["allowed_signers_sha256"],
            "LINT_ALLOWED_SIGNERS_SHA256",
        ),
    )
    for old, new, name in replacements:
        source = replace_counted(source, old, new, 1, f"tools/verify_repo.py {name}")
    version = tag.removeprefix("v")
    occurrences = source.count(version)
    if occurrences != 1:
        raise RepinError(
            "the pinned release version must appear exactly once in "
            f"tools/verify_repo.py, found {occurrences} sites of {version!r}"
        )
    return source


def build_workflow_sources(release: dict[str, str]) -> dict[str, str]:
    """Rewrite the pinned checkout ref in each workflow."""

    old_line = f"ref: {verify_repo.LINT_REF}"
    new_line = f"ref: {release['ref']}"
    sources: dict[str, str] = {}
    for relative, expected in WORKFLOW_REF_COUNTS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        sources[relative] = replace_counted(
            text,
            old_line,
            new_line,
            expected,
            relative,
        )
    return sources


def verify_release_pin(
    clone_root: Path,
    dependency_bytes: bytes,
    manifest_bytes: bytes,
    signers_bytes: bytes,
    stage: Path,
) -> None:
    """Verify the signed release before any surface is written."""

    dependency_path = stage / DEPENDENCY_NAME
    manifest_path = stage / MANIFEST_NAME
    signers_path = stage / "lint-release-allowed-signers"
    dependency_path.write_bytes(dependency_bytes)
    manifest_path.write_bytes(manifest_bytes)
    signers_path.write_bytes(signers_bytes)
    auto_lint_pr.verify_lint_release(
        clone_root,
        manifest_path,
        dependency_path,
        signers_path,
    )


def repin(tag: str, lint_root: Path, manifest_path: Path | None) -> None:
    """Recompute and rewrite every pinned lint-release surface."""

    if TAG_PATTERN.fullmatch(tag) is None:
        raise RepinError(f"release tag is not a semantic version tag: {tag}")
    if not lint_root.is_dir():
        raise RepinError(f"lint checkout does not exist: {lint_root}")
    with tempfile.TemporaryDirectory(prefix="repin-lint-") as scratch:
        scratch_path = Path(scratch)
        if manifest_path is None:
            manifest_path = fetch_manifest_with_gh(tag, scratch_path)
        manifest_bytes = manifest_path.read_bytes()
        clone_root = clone_at_tag(lint_root, tag, scratch_path)
        signers_source = clone_root / ALLOWED_SIGNERS_SOURCE
        if signers_source.is_symlink() or not signers_source.is_file():
            raise RepinError(f"lint release has no {ALLOWED_SIGNERS_SOURCE} file")
        signers_bytes = signers_source.read_bytes()
        release = resolve_release(clone_root, tag)
        release["allowed_signers_sha256"] = sha256_bytes(signers_bytes)
        dependency = build_dependency(release, manifest_bytes, signers_bytes)
        dependency_bytes = render_dependency(dependency)
        verify_repo_source = build_verify_repo_source(tag, release)
        workflow_sources = build_workflow_sources(release)
        stage = scratch_path / "stage"
        stage.mkdir()
        verify_release_pin(
            clone_root,
            dependency_bytes,
            manifest_bytes,
            signers_bytes,
            stage,
        )
    (ROOT / DEPENDENCY_NAME).write_bytes(dependency_bytes)
    (ROOT / MANIFEST_NAME).write_bytes(manifest_bytes)
    (ROOT / ALLOWED_SIGNERS_COPY).write_bytes(signers_bytes)
    (ROOT / "tools" / "verify_repo.py").write_text(
        verify_repo_source,
        encoding="utf-8",
    )
    for relative, text in workflow_sources.items():
        (ROOT / relative).write_text(text, encoding="utf-8")
    print(
        json.dumps(
            {
                "commit": release["commit"],
                "ref": release["ref"],
                "status": "ok",
            },
            sort_keys=True,
        )
    )


def main() -> int:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("tag", help="lint release tag, e.g. v0.1.7")
    argument_parser.add_argument(
        "--lint-root",
        required=True,
        type=Path,
        help="path to a trycopilotai/lint checkout with full history and tags",
    )
    argument_parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "path to the release's manifest asset; downloaded with "
            "the gh CLI when absent"
        ),
    )
    arguments = argument_parser.parse_args()
    try:
        repin(arguments.tag, arguments.lint_root, arguments.manifest)
    except (
        RepinError,
        auto_lint_pr.DependencyError,
        OSError,
        subprocess.CalledProcessError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
