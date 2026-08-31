from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BUMP_FILES = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".github/workflows/release.yml",
    "README.md",
    "tests/test_repository.py",
    "tools/bump_version.py",
    "tools/verify_repo.py",
)

REPIN_FILES = (
    ".github/lint-release-allowed-signers",
    ".github/workflows/auto-lint-pr.yml",
    ".github/workflows/ci.yml",
    "auto_lint_pr.py",
    "lint-dependency.json",
    "lint-release-manifest.json",
    "tools/repin_lint.py",
    "tools/verify_repo.py",
)


def copy_tree(relatives: tuple[str, ...], destination: Path) -> None:
    """Copy repository files into a fixture tree, normalized to LF.

    The tools write LF and the repository commits LF, so the
    round-trip contract is over LF bytes. A Windows working tree
    can arrive CRLF-smudged by runner git configuration; that is a
    checkout artifact, not tool behavior, so it is normalized away
    at the copy boundary instead of failing the byte comparison.
    """

    for relative in relatives:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = (ROOT / relative).read_bytes().replace(b"\r\n", b"\n")
        target.write_bytes(payload)


def read_all(root: Path, relatives: tuple[str, ...]) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for relative in relatives:
        contents[relative] = (root / relative).read_bytes()
    return contents


def run_tool(
    root: Path,
    relative: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / relative), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def run_git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *arguments,
        ],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def pinned_lint_tag() -> str:
    dependency = json.loads((ROOT / "lint-dependency.json").read_text(encoding="utf-8"))
    return dependency["ref"].rsplit("/", maxsplit=1)[1]


def lint_checkout_with_tag(tag: str) -> Path | None:
    """Find a real lint checkout that holds the pinned tag."""

    override = os.environ.get("AUTO_LINT_PR_LINT_ROOT")
    if override is not None:
        candidates = [Path(override)]
    else:
        candidates = [ROOT.parent / "lint"]
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(candidate),
                "rev-parse",
                "--verify",
                f"refs/tags/{tag}^{{tag}}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode == 0:
            return candidate
    return None


def make_unsigned_lint_release(directory: Path, tag: str) -> Path:
    """Create a lint-shaped repository with an unsigned tag."""

    repository = directory / "unsigned-lint"
    repository.mkdir()
    run_git(repository, "init", "--quiet")
    signers = repository / ".github" / "release-allowed-signers"
    signers.parent.mkdir()
    signers.write_text(
        "trycopilotai-release ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlaceholder\n",
        encoding="utf-8",
    )
    run_git(repository, "add", ".github/release-allowed-signers")
    run_git(repository, "commit", "--quiet", "-m", "release contents")
    run_git(repository, "tag", "-a", tag, "-m", f"release {tag}")
    return repository


def current_version(root: Path) -> str:
    manifest = json.loads(
        (root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    return manifest["version"]


def next_patch(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


class BumpVersionTest(unittest.TestCase):
    """Version-agnostic on purpose.

    These tests derive every version from the checked-in plugin
    manifest instead of hardcoding one, so that running the bump
    tool for a real release does not turn this suite red at the
    bumped version - which is exactly the commit the release tag
    points at.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="bump-version-test-")
        self.addCleanup(scratch.cleanup)
        self.copy_root = Path(scratch.name)
        copy_tree(BUMP_FILES, self.copy_root)
        self.original = read_all(self.copy_root, BUMP_FILES)
        self.old = current_version(self.copy_root)
        self.new = next_patch(self.old)
        self.after = next_patch(self.new)

    def test_bump_and_restore_round_trips(self) -> None:
        bumped = run_tool(self.copy_root, "tools/bump_version.py", self.new)
        self.assertEqual(0, bumped.returncode, bumped.stderr)
        plugin = (self.copy_root / ".claude-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'"version": "{self.new}"', plugin)
        readme = (self.copy_root / "README.md").read_text(encoding="utf-8")
        self.assertEqual(2, readme.count(f"release=v{self.new}"))
        self.assertEqual(1, readme.count(f"auto-lint-pr.yml@v{self.new}"))
        tests = (self.copy_root / "tests" / "test_repository.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(1, tests.count(f'"v{self.new}"'))
        self.assertEqual(1, tests.count(f'"v{self.after}"'))

        restored = run_tool(self.copy_root, "tools/bump_version.py", self.old)
        self.assertEqual(0, restored.returncode, restored.stderr)
        self.assertEqual(self.original, read_all(self.copy_root, BUMP_FILES))

    def test_bump_refuses_on_count_mismatch(self) -> None:
        readme = self.copy_root / "README.md"
        text = readme.read_text(encoding="utf-8")
        readme.write_text(
            text.replace(f"release=v{self.old}", "release=v9.9.9", 1),
            encoding="utf-8",
        )
        tampered = read_all(self.copy_root, BUMP_FILES)

        completed = run_tool(self.copy_root, "tools/bump_version.py", self.new)

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("standalone installs", completed.stderr)
        self.assertEqual(tampered, read_all(self.copy_root, BUMP_FILES))

    def test_bump_refuses_a_non_semantic_version(self) -> None:
        completed = run_tool(
            self.copy_root,
            "tools/bump_version.py",
            f"v{self.new}",
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertEqual(self.original, read_all(self.copy_root, BUMP_FILES))


class RepinLintTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="repin-lint-test-")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.copy_root = self.scratch / "repository"
        self.copy_root.mkdir()
        copy_tree(REPIN_FILES, self.copy_root)
        self.original = read_all(self.copy_root, REPIN_FILES)

    def test_repin_to_the_current_tag_is_a_noop(self) -> None:
        tag = pinned_lint_tag()
        lint_root = lint_checkout_with_tag(tag)
        if lint_root is None:
            self.skipTest(f"no lint checkout with {tag} is available")

        completed = run_tool(
            self.copy_root,
            "tools/repin_lint.py",
            tag,
            "--lint-root",
            str(lint_root),
            "--manifest",
            str(self.copy_root / "lint-release-manifest.json"),
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(self.original, read_all(self.copy_root, REPIN_FILES))

    def test_repin_refuses_on_count_mismatch(self) -> None:
        lint_root = make_unsigned_lint_release(self.scratch, "v9.9.9")
        verifier = self.copy_root / "tools" / "verify_repo.py"
        dependency = json.loads(
            (self.copy_root / "lint-dependency.json").read_text(encoding="utf-8")
        )
        with verifier.open("a", encoding="utf-8") as stream:
            stream.write(f"# duplicate site: {dependency['commit']}\n")
        tampered = read_all(self.copy_root, REPIN_FILES)

        completed = run_tool(
            self.copy_root,
            "tools/repin_lint.py",
            "v9.9.9",
            "--lint-root",
            str(lint_root),
            "--manifest",
            str(self.copy_root / "lint-release-manifest.json"),
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("LINT_COMMIT", completed.stderr)
        self.assertEqual(tampered, read_all(self.copy_root, REPIN_FILES))

    def test_repin_refuses_an_unverified_tag(self) -> None:
        lint_root = make_unsigned_lint_release(self.scratch, "v9.9.9")

        completed = run_tool(
            self.copy_root,
            "tools/repin_lint.py",
            "v9.9.9",
            "--lint-root",
            str(lint_root),
            "--manifest",
            str(self.copy_root / "lint-release-manifest.json"),
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("signature", completed.stderr)
        self.assertEqual(self.original, read_all(self.copy_root, REPIN_FILES))


if __name__ == "__main__":
    unittest.main()
