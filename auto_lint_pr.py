#!/usr/bin/env python3
"""Turn pinned lint output into a reviewable pull request."""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "lint-release-manifest.json"
DEFAULT_DEPENDENCY = ROOT / "lint-dependency.json"
DEFAULT_ALLOWED_SIGNERS = ROOT / ".github" / "lint-release-allowed-signers"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
BOT_LOGIN = "github-actions[bot]"
# GitHub commits on your behalf when it signs for you, so a commit
# made through `createCommitOnBranch` records GitHub's own signing
# identity as the committer rather than the account that asked for
# it. The account is still the author, which is where provenance
# lives. Accepting this identity is only safe in the presence of
# `wasSignedByGitHub`, which is why the committer is checked after
# the signature and not before.
GITHUB_SIGNER_EMAIL = "noreply@github.com"
GITHUB_SIGNER_LOGIN = "web-flow"
# A refusal that names no way forward strands the operator on the
# failure they are most likely to hit first. Each remedy below
# states only what a human may do. This tool never deletes
# branches; it resets its own stale branch only when every
# branch-only commit passes the bot-ownership and signature audit,
# so a branch that fails that audit is always a human act.
PR_CREATION_FORBIDDEN_MARKER = "is not permitted to create or approve pull requests"
PR_CREATION_SETTING_MESSAGE = (
    "GitHub refused to let Actions open the pull request. Turn on "
    '"Allow GitHub Actions to create and approve pull requests" in '
    "the repository or organization Actions settings. The "
    "publication branch and its signed commit already exist, so "
    "rerunning publish once the setting is on completes the "
    "publication."
)
CONTENTS_WRITE_MESSAGE = (
    "branch creation was refused with HTTP 403; the calling "
    "workflow job must grant contents: write, because a reusable "
    "workflow cannot elevate the caller's token"
)
STALE_BRANCH_REMEDY = (
    "delete or rename the auto-lint branch and rerun; this tool "
    "never deletes branches"
)
EXISTING_PR_REMEDY = (
    "close that pull request or align it with this base and head, " "then rerun"
)
CREATED_COMMIT_REMEDY = (
    "the commit this tool just created failed identity validation; "
    "GitHub's signing identity may have changed, so report this "
    "instead of deleting anything"
)
TOKEN_NAMES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
)
ACTION_COMMAND_NAMES = (
    "BASH_ENV",
    "ENV",
    "GITHUB_ACTION_PATH",
    "GITHUB_ENV",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "GITHUB_STATE",
    "GITHUB_STEP_SUMMARY",
    "PYTHONHOME",
    "PYTHONPATH",
)
REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/" r"[A-Za-z0-9_.-]+"
)
GIT_OBJECT_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
IMAGE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_PREFIX = "ghcr.io/trycopilotai/lint-"
LINT_LANGUAGE_IDS = frozenset(
    {
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
)


class AutoLintError(Exception):
    """Base error for the auto-lint transaction."""


def error_chain_text(error: BaseException) -> str:
    """Return the error text joined with every chained cause.

    Wrapping layers preserve the underlying GitHub diagnostic in
    `__cause__` but `str(error)` shows only the outermost message.
    Refusal handling matches against the whole chain so a marker in
    the original API text is still reachable after re-wrapping.
    """

    parts: list[str] = []
    current: BaseException | None = error
    while current is not None:
        parts.append(str(current))
        current = current.__cause__
    return "; ".join(parts)


class SafetyError(AutoLintError):
    """A branch, pull request, or delta failed a safety check."""


class StaleBranchError(SafetyError):
    """A retained auto-lint branch no longer descends from the base.

    The branch comparison found a status other than "ahead": the
    base branch moved, by an ordinary push or by merging a previous
    auto-lint pull request, while the tool's branch lingered. The
    error carries the comparison's merge base so publish can audit
    the branch-only commits and recover with an audited reset
    instead of stranding the operator on a refusal.
    """

    def __init__(self, message: str, merge_base: str) -> None:
        super().__init__(message)
        self.merge_base = merge_base


class DependencyError(AutoLintError):
    """The pinned lint dependency could not be verified."""


class CommandError(AutoLintError):
    """A formatter, hook, Git, or GitHub command failed."""


PHASES = ("prepare", "verify", "publish")


class PhaseAndPathParser(argparse.ArgumentParser):
    """Split the phase from the paths after parsing, not during.

    Python 3.11 matches positionals one contiguous run at a time,
    so `prepare --language python src/a.py` gives the first run to
    both positionals, leaves the paths in a later run with nothing
    to match, and fails with `unrecognized arguments`. 3.12 changed
    this. The supported versions therefore disagree about a command
    line the tool documents, and the only one that rejected it was
    the oldest one the matrix tests.

    Collecting a single positional list and splitting it here
    behaves identically on every supported version. The phase is
    still validated, and still refused when it is not one of the
    three, so nothing about the accepted command line changes.
    """

    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        # `parse_known_args` is what makes the two runs reachable.
        # Positionals separated by an option land in a later run
        # that the declared positional has already been filled
        # from, so they arrive here as leftovers instead.
        parsed, leftover = self.parse_known_args(args, namespace)
        unknown_options = [token for token in leftover if token.startswith("-")]
        if unknown_options:
            self.error("unrecognized arguments: " + " ".join(unknown_options))
        words = list(getattr(parsed, "phase_and_paths", []))
        words.extend(token for token in leftover if not token.startswith("-"))
        phase = "prepare"
        if words:
            if words[0] not in PHASES:
                self.error(
                    "argument phase: invalid choice: "
                    f"{words[0]!r} (choose from "
                    + ", ".join(repr(name) for name in PHASES)
                    + ")"
                )
            phase = words.pop(0)
        parsed.phase = phase
        parsed.paths = words
        del parsed.phase_and_paths
        return parsed


def parser() -> argparse.ArgumentParser:
    argument_parser = PhaseAndPathParser(
        description="Format a repository and publish the exact delta as a PR",
    )
    argument_parser.add_argument(
        "phase_and_paths",
        nargs="*",
        metavar="[{prepare,verify,publish}] [paths ...]",
    )
    argument_parser.add_argument("--cwd")
    argument_parser.add_argument("--workspace-root")
    argument_parser.add_argument("--lint-root")
    argument_parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
    )
    argument_parser.add_argument(
        "--dependency",
        default=str(DEFAULT_DEPENDENCY),
    )
    argument_parser.add_argument(
        "--allowed-signers",
        default=str(DEFAULT_ALLOWED_SIGNERS),
    )
    backend = argument_parser.add_mutually_exclusive_group()
    backend.add_argument(
        "--docker",
        dest="docker",
        action="store_true",
    )
    backend.add_argument(
        "--local",
        dest="docker",
        action="store_false",
    )
    argument_parser.set_defaults(docker=True)
    selection = argument_parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--modified", action="store_true")
    selection.add_argument("--files-from0")
    argument_parser.add_argument(
        "--language",
        action="append",
        default=[],
    )
    argument_parser.add_argument("--hook")
    argument_parser.add_argument("--base", default="main")
    argument_parser.add_argument("--repository")
    argument_parser.add_argument(
        "--label",
        action="append",
        default=[],
    )
    argument_parser.add_argument(
        "--reviewer",
        action="append",
        default=[],
    )
    argument_parser.add_argument(
        "--state",
    )
    argument_parser.add_argument("--verification")
    argument_parser.add_argument("--restore", action="store_true")
    # Operator-only: directory for raw API request/response
    # recordings. Never set in CI; the recordings feed the
    # committed fixtures under tests/fixtures/github/.
    argument_parser.add_argument("--record")
    argument_parser.add_argument(
        "--title",
        default="Apply automated formatting",
    )
    argument_parser.add_argument(
        "--body",
        default=(
            "This pull request contains only the delta produced by "
            "the pinned trycopilotai/lint release."
        ),
    )
    return argument_parser


def token_free_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    isolated = dict(environment)
    for name in TOKEN_NAMES + ACTION_COMMAND_NAMES:
        isolated.pop(name, None)
    return isolated


def dependency_git_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    isolated = token_free_environment(environment)
    for name in list(isolated):
        if name.startswith("GIT_"):
            isolated.pop(name)
    isolated["GIT_CONFIG_GLOBAL"] = os.devnull
    isolated["GIT_CONFIG_NOSYSTEM"] = "1"
    isolated["GIT_NO_REPLACE_OBJECTS"] = "1"
    return isolated


def consumer_git_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    """Remove Git routing and executable configuration from consumer work."""

    return dependency_git_environment(environment)


def trusted_git() -> Path:
    """Resolve Git from fixed host locations without consulting PATH."""

    candidates = [
        Path("/usr/bin/git"),
        Path("/usr/local/bin/git"),
        Path("C:/Program Files/Git/cmd/git.exe"),
        Path("C:/Program Files/Git/bin/git.exe"),
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise CommandError("Git is required from a trusted host location")


def trusted_gh() -> Path:
    """Resolve GitHub CLI from fixed host locations without consulting PATH."""

    candidates = [
        Path("/usr/bin/gh"),
        Path("/usr/local/bin/gh"),
        Path("/opt/homebrew/bin/gh"),
        Path("C:/Program Files/GitHub CLI/gh.exe"),
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise CommandError("GitHub CLI is required from a trusted host location")


def trusted_ssh_keygen() -> Path:
    """Resolve the host SSH verifier without consulting Git config."""

    candidates = [
        Path("/usr/bin/ssh-keygen"),
        Path("/bin/ssh-keygen"),
        Path("C:/Windows/System32/OpenSSH/ssh-keygen.exe"),
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise DependencyError("ssh-keygen is required to verify the lint release")


def refuse_pull_request_target(
    environment: Mapping[str, str],
) -> None:
    """Refuse a caller that can execute unreviewed base-repository code."""

    if environment.get("GITHUB_EVENT_NAME") == "pull_request_target":
        raise SafetyError("pull_request_target callers are not allowed")


def branch_name(base: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if slug == "":
        raise SafetyError("base branch does not produce a safe slug")
    return f"auto-lint/{slug}"


def validate_selection(arguments: argparse.Namespace) -> None:
    selected = 0
    if arguments.all:
        selected += 1
    if arguments.modified:
        selected += 1
    if arguments.files_from0 is not None:
        selected += 1
    if arguments.paths:
        selected += 1
    if selected > 1:
        raise SafetyError("selection modes are mutually exclusive")


def lint_command(
    arguments: argparse.Namespace,
    image_manifest: Path | None = None,
    runtime_root: Path | None = None,
) -> list[str]:
    validate_selection(arguments)
    if arguments.lint_root is None:
        raise SafetyError("prepare requires --lint-root")
    lint_root = Path(arguments.lint_root).resolve()
    if runtime_root is not None:
        lint_root = runtime_root.resolve()
    cwd = "."
    if arguments.cwd is not None:
        cwd = arguments.cwd
    command = [
        sys.executable,
        str(lint_root / "lint.py"),
    ]
    command.extend(
        [
            "--write",
            "--cwd",
            str(Path(cwd).resolve()),
        ]
    )
    if arguments.docker:
        if image_manifest is None:
            raise SafetyError("Docker lint requires a verified image manifest")
        command.append("--docker")
        command.extend(["--image-manifest", str(image_manifest)])
    for language in arguments.language:
        command.extend(["--language", language])
    if arguments.modified:
        command.append("--modified")
    elif arguments.files_from0 is not None:
        command.extend(["--files-from0", arguments.files_from0])
    elif arguments.paths:
        command.extend(arguments.paths)
    else:
        command.append("--all")
    return command


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DependencyError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
        )
    except (OSError, json.JSONDecodeError) as error:
        raise DependencyError(f"could not load {path}") from error
    if not isinstance(value, dict):
        raise DependencyError(f"{path} must contain a JSON object")
    return value


def require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    description: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise DependencyError(
            f"{description} fields differ: missing={missing} extra={extra}"
        )


def require_git_object(value: Any, description: str) -> str:
    if not isinstance(value, str):
        raise DependencyError(f"{description} must be a string")
    if GIT_OBJECT_PATTERN.fullmatch(value) is None:
        raise DependencyError(f"{description} is invalid")
    return value


def require_sha256(value: Any, description: str) -> str:
    if not isinstance(value, str):
        raise DependencyError(f"{description} must be a string")
    if SHA256_PATTERN.fullmatch(value) is None:
        raise DependencyError(f"{description} is invalid")
    return value


def lint_language_ids(lint_root: Path) -> tuple[set[str], dict[str, str]]:
    value = load_json(lint_root / "languages.json")
    require_exact_keys(
        value,
        {"languages", "limits", "policy", "tools"},
        "lint language manifest",
    )
    languages = value.get("languages")
    tools = value.get("tools")
    if not isinstance(languages, list):
        raise DependencyError("lint language manifest languages must be a list")
    if not isinstance(tools, dict):
        raise DependencyError("lint language manifest tools must be an object")
    language_ids: set[str] = set()
    for record in languages:
        if not isinstance(record, dict):
            raise DependencyError("lint language records must be objects")
        language = record.get("id")
        if not isinstance(language, str) or language == "":
            raise DependencyError("lint language id is invalid")
        if language in language_ids:
            raise DependencyError(f"lint language id is repeated: {language}")
        language_ids.add(language)
    if language_ids != LINT_LANGUAGE_IDS:
        missing = sorted(LINT_LANGUAGE_IDS - language_ids)
        extra = sorted(language_ids - LINT_LANGUAGE_IDS)
        raise DependencyError(
            f"lint language ids differ: missing={missing} extra={extra}"
        )
    pinned_tools: dict[str, str] = {}
    for tool, version in tools.items():
        if not isinstance(tool, str) or not isinstance(version, str):
            raise DependencyError("lint tool versions must be strings")
        pinned_tools[tool] = version
    return language_ids, pinned_tools


def validate_image_digests(
    value: Any,
    language_ids: set[str],
) -> dict[str, str]:
    if not isinstance(value, dict):
        raise DependencyError("lint release image digests must be an object")
    expected = {f"{IMAGE_PREFIX}{language}" for language in language_ids}
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise DependencyError(
            "lint release image coverage differs: " f"missing={missing} extra={extra}"
        )
    digests: dict[str, str] = {}
    for image, digest in value.items():
        if not isinstance(image, str) or not isinstance(digest, str):
            raise DependencyError("lint release image records must be strings")
        if IMAGE_DIGEST_PATTERN.fullmatch(digest) is None:
            raise DependencyError(f"lint release image digest is invalid: {image}")
        digests[image] = digest
    return digests


def selected_image_digests(
    digests: Mapping[str, str],
    languages: Sequence[str],
) -> dict[str, str]:
    if not languages:
        return dict(digests)
    selected: dict[str, str] = {}
    for language in languages:
        image = f"{IMAGE_PREFIX}{language}"
        digest = digests.get(image)
        if digest is None:
            raise DependencyError(f"selected lint image is not in the release: {image}")
        selected[image] = digest
    return selected


def validate_release_manifest(
    manifest: dict[str, Any],
    lint_root: Path,
    dependency: dict[str, Any],
) -> dict[str, str]:
    schema = manifest.get("schema_version")
    if schema != 1:
        raise DependencyError("unsupported lint release manifest schema")
    require_exact_keys(
        manifest,
        {"images", "release", "schema_version", "source", "tools"},
        "lint release manifest",
    )

    release = manifest.get("release")
    ref = dependency["ref"]
    if not isinstance(release, str) or release == "":
        raise DependencyError("lint release version is invalid")
    if ref != f"refs/tags/v{release}":
        raise DependencyError("lint dependency ref does not match the release")

    source = manifest.get("source")
    if not isinstance(source, dict):
        raise DependencyError("lint release manifest is missing source")
    require_exact_keys(
        source,
        {"archive", "commit", "sha256"},
        "lint release source",
    )
    if source.get("commit") != dependency["commit"]:
        raise DependencyError("lint release source commit does not match")
    archive = source.get("archive")
    if archive != f"lint-{release}.tar.gz":
        raise DependencyError("lint release source archive name does not match")
    require_sha256(source.get("sha256"), "lint release archive checksum")

    language_ids, tools = lint_language_ids(lint_root)
    if manifest.get("tools") != tools:
        raise DependencyError("lint release tools do not match languages.json")
    images = manifest.get("images")
    return validate_image_digests(images, language_ids)


def validate_lint_dependency(
    dependency: dict[str, Any],
    dependency_path: Path,
    manifest_path: Path,
    allowed_signers_path: Path,
) -> None:
    require_exact_keys(
        dependency,
        {
            "allowed_signers_sha256",
            "commit",
            "manifest",
            "manifest_sha256",
            "ref",
            "repository",
            "schema",
            "signer",
            "tag_object",
            "tree",
        },
        "lint dependency ledger",
    )
    if dependency.get("schema") != 2:
        raise DependencyError("unsupported lint dependency schema")
    if dependency.get("repository") != "https://github.com/trycopilotai/lint":
        raise DependencyError("lint dependency repository is invalid")
    ref = dependency.get("ref")
    if (
        not isinstance(ref, str)
        or re.fullmatch(r"refs/tags/v[0-9]+\.[0-9]+\.[0-9]+", ref) is None
    ):
        raise DependencyError("lint dependency ref is invalid")
    for name in ("commit", "tag_object", "tree"):
        require_git_object(dependency.get(name), f"lint dependency {name}")
    signer = dependency.get("signer")
    if signer != "trycopilotai-release":
        raise DependencyError("lint dependency signer is invalid")
    manifest_name = dependency.get("manifest")
    if manifest_name != manifest_path.name:
        raise DependencyError("lint dependency manifest path does not match")
    expected_manifest = dependency_path.parent / str(manifest_name)
    if manifest_path != expected_manifest:
        raise DependencyError("lint dependency manifest location is invalid")
    expected_manifest_sha = require_sha256(
        dependency.get("manifest_sha256"),
        "lint dependency manifest checksum",
    )
    if sha256(manifest_path) != expected_manifest_sha:
        raise DependencyError("lint dependency manifest checksum does not match")
    expected_signers_sha = require_sha256(
        dependency.get("allowed_signers_sha256"),
        "lint dependency allowed-signers checksum",
    )
    if allowed_signers_path.is_symlink() or not allowed_signers_path.is_file():
        raise DependencyError("lint allowed-signers input must be a regular file")
    if sha256(allowed_signers_path) != expected_signers_sha:
        raise DependencyError("lint allowed-signers checksum does not match")
    allowed_lines = allowed_signers_path.read_text(encoding="utf-8").splitlines()
    if len(allowed_lines) != 1:
        raise DependencyError("lint allowed-signers input must contain one signer")
    if not allowed_lines[0].startswith(f"{signer} ssh-ed25519 "):
        raise DependencyError("lint allowed-signers principal does not match")


def git_object_type(
    repository: Path,
    object_name: str,
    environment: Mapping[str, str],
) -> str:
    completed = git(
        repository,
        "cat-file",
        "-t",
        object_name,
        check=False,
        environment=environment,
    )
    if completed.returncode != 0:
        raise DependencyError(f"lint release object does not exist: {object_name}")
    return completed.stdout.strip()


def deterministic_archive_sha256(
    lint_root: Path,
    ref: str,
    release: str,
    environment: Mapping[str, str],
) -> str:
    archive = git_archive_payload(
        lint_root,
        ref,
        environment,
        prefix=f"lint-{release}/",
    )
    destination = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=destination, mode="wb", mtime=0) as handle:
        handle.write(archive)
    return hashlib.sha256(destination.getvalue()).hexdigest()


def git_archive_payload(
    lint_root: Path,
    object_name: str,
    environment: Mapping[str, str],
    prefix: str | None = None,
) -> bytes:
    """Return the exact Git archive bytes for one immutable object."""

    command = [str(trusted_git()), "archive", "--format=tar"]
    if prefix is not None:
        command.append(f"--prefix={prefix}")
    command.append(object_name)
    completed = subprocess.run(
        command,
        cwd=lint_root,
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DependencyError(f"could not reproduce lint source archive: {detail}")
    return completed.stdout


def archive_relative_path(value: str) -> PurePosixPath:
    """Validate one path emitted by Git's tar writer."""

    if value == "" or "\0" in value or "\\" in value:
        raise DependencyError("lint archive contains a non-canonical path")
    relative = PurePosixPath(value)
    if relative.is_absolute():
        raise DependencyError("lint archive contains an absolute path")
    if relative.as_posix() != value.rstrip("/"):
        raise DependencyError("lint archive contains a non-canonical path")
    for component in relative.parts:
        if component in {"", ".", ".."}:
            raise DependencyError("lint archive path escapes its root")
    return relative


def resolved_link_path(
    parent: PurePosixPath,
    value: str,
) -> PurePosixPath:
    """Resolve a relative archive link without allowing root escape."""

    if value == "" or "\0" in value or "\\" in value:
        raise DependencyError("lint archive link is not canonical")
    link = PurePosixPath(value)
    if link.is_absolute():
        raise DependencyError("lint archive link is absolute")
    parts = list(parent.parts)
    for component in link.parts:
        if component in {"", "."}:
            continue
        if component == "..":
            if not parts:
                raise DependencyError("lint archive link escapes its root")
            parts.pop()
            continue
        parts.append(component)
    if not parts:
        raise DependencyError("lint archive link targets its root")
    return PurePosixPath(*parts)


def materialize_git_tree(
    lint_root: Path,
    object_name: str,
    destination: Path,
    environment: Mapping[str, str],
) -> None:
    """Materialize signed Git bytes without checkout filters or local state."""

    archive = git_archive_payload(lint_root, object_name, environment)
    members: dict[PurePosixPath, tarfile.TarInfo] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        for member in handle.getmembers():
            relative = archive_relative_path(member.name)
            if relative in members:
                raise DependencyError("lint archive contains a duplicate path")
            if not member.isdir() and not member.isfile() and not member.issym():
                raise DependencyError("lint archive contains an unsupported entry")
            members[relative] = member

        destination.mkdir(mode=0o755)
        directories = [path for path, member in members.items() if member.isdir()]
        for relative in sorted(directories, key=lambda path: len(path.parts)):
            target = destination.joinpath(*relative.parts)
            target.mkdir(mode=0o755, parents=True, exist_ok=True)

        files = [path for path, member in members.items() if member.isfile()]
        for relative in sorted(files, key=lambda path: path.as_posix()):
            member = members[relative]
            source = handle.extractfile(member)
            if source is None:
                raise DependencyError("lint archive file has no payload")
            payload = source.read()
            if len(payload) != member.size:
                raise DependencyError("lint archive file is truncated")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(payload)
            mode = 0o644
            if member.mode & 0o111:
                mode = 0o755
            os.chmod(target, mode)

        links = [path for path, member in members.items() if member.issym()]
        for relative in sorted(links, key=lambda path: len(path.parts)):
            member = members[relative]
            resolved = resolved_link_path(relative.parent, member.linkname)
            if resolved not in members:
                raise DependencyError("lint archive link target is missing")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if os.name == "nt":
                target.write_text(member.linkname, encoding="utf-8", newline="")
            else:
                os.symlink(member.linkname, target)


def executable_git_config_names(
    repository: Path,
    environment: Mapping[str, str],
) -> list[str]:
    """Return repository-local settings that can launch helper programs."""

    pattern = (
        r"^(core\.fsmonitor|gpg\..*program|"
        r"filter\..*\.(clean|smudge|process)|diff\.external|"
        r"diff\..*\.command|merge\..*\.driver)$"
    )
    completed = subprocess.run(
        [
            str(trusted_git()),
            "config",
            "--local",
            "--includes",
            "--name-only",
            "--null",
            "--get-regexp",
            pattern,
        ],
        cwd=repository,
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode == 1 and completed.stdout == b"":
        return []
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DependencyError(f"could not inspect lint Git configuration: {detail}")
    return sorted(
        value.decode("utf-8", errors="replace")
        for value in completed.stdout.split(b"\0")
        if value != b""
    )


def reject_executable_git_config(
    repository: Path,
    environment: Mapping[str, str],
) -> None:
    """Reject executable configuration in the lint dependency checkout."""

    names = executable_git_config_names(repository, environment)
    if not names:
        return
    raise DependencyError(
        "lint checkout has executable repository Git configuration: " + ", ".join(names)
    )


def reject_consumer_executable_git_config(
    repository: Path,
    environment: Mapping[str, str],
) -> None:
    """Reject executable configuration in a consumer checkout."""

    names = executable_git_config_names(repository, environment)
    if not names:
        return
    raise SafetyError(
        "consumer checkout has executable repository Git configuration: "
        + ", ".join(names)
    )


def git_tree_entries(
    repository: Path,
    commit: str,
    environment: Mapping[str, str],
) -> dict[str, tuple[str, str, str]]:
    """Return the exact recursive tree inventory for one commit."""

    completed = subprocess.run(
        [str(trusted_git()), "ls-tree", "-rz", "--full-tree", commit],
        cwd=repository,
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DependencyError(f"could not inventory the lint tree: {detail}")
    entries: dict[str, tuple[str, str, str]] = {}
    for raw in completed.stdout.split(b"\0"):
        if raw == b"":
            continue
        metadata, separator, raw_path = raw.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 3:
            raise DependencyError("lint tree inventory is malformed")
        path = os.fsdecode(raw_path)
        archive_relative_path(path)
        if path in entries:
            raise DependencyError("lint tree inventory repeats a path")
        entries[path] = (
            fields[0].decode("ascii"),
            fields[1].decode("ascii"),
            fields[2].decode("ascii"),
        )
    return entries


def git_blob_payload(
    repository: Path,
    object_name: str,
    environment: Mapping[str, str],
) -> bytes:
    """Read one exact blob without applying worktree filters."""

    completed = subprocess.run(
        [str(trusted_git()), "cat-file", "blob", object_name],
        cwd=repository,
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DependencyError(f"could not read a lint tree blob: {detail}")
    return completed.stdout


def worktree_paths(repository: Path) -> set[str]:
    """Inventory files and links without following worktree symlinks."""

    paths: set[str] = set()
    for root, directories, files in os.walk(
        repository, topdown=True, followlinks=False
    ):
        current = Path(root)
        if current == repository:
            directories[:] = [name for name in directories if name != ".git"]
        for name in list(directories):
            candidate = current / name
            if candidate.is_symlink():
                relative = candidate.relative_to(repository).as_posix()
                paths.add(relative)
                directories.remove(name)
        for name in files:
            if current == repository and name == ".git":
                continue
            candidate = current / name
            relative = candidate.relative_to(repository).as_posix()
            paths.add(relative)
    return paths


def verify_worktree_matches_commit(
    repository: Path,
    commit: str,
    environment: Mapping[str, str],
) -> None:
    """Compare actual bytes to Git objects without trusting index flags."""

    entries = git_tree_entries(repository, commit, environment)
    expected_paths = set(entries)
    actual_paths = worktree_paths(repository)
    if actual_paths != expected_paths:
        raise DependencyError("lint checkout contains residue or missing paths")
    for relative, (mode, object_type, object_name) in entries.items():
        if object_type != "blob":
            raise DependencyError("lint checkout contains an unsupported Git object")
        path = repository / relative
        expected = git_blob_payload(repository, object_name, environment)
        if mode == "120000":
            if path.is_symlink():
                actual = os.fsencode(os.readlink(path))
            elif os.name == "nt" and path.is_file():
                actual = path.read_bytes()
            else:
                raise DependencyError("lint checkout symbolic link has the wrong type")
        else:
            if path.is_symlink() or not path.is_file():
                raise DependencyError("lint checkout file has the wrong type")
            actual = path.read_bytes()
            if os.name != "nt":
                executable = bool(path.stat().st_mode & stat.S_IXUSR)
                expected_executable = mode == "100755"
                if executable != expected_executable:
                    raise DependencyError("lint checkout file mode does not match")
        if actual != expected:
            raise DependencyError("lint checkout bytes do not match the signed commit")


def git(
    repository: Path,
    *arguments: str,
    check: bool = True,
    environment: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(trusted_git()), *arguments],
        cwd=repository,
        check=False,
        env=environment,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip()
        if detail == "":
            detail = completed.stdout.strip()
        raise CommandError(f"git {' '.join(arguments)} failed: {detail}")
    return completed


def verify_lint_release(
    lint_root: Path,
    manifest_path: Path,
    dependency_path: Path,
    allowed_signers_path: Path,
    verified_root: Path | None = None,
) -> dict[str, Any]:
    if dependency_path.is_symlink() or not dependency_path.is_file():
        raise DependencyError("lint dependency ledger must be a regular file")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DependencyError("lint release manifest must be a regular file")
    if allowed_signers_path.is_symlink() or not allowed_signers_path.is_file():
        raise DependencyError("lint allowed-signers input must be a regular file")
    lint_root = lint_root.resolve()
    manifest_path = manifest_path.resolve()
    dependency_path = dependency_path.resolve()
    allowed_signers_path = allowed_signers_path.resolve()
    if allowed_signers_path.is_relative_to(lint_root):
        raise DependencyError("lint allowed-signers input must be controller-bound")
    dependency = load_json(dependency_path)
    validate_lint_dependency(
        dependency,
        dependency_path,
        manifest_path,
        allowed_signers_path,
    )
    manifest = load_json(manifest_path)
    isolated = dependency_git_environment(os.environ)
    reject_executable_git_config(lint_root, isolated)
    actual_commit = git(
        lint_root,
        "rev-parse",
        "HEAD",
        environment=isolated,
    ).stdout.strip()
    if actual_commit != dependency["commit"]:
        raise DependencyError(
            f"lint checkout is {actual_commit}; expected {dependency['commit']}"
        )
    actual_tree = git(
        lint_root,
        "rev-parse",
        "HEAD^{tree}",
        environment=isolated,
    ).stdout.strip()
    if actual_tree != dependency["tree"]:
        raise DependencyError("lint checkout tree does not match")
    ref = dependency["ref"]
    tag_object = dependency["tag_object"]
    if git_object_type(lint_root, tag_object, isolated) != "tag":
        raise DependencyError("lint release ref is not an annotated tag")
    actual_tag_object = git(
        lint_root,
        "rev-parse",
        f"{ref}^{{tag}}",
        check=False,
        environment=isolated,
    )
    if actual_tag_object.returncode != 0:
        raise DependencyError("lint release ref does not resolve to a tag")
    actual_tag_object_value = actual_tag_object.stdout.strip()
    if actual_tag_object_value != dependency["tag_object"]:
        raise DependencyError("lint release tag object does not match")
    peeled_commit = git(
        lint_root,
        "rev-parse",
        f"{tag_object}^{{commit}}",
        environment=isolated,
    ).stdout.strip()
    if peeled_commit != dependency["commit"]:
        raise DependencyError("lint release tag does not point at the commit")
    verification = subprocess.run(
        [
            str(trusted_git()),
            "-c",
            "gpg.format=ssh",
            "-c",
            f"gpg.ssh.program={trusted_ssh_keygen()}",
            "-c",
            f"gpg.ssh.allowedSignersFile={allowed_signers_path}",
            "verify-tag",
            tag_object,
        ],
        cwd=lint_root,
        check=False,
        env=isolated,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if verification.returncode != 0:
        raise DependencyError("lint release tag signature is not trusted")
    signature_output = verification.stdout + verification.stderr
    if dependency["signer"] not in signature_output:
        raise DependencyError("lint release signature principal does not match")
    verify_worktree_matches_commit(lint_root, dependency["commit"], isolated)
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if verified_root is None:
        temporary_directory = tempfile.TemporaryDirectory(prefix="auto-lint-pr-verify-")
        verified_root = Path(temporary_directory.name) / "lint"
    try:
        materialize_git_tree(
            lint_root,
            dependency["commit"],
            verified_root,
            isolated,
        )
        image_digests = validate_release_manifest(
            manifest,
            verified_root,
            dependency,
        )
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()
    release = manifest["release"]
    archive_sha = deterministic_archive_sha256(
        lint_root,
        dependency["commit"],
        release,
        isolated,
    )
    if archive_sha != manifest["source"]["sha256"]:
        raise DependencyError("lint release archive checksum does not reproduce")
    manifest["verified_dependency"] = {
        "commit": dependency["commit"],
        "dependency_sha256": sha256(dependency_path),
        "images": image_digests,
        "manifest_sha256": sha256(manifest_path),
        "tag_object": dependency["tag_object"],
        "tree": dependency["tree"],
    }
    return manifest


def repository_root(
    cwd: Path,
    workspace_root: Path | None = None,
) -> Path:
    isolated = consumer_git_environment(os.environ)
    completed = git(
        cwd,
        "rev-parse",
        "--show-toplevel",
        environment=isolated,
    )
    repository = Path(completed.stdout.strip()).resolve()
    if workspace_root is not None:
        boundary = workspace_root.resolve()
        if not cwd.resolve().is_relative_to(boundary):
            raise SafetyError("working directory is outside the trusted workspace")
        if not repository.is_relative_to(boundary):
            raise SafetyError("repository root is outside the trusted workspace")
    reject_consumer_executable_git_config(repository, isolated)
    return repository


def transaction_state_path(
    arguments: argparse.Namespace,
    repository: Path | None = None,
) -> Path:
    """Resolve the transaction state beneath the selected checkout by default."""

    if arguments.state is not None:
        return Path(arguments.state).resolve()
    if repository is None:
        cwd_value = "."
        if arguments.cwd is not None:
            cwd_value = arguments.cwd
        workspace_root = None
        if arguments.workspace_root is not None:
            workspace_root = Path(arguments.workspace_root)
        repository = repository_root(Path(cwd_value).resolve(), workspace_root)
    isolated = consumer_git_environment(os.environ)
    completed = git(
        repository,
        "rev-parse",
        "--git-path",
        "auto-lint-pr-state.json",
        environment=isolated,
    )
    path = Path(completed.stdout.strip())
    if not path.is_absolute():
        path = repository / path
    return path.resolve()


def changed_paths(repository: Path) -> list[str]:
    isolated = consumer_git_environment(os.environ)
    reject_consumer_executable_git_config(repository, isolated)
    tracked = git(
        repository,
        "diff",
        "--no-renames",
        "--name-only",
        "-z",
        "HEAD",
        environment=isolated,
    ).stdout
    untracked = git(
        repository,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        environment=isolated,
    ).stdout
    paths = set()
    for payload in (tracked, untracked):
        for value in payload.split("\0"):
            if value != "":
                paths.add(value)
    return sorted(paths)


def file_record(repository: Path, relative: str) -> dict[str, str]:
    path = repository / relative
    record = {"path": relative}
    if path.is_symlink():
        target = os.fsencode(os.readlink(path))
        record["content"] = base64.b64encode(target).decode("ascii")
        record["kind"] = "symlink"
        record["mode"] = "120000"
        record["sha256"] = hashlib.sha256(target).hexdigest()
        return record
    if path.is_file():
        payload = path.read_bytes()
        mode = "100644"
        if path.stat().st_mode & 0o111:
            mode = "100755"
        record["content"] = base64.b64encode(payload).decode("ascii")
        record["kind"] = "file"
        record["mode"] = mode
        record["sha256"] = hashlib.sha256(payload).hexdigest()
        return record
    if not path.exists():
        isolated = consumer_git_environment(os.environ)
        entry = git(
            repository,
            "ls-tree",
            "-z",
            "HEAD",
            "--",
            relative,
            environment=isolated,
        ).stdout
        metadata, separator, listed_path = entry.partition("\t")
        fields = metadata.split()
        if separator != "\t" or listed_path.rstrip("\0") != relative:
            raise SafetyError(f"deleted path is absent from HEAD: {relative}")
        if len(fields) != 3:
            raise SafetyError(f"deleted path metadata is invalid: {relative}")
        record["content"] = ""
        record["kind"] = "deleted"
        record["mode"] = fields[0]
        record["sha256"] = ""
        return record
    raise SafetyError(f"changed path is not a file: {relative}")


def delta_records(repository: Path) -> list[dict[str, str]]:
    return [file_record(repository, relative) for relative in changed_paths(repository)]


def validate_prepared_modes(records: list[dict[str, str]]) -> None:
    """Restrict every changed path to a representable regular blob."""

    for record in records:
        if record["kind"] not in {"deleted", "file"}:
            path = record["path"]
            raise SafetyError(f"prepared path has an unsupported kind: {path}")
        if record["mode"] != "100644":
            path = record["path"]
            raise SafetyError(f"prepared path has an unsupported mode: {path}")


def publication_file_changes(
    records: list[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    """Return immutable GraphQL changes from recorded prepare bytes."""

    validate_prepared_modes(records)
    additions = []
    deletions = []
    for record in records:
        path = record["path"]
        if record["kind"] == "deleted":
            deletions.append({"path": path})
            continue
        prepared_payload(record)
        additions.append(
            {
                "contents": record["content"],
                "path": path,
            }
        )
    return {
        "additions": additions,
        "deletions": deletions,
    }


def prepared_payload(record: dict[str, str]) -> bytes:
    """Decode and authenticate one prepared file payload."""

    path = record.get("path", "<unknown>")
    try:
        payload = base64.b64decode(record["content"], validate=True)
        expected = record["sha256"]
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise SafetyError(f"prepared content is invalid: {path}") from error
    if hashlib.sha256(payload).hexdigest() != expected:
        raise SafetyError(f"prepared content digest changed: {path}")
    return payload


def canonical_sha256(value: Any) -> str:
    """Hash one value using the transaction's canonical JSON codec."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def prepared_relative_path(value: Any) -> PurePosixPath:
    """Validate one canonical Git worktree path from a state artifact."""

    if not isinstance(value, str) or value == "":
        raise SafetyError("prepared path must be a non-empty string")
    if "\0" in value or "\\" in value:
        raise SafetyError(f"prepared path is not canonical: {value!r}")
    relative = PurePosixPath(value)
    if relative.is_absolute() or relative.as_posix() != value:
        raise SafetyError(f"prepared path is not canonical: {value!r}")
    for component in relative.parts:
        if component in {"", ".", ".."}:
            raise SafetyError(f"prepared path escapes the repository: {value!r}")
        if component.casefold() == ".git":
            raise SafetyError(f"prepared path targets Git metadata: {value!r}")
        windows_stem = component.split(".", maxsplit=1)[0].casefold()
        windows_reserved = {"aux", "con", "nul", "prn"}
        windows_reserved.update({f"com{index}" for index in range(1, 10)})
        windows_reserved.update({f"lpt{index}" for index in range(1, 10)})
        if ":" in component or component.endswith((" ", ".")):
            raise SafetyError(f"prepared path is unsafe on Windows: {value!r}")
        if windows_stem in windows_reserved:
            raise SafetyError(f"prepared path is unsafe on Windows: {value!r}")
    return relative


def secure_dir_fd_restoration_available() -> bool:
    """Return whether descriptor-relative restoration is fully supported."""

    required = (os.open, os.mkdir, os.stat, os.unlink)
    for operation in required:
        if operation not in os.supports_dir_fd:
            return False
    if getattr(os, "O_DIRECTORY", 0) == 0:
        return False
    if getattr(os, "O_NOFOLLOW", 0) == 0:
        return False
    return True


def open_prepared_parent(
    repository: Path,
    relative: PurePosixPath,
) -> tuple[int, str]:
    """Pin a prepared path's parent without following symbolic links."""

    if not secure_dir_fd_restoration_available():
        raise SafetyError("secure restoration requires directory file descriptors")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    current = os.open(repository.resolve(), flags)
    try:
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(component, mode=0o755, dir_fd=current)
                child = os.open(component, flags, dir_fd=current)
            except OSError as error:
                raise SafetyError(
                    f"prepared path has an unsafe parent: {relative.as_posix()!r}"
                ) from error
            os.close(current)
            current = child
        return current, relative.parts[-1]
    except BaseException:
        os.close(current)
        raise


def prepared_target_stat(parent: int, name: str) -> os.stat_result | None:
    """Inspect a destination without following its final symbolic link."""

    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def write_prepared_file(parent: int, name: str, payload: bytes) -> None:
    """Atomically replace one file relative to a pinned directory."""

    temporary_name = ".auto-lint-pr-" + secrets.token_hex(16)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            mode=0o600,
            dir_fd=parent,
        )
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise SafetyError("could not restore the complete prepared payload")
            remaining = remaining[written:]
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        temporary_name = ""
        os.fsync(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name != "":
            try:
                os.unlink(temporary_name, dir_fd=parent)
            except FileNotFoundError:
                pass


def prepared_metadata_is_reparse(metadata: os.stat_result) -> bool:
    """Reject links and Windows reparse points without following them."""

    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def prepared_path_metadata(path: Path) -> os.stat_result | None:
    """Inspect a portable destination without following its final entry."""

    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def prepared_metadata_identity(metadata: os.stat_result) -> tuple[int, int]:
    """Return fields that change when a guarded path is substituted."""

    return (metadata.st_dev, metadata.st_ino)


def open_windows_directory_guard(path: Path) -> tuple[Any, int] | None:
    """Pin a Windows directory without allowing rename or deletion."""

    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    file_read_attributes = 0x0080
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    handle = create_file(
        str(path),
        file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise SafetyError(f"could not guard prepared directory: {path}") from OSError(
            error,
            os.strerror(error),
            str(path),
        )
    return kernel32, handle


def close_windows_directory_guard(guard: tuple[Any, int] | None) -> None:
    """Release one Windows directory guard."""

    if guard is None:
        return
    kernel32, handle = guard
    if not kernel32.CloseHandle(handle):
        error = kernel32.GetLastError()
        raise SafetyError("could not release prepared directory guard") from OSError(
            error,
            os.strerror(error),
        )


def require_prepared_directory(path: Path) -> os.stat_result:
    """Require a real portable directory rather than a link or reparse point."""

    metadata = prepared_path_metadata(path)
    if metadata is None:
        raise SafetyError(f"prepared directory disappeared: {path}")
    if prepared_metadata_is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise SafetyError(f"prepared path has an unsafe parent: {path}")
    return metadata


def acquire_prepared_directory_guard(path: Path) -> dict[str, Any]:
    """Validate and pin one directory used by portable restoration."""

    before = require_prepared_directory(path)
    windows_guard = open_windows_directory_guard(path)
    try:
        after = require_prepared_directory(path)
        if prepared_metadata_identity(before) != prepared_metadata_identity(after):
            raise SafetyError(f"prepared directory was substituted: {path}")
        return {
            "identity": prepared_metadata_identity(after),
            "path": path,
            "windows_guard": windows_guard,
        }
    except BaseException:
        close_windows_directory_guard(windows_guard)
        raise


def validate_prepared_directory_guards(guards: dict[str, dict[str, Any]]) -> None:
    """Fail when any portable parent no longer names its pinned directory."""

    for guard in guards.values():
        path = guard["path"]
        metadata = require_prepared_directory(path)
        if prepared_metadata_identity(metadata) != guard["identity"]:
            raise SafetyError(f"prepared directory was substituted: {path}")


def close_prepared_directory_guards(guards: dict[str, dict[str, Any]]) -> None:
    """Release portable restoration guards in deepest-first order."""

    ordered = sorted(
        guards.values(),
        key=lambda guard: len(guard["path"].parts),
        reverse=True,
    )
    first_error: SafetyError | None = None
    for guard in ordered:
        try:
            close_windows_directory_guard(guard["windows_guard"])
        except SafetyError as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def portable_prepared_parent(
    repository: Path,
    relative: PurePosixPath,
    guards: dict[str, dict[str, Any]],
    created: list[Path],
) -> Path:
    """Create and guard a prepared parent on platforms without dir_fd."""

    current = Path(os.path.abspath(repository))
    for component in (None, *relative.parts[:-1]):
        if component is not None:
            current = current / component
            if prepared_path_metadata(current) is None:
                try:
                    os.mkdir(current, mode=0o755)
                    created.append(current)
                except FileExistsError:
                    pass
        key = os.path.normcase(str(current))
        if key not in guards:
            guards[key] = acquire_prepared_directory_guard(current)
        else:
            validate_prepared_directory_guards({key: guards[key]})
    return current


def portable_target_state(path: Path) -> tuple[os.stat_result | None, bytes | None]:
    """Capture a portable target for preflight and transactional rollback."""

    metadata = prepared_path_metadata(path)
    if metadata is None:
        return None, None
    if prepared_metadata_is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise SafetyError(f"prepared destination is not a regular file: {path}")
    payload = path.read_bytes()
    after = prepared_path_metadata(path)
    if after is None:
        raise SafetyError(f"prepared destination was substituted: {path}")
    if prepared_metadata_identity(metadata) != prepared_metadata_identity(after):
        raise SafetyError(f"prepared destination was substituted: {path}")
    return after, payload


def portable_write_prepared_file(path: Path, payload: bytes) -> None:
    """Atomically replace a portable target with authenticated bytes."""

    temporary = path.parent / (".auto-lint-pr-" + secrets.token_hex(16))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise SafetyError("could not restore the complete prepared payload")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        temporary = Path()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary != Path():
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def validate_portable_target(
    path: Path,
    expected: os.stat_result | None,
) -> None:
    """Require that a portable target still matches its preflight state."""

    actual = prepared_path_metadata(path)
    if expected is None:
        if actual is not None:
            raise SafetyError(f"prepared destination appeared: {path}")
        return
    if actual is None or prepared_metadata_is_reparse(actual):
        raise SafetyError(f"prepared destination was substituted: {path}")
    if not stat.S_ISREG(actual.st_mode):
        raise SafetyError(f"prepared destination is not a regular file: {path}")
    if prepared_metadata_identity(actual) != prepared_metadata_identity(expected):
        raise SafetyError(f"prepared destination was substituted: {path}")


def verify_portable_result(path: Path, payload: bytes | None) -> None:
    """Confirm that one portable mutation produced the intended state."""

    metadata = prepared_path_metadata(path)
    if payload is None:
        if metadata is not None:
            raise SafetyError(f"prepared deletion was not restored: {path}")
        return
    if metadata is None or prepared_metadata_is_reparse(metadata):
        raise SafetyError(f"prepared destination was substituted: {path}")
    if not stat.S_ISREG(metadata.st_mode) or path.read_bytes() != payload:
        raise SafetyError(f"prepared payload was not restored: {path}")


def rollback_portable_operations(
    operations: list[dict[str, Any]],
    guards: dict[str, dict[str, Any]],
) -> None:
    """Restore every already-started portable mutation in reverse order."""

    for operation in reversed(operations):
        validate_prepared_directory_guards(guards)
        path = operation["path"]
        metadata = prepared_path_metadata(path)
        if metadata is not None:
            if prepared_metadata_is_reparse(metadata):
                raise SafetyError(f"prepared rollback target is unsafe: {path}")
            if not stat.S_ISREG(metadata.st_mode):
                raise SafetyError(f"prepared rollback target is not a file: {path}")
        original = operation["original"]
        if original is None:
            if metadata is not None:
                os.unlink(path)
        else:
            portable_write_prepared_file(path, original)


def restore_prepared_delta_portable(
    repository: Path,
    records: list[dict[str, str]],
) -> None:
    """Restore a whole delta transactionally without directory descriptors."""

    guards: dict[str, dict[str, Any]] = {}
    created: list[Path] = []
    operations: list[dict[str, Any]] = []
    started: list[dict[str, Any]] = []
    completed = False
    try:
        seen: set[str] = set()
        for record in records:
            relative = prepared_relative_path(record.get("path"))
            path_key = relative.as_posix().casefold()
            if path_key in seen:
                raise SafetyError(f"prepared path is duplicated: {relative}")
            seen.add(path_key)
            parent = portable_prepared_parent(repository, relative, guards, created)
            target = parent / relative.parts[-1]
            metadata, original = portable_target_state(target)
            if record["kind"] == "deleted":
                if metadata is None:
                    raise SafetyError(
                        f"prepared deletion is not a regular file: {relative}"
                    )
                if metadata.st_mode & 0o111:
                    raise SafetyError(
                        f"prepared deletion changes an executable: {relative}"
                    )
                payload = None
            else:
                payload = prepared_payload(record)
            operations.append(
                {
                    "metadata": metadata,
                    "original": original,
                    "path": target,
                    "payload": payload,
                }
            )
        for operation in operations:
            validate_prepared_directory_guards(guards)
            validate_portable_target(operation["path"], operation["metadata"])
            started.append(operation)
            payload = operation["payload"]
            if payload is None:
                os.unlink(operation["path"])
            else:
                portable_write_prepared_file(operation["path"], payload)
            validate_prepared_directory_guards(guards)
            verify_portable_result(operation["path"], payload)
        completed = True
    except BaseException:
        try:
            rollback_portable_operations(started, guards)
        except BaseException as rollback_error:
            raise SafetyError(
                "portable prepared restoration rollback failed"
            ) from rollback_error
        raise
    finally:
        close_prepared_directory_guards(guards)
        if not completed:
            for path in reversed(created):
                try:
                    path.rmdir()
                except (FileNotFoundError, OSError):
                    pass


def restore_prepared_delta_dir_fd(
    repository: Path,
    records: list[dict[str, str]],
) -> None:
    """Restore records through pinned descriptor-relative parents."""

    for record in records:
        relative = prepared_relative_path(record.get("path"))
        parent, name = open_prepared_parent(repository, relative)
        try:
            target = prepared_target_stat(parent, name)
            if record["kind"] == "deleted":
                if target is None or not stat.S_ISREG(target.st_mode):
                    raise SafetyError(
                        f"prepared deletion is not a regular file: {relative}"
                    )
                if target.st_mode & 0o111:
                    raise SafetyError(
                        f"prepared deletion changes an executable: {relative}"
                    )
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
                continue
            if target is not None and not stat.S_ISREG(target.st_mode):
                raise SafetyError(
                    f"prepared destination is not a regular file: {relative}"
                )
            write_prepared_file(parent, name, prepared_payload(record))
        finally:
            os.close(parent)


def restore_prepared_delta(
    repository: Path,
    records: list[dict[str, str]],
) -> None:
    """Materialize recorded bytes into a clean checkout without credentials."""

    validate_prepared_modes(records)
    if secure_dir_fd_restoration_available():
        restore_prepared_delta_dir_fd(repository, records)
        return
    restore_prepared_delta_portable(repository, records)


def ensure_clean(repository: Path) -> None:
    if changed_paths(repository):
        raise SafetyError("repository must be clean before formatting")


def write_canonical(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    path.write_text(payload + "\n", encoding="utf-8")


def write_action_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output is None:
        return
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def run_checked(
    command: Sequence[str] | str,
    cwd: Path,
    environment: Mapping[str, str],
    shell: bool = False,
) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        env=environment,
        shell=shell,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    if completed.returncode != 0:
        raise CommandError(f"command failed with exit {completed.returncode}")


def run_prepare(arguments: argparse.Namespace) -> dict[str, Any]:
    refuse_pull_request_target(os.environ)
    isolated = consumer_git_environment(os.environ)
    cwd_value = "."
    if arguments.cwd is not None:
        cwd_value = arguments.cwd
    cwd = Path(cwd_value).resolve()
    workspace_root = None
    if arguments.workspace_root is not None:
        workspace_root = Path(arguments.workspace_root)
    repository = repository_root(cwd, workspace_root)
    ensure_clean(repository)
    base_head = git(
        repository,
        "rev-parse",
        "HEAD",
        environment=isolated,
    ).stdout.strip()
    if arguments.lint_root is None:
        raise SafetyError("prepare requires --lint-root")
    lint_root = Path(arguments.lint_root).resolve()
    manifest_path = Path(arguments.manifest).resolve()
    with tempfile.TemporaryDirectory(prefix="auto-lint-pr-runtime-") as directory:
        runtime_directory = Path(directory)
        runtime_root = runtime_directory / "lint"
        manifest = verify_lint_release(
            lint_root,
            manifest_path,
            Path(arguments.dependency),
            Path(arguments.allowed_signers),
            runtime_root,
        )
        runtime_manifest = dict(manifest)
        release_binding = manifest.pop("verified_dependency")
        runtime_manifest.pop("verified_dependency", None)
        release_binding["images"] = selected_image_digests(
            release_binding["images"],
            arguments.language,
        )
        runtime_manifest_path = runtime_directory / "lint-release-manifest.json"
        write_canonical(runtime_manifest_path, runtime_manifest)
        run_checked(
            lint_command(
                arguments,
                image_manifest=runtime_manifest_path,
                runtime_root=runtime_root,
            ),
            cwd=repository,
            environment=isolated,
        )
    if sha256(manifest_path) != release_binding["manifest_sha256"]:
        raise DependencyError("lint release manifest changed during formatting")
    if arguments.hook is not None:
        run_checked(
            arguments.hook,
            cwd=repository,
            environment=isolated,
            shell=True,
        )
    current_head = git(
        repository,
        "rev-parse",
        "HEAD",
        environment=isolated,
    ).stdout.strip()
    if current_head != base_head:
        raise SafetyError("base checkout changed during token-free preparation")
    records = delta_records(repository)
    validate_prepared_modes(records)
    state = {
        "base": arguments.base,
        "base_head": base_head,
        "branch": branch_name(arguments.base),
        "changed": bool(records),
        "cwd": str(repository),
        "delta": records,
        "lint_commit": manifest["source"]["commit"],
        "lint_release": release_binding,
        "repository": arguments.repository,
        "schema": 2,
    }
    write_canonical(transaction_state_path(arguments, repository), state)
    write_action_output("changed", str(bool(records)).lower())
    write_action_output("base-head", base_head)
    write_action_output("branch", state["branch"])
    return state


def transaction_state(path: Path) -> dict[str, Any]:
    """Load and validate one untrusted cross-job state artifact."""

    state = load_json(path)
    if state.get("schema") != 2:
        raise SafetyError("unsupported state schema")
    for name in ("base", "base_head", "branch", "cwd", "lint_commit"):
        if not isinstance(state.get(name), str) or state[name] == "":
            raise SafetyError(f"transaction state has an invalid {name}")
    if state["branch"] != branch_name(state["base"]):
        raise SafetyError("transaction branch does not match its base")
    records = state.get("delta")
    if not isinstance(records, list):
        raise SafetyError("transaction delta must be a list")
    if not isinstance(state.get("changed"), bool):
        raise SafetyError("transaction changed flag must be boolean")
    if state["changed"] != bool(records):
        raise SafetyError("transaction changed flag disagrees with its delta")
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise SafetyError("transaction delta record must be an object")
        required = {"content", "kind", "mode", "path", "sha256"}
        if set(record) != required:
            raise SafetyError("transaction delta record has unexpected fields")
        for name in required:
            if not isinstance(record[name], str):
                raise SafetyError(f"transaction delta has an invalid {name}")
        relative = prepared_relative_path(record["path"])
        canonical = relative.as_posix()
        if canonical in seen:
            raise SafetyError(f"transaction delta repeats a path: {canonical}")
        seen.add(canonical)
        if record["kind"] == "file":
            prepared_payload(record)
        elif record["kind"] == "deleted":
            if record["content"] != "" or record["sha256"] != "":
                raise SafetyError(f"prepared deletion contains bytes: {canonical}")
        else:
            raise SafetyError(f"prepared path has an unsupported kind: {canonical}")
    validate_prepared_modes(records)
    lint_release = state.get("lint_release")
    if not isinstance(lint_release, dict):
        raise SafetyError("transaction lint release must be an object")
    required_release = {
        "commit",
        "dependency_sha256",
        "images",
        "manifest_sha256",
        "tag_object",
        "tree",
    }
    if set(lint_release) != required_release:
        raise SafetyError("transaction lint release has unexpected fields")
    if lint_release.get("commit") != state["lint_commit"]:
        raise SafetyError("transaction lint release commit does not match")
    for name in (
        "commit",
        "tag_object",
        "tree",
    ):
        value = lint_release.get(name)
        if not isinstance(value, str) or GIT_OBJECT_PATTERN.fullmatch(value) is None:
            raise SafetyError(f"transaction lint release has an invalid {name}")
    for name in ("dependency_sha256", "manifest_sha256"):
        value = lint_release.get(name)
        if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
            raise SafetyError(f"transaction lint release has an invalid {name}")
    images = lint_release.get("images")
    if not isinstance(images, dict) or not images:
        raise SafetyError("transaction lint release images must be an object")
    for image, digest in images.items():
        if not isinstance(image, str) or not image.startswith(IMAGE_PREFIX):
            raise SafetyError("transaction lint release image is invalid")
        if not isinstance(digest, str):
            raise SafetyError("transaction lint release digest must be a string")
        if IMAGE_DIGEST_PATTERN.fullmatch(digest) is None:
            raise SafetyError("transaction lint release digest is invalid")
    repository_name = state.get("repository")
    if repository_name is not None:
        if not isinstance(repository_name, str):
            raise SafetyError("transaction repository must be a string")
        normalize_repository(repository_name)
    return state


def transaction_repository(
    arguments: argparse.Namespace,
    state: dict[str, Any],
) -> Path:
    """Resolve the checkout for this phase without trusting an old runner path."""

    value = state["cwd"]
    if arguments.cwd is not None:
        value = arguments.cwd
    workspace_root = None
    if arguments.workspace_root is not None:
        workspace_root = Path(arguments.workspace_root)
    return repository_root(Path(value).resolve(), workspace_root)


def validate_transaction_binding(
    arguments: argparse.Namespace,
    state: dict[str, Any],
) -> None:
    """Bind untrusted state routing fields to trusted phase inputs."""

    if state["base"] != arguments.base:
        raise SafetyError("transaction state does not match the trusted base")
    if arguments.repository is None:
        return
    trusted_repository = normalize_repository(arguments.repository)
    state_repository = state.get("repository")
    if state_repository is None:
        return
    if normalize_repository(state_repository) != trusted_repository:
        raise SafetyError("transaction state does not match the trusted repository")


def verify_transaction_checkout(
    repository: Path,
    state: dict[str, Any],
    *,
    restore: bool,
) -> None:
    """Recreate and verify the exact prepared delta without credentials."""

    isolated = consumer_git_environment(os.environ)
    current_head = git(
        repository,
        "rev-parse",
        "HEAD",
        environment=isolated,
    ).stdout.strip()
    if current_head != state["base_head"]:
        raise SafetyError("base checkout changed after preparation")
    if restore:
        ensure_clean(repository)
        restore_prepared_delta(repository, state["delta"])
    assert_delta(repository, state["delta"])


def run_verify(arguments: argparse.Namespace) -> dict[str, Any]:
    """Restore and attest a cross-job delta before any write token is injected."""

    refuse_pull_request_target(os.environ)
    state_path = transaction_state_path(arguments)
    state = transaction_state(state_path)
    validate_transaction_binding(arguments, state)
    repository = transaction_repository(arguments, state)
    verify_transaction_checkout(
        repository,
        state,
        restore=arguments.restore,
    )
    verification_path = arguments.verification
    if verification_path is None:
        verification_path = str(state_path.with_suffix(".verified.json"))
    receipt = {
        "base_head": state["base_head"],
        "cwd": str(repository),
        "delta_sha256": canonical_sha256(state["delta"]),
        "schema": 1,
        "state_sha256": sha256(state_path),
    }
    write_canonical(Path(verification_path).resolve(), receipt)
    write_action_output("base-head", state["base_head"])
    write_action_output("changed", str(state["changed"]).lower())
    return receipt


def verify_prior_receipt(
    arguments: argparse.Namespace,
    state_path: Path,
    state: dict[str, Any],
    repository: Path,
) -> None:
    """Bind publication to the preceding token-free verification step."""

    verification_path = arguments.verification
    if verification_path is None:
        verification_path = str(state_path.with_suffix(".verified.json"))
    receipt_path = Path(verification_path).resolve()
    if not receipt_path.is_file():
        raise SafetyError("token-free verification receipt is missing")
    receipt = load_json(receipt_path)
    expected = {
        "base_head": state["base_head"],
        "cwd": str(repository),
        "delta_sha256": canonical_sha256(state["delta"]),
        "schema": 1,
        "state_sha256": sha256(state_path),
    }
    if receipt != expected:
        raise SafetyError("token-free verification receipt does not match")


def select_existing_pull_request(
    pull_requests: list[dict[str, Any]],
    base: str,
    branch: str,
    repository_name: str,
) -> dict[str, Any] | None:
    if not pull_requests:
        return None
    if len(pull_requests) != 1:
        raise SafetyError("expected at most one open auto-lint pull request")
    pull_request = pull_requests[0]
    base_record = pull_request.get("base")
    head_record = pull_request.get("head")
    if not isinstance(base_record, dict):
        raise SafetyError("existing pull request has no base record")
    if not isinstance(head_record, dict):
        raise SafetyError("existing pull request has no head record")
    actual_base = base_record.get("ref")
    actual_head = head_record.get("ref")
    if actual_base != base or actual_head != branch:
        raise SafetyError(
            "existing pull request does not match base and head; " + EXISTING_PR_REMEDY
        )
    head_repository = head_record.get("repo")
    if not isinstance(head_repository, dict):
        raise SafetyError("existing pull request has no head repository")
    if head_repository.get("full_name") != repository_name:
        raise SafetyError("existing pull request has the wrong head repository")
    return pull_request


def require_token() -> dict[str, str]:
    trusted_gh()
    token = os.environ.get("GH_TOKEN")
    if token is None or token == "":
        token = os.environ.get("GITHUB_TOKEN")
    if token is None or token == "":
        raise SafetyError("publish requires GH_TOKEN or GITHUB_TOKEN")
    environment = token_free_environment(os.environ)
    environment["GH_TOKEN"] = token
    environment["GH_HOST"] = "github.com"
    return environment


def normalize_repository(value: str) -> str:
    if REPOSITORY_PATTERN.fullmatch(value) is None:
        raise SafetyError("repository must use the owner/name form")
    return value


RECORD_DIRECTORY: Path | None = None
RECORD_SEQUENCE = 0


def scrub_recorded_value(value: Any) -> Any:
    """Return a deep copy with file contents replaced.

    Recorded requests exist so the test suite can compare its
    stubs to what GitHub actually says. The base64 file payloads
    inside a createCommitOnBranch input add consumer file bytes
    without adding response shape, so they are dropped at record
    time rather than trusted to a later sanitization pass.
    """

    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, entry in value.items():
            if key == "contents":
                scrubbed[key] = "<omitted>"
            else:
                scrubbed[key] = scrub_recorded_value(entry)
        return scrubbed
    if isinstance(value, list):
        return [scrub_recorded_value(entry) for entry in value]
    return value


def record_api_exchange(
    arguments: Sequence[str],
    input_value: Any | None,
    completed: subprocess.CompletedProcess[str],
) -> None:
    """Write one request/response pair into the record directory.

    The record deliberately excludes the process environment: the
    token lives there and only there, so leaving the mapping out
    entirely is what keeps a recording safe to read back.
    """

    global RECORD_SEQUENCE
    if RECORD_DIRECTORY is None:
        return
    RECORD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    RECORD_SEQUENCE += 1
    record = {
        "arguments": list(arguments),
        "input": scrub_recorded_value(input_value),
        "returncode": completed.returncode,
        "stderr": completed.stderr,
        "stdout": completed.stdout,
    }
    path = RECORD_DIRECTORY / f"{RECORD_SEQUENCE:04d}.json"
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def gh_api(
    arguments: Sequence[str],
    environment: Mapping[str, str],
    input_value: Any | None = None,
) -> Any:
    command = [str(trusted_gh()), "api", *arguments]
    input_text = None
    if input_value is not None:
        command.extend(["--input", "-"])
        input_text = json.dumps(input_value, separators=(",", ":"))
    completed = subprocess.run(
        command,
        check=False,
        env=environment,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    record_api_exchange(arguments, input_value, completed)
    if completed.returncode != 0:
        raise CommandError(f"GitHub API failed: {completed.stderr.strip()}")
    if completed.stdout.strip() == "":
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CommandError("GitHub API returned invalid JSON") from error


def remote_tip(
    repository_name: str,
    branch: str,
    environment: Mapping[str, str],
) -> str | None:
    repository_name = normalize_repository(repository_name)
    value = gh_api(
        [
            "--method",
            "GET",
            f"repos/{repository_name}/git/matching-refs/heads/{branch}",
        ],
        environment,
    )
    if not isinstance(value, list):
        raise CommandError("remote branch query did not return a list")
    exact = []
    for record in value:
        if record.get("ref") == f"refs/heads/{branch}":
            exact.append(record)
    if not exact:
        return None
    if len(exact) != 1:
        raise SafetyError("remote branch query returned duplicate refs")
    object_record = exact[0].get("object")
    if not isinstance(object_record, dict):
        raise SafetyError("remote branch query has no object")
    sha = object_record.get("sha")
    if not isinstance(sha, str):
        raise SafetyError("remote branch query has no commit")
    return sha


def require_remote_base_tip(
    repository_name: str,
    base: str,
    expected: str,
    environment: Mapping[str, str],
) -> None:
    """Require the publication base to remain at the prepared commit."""

    actual = remote_tip(repository_name, base, environment)
    if actual != expected:
        raise SafetyError("remote base branch changed after preparation")


def open_pull_requests(
    repository_name: str,
    branch: str,
    environment: Mapping[str, str],
) -> list[dict[str, Any]]:
    repository_name = normalize_repository(repository_name)
    owner = repository_name.split("/", 1)[0]
    value = gh_api(
        [
            "--method",
            "GET",
            f"repos/{repository_name}/pulls",
            "-f",
            "state=open",
            "-f",
            f"head={owner}:{branch}",
        ],
        environment,
    )
    if not isinstance(value, list):
        raise CommandError("pull request query did not return a list")
    return value


def assert_delta(repository: Path, expected: list[dict[str, str]]) -> None:
    actual = delta_records(repository)
    if actual != expected:
        raise SafetyError("working tree changed after token-free preparation")


def require_matching_pull_tip(
    pull_request: dict[str, Any],
    tip: str,
) -> None:
    head = pull_request.get("head")
    if not isinstance(head, dict):
        raise SafetyError("existing pull request has no head record")
    if head.get("sha") != tip:
        raise SafetyError("existing pull request head differs from branch tip")


def require_matching_pull_base(
    pull_request: dict[str, Any],
    base: str,
) -> None:
    base_record = pull_request.get("base")
    if not isinstance(base_record, dict):
        raise SafetyError("existing pull request has no base record")
    if base_record.get("sha") != base:
        raise SafetyError("existing pull request base differs from branch tip")


def revalidate_publication(
    repository_name: str,
    base: str,
    base_head: str,
    branch: str,
    branch_tip: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Re-read every mutable publication ref immediately before success."""

    require_remote_base_tip(repository_name, base, base_head, environment)
    actual_branch_tip = remote_tip(repository_name, branch, environment)
    if actual_branch_tip != branch_tip:
        raise SafetyError("auto-lint branch changed during publication")
    pull_request = select_existing_pull_request(
        open_pull_requests(repository_name, branch, environment),
        base=base,
        branch=branch,
        repository_name=repository_name,
    )
    if pull_request is None:
        raise SafetyError("published auto-lint branch has no open pull request")
    require_matching_pull_tip(pull_request, branch_tip)
    require_matching_pull_base(pull_request, base_head)
    return pull_request


def committed_by_bot_or_github(login: Any, email: Any) -> bool:
    """Report whether a committer identity is one this tool creates.

    Two identities qualify. The bot commits when a caller builds the
    commit itself. GitHub commits when it signs one on the bot's
    behalf, which is what `createCommitOnBranch` does, and it stamps
    its own `web-flow` identity while doing so. Both halves of an
    identity must agree; a login without its matching email is a
    different account wearing the name.
    """
    if login == BOT_LOGIN and email == BOT_EMAIL:
        return True
    return login == GITHUB_SIGNER_LOGIN and email == GITHUB_SIGNER_EMAIL


def validate_branch_commits(
    commits: list[dict[str, Any]],
    remedy: str = STALE_BRANCH_REMEDY,
) -> None:
    """Require every branch-only commit to be signed and bot-owned.

    Two call paths share these checks: the audit of an existing
    auto-lint branch, where deleting the stale branch is the way
    forward, and `verify_created_commit` on a commit this tool just
    made, where deletion fixes nothing. The caller names the remedy
    that is true for its own path.
    """

    def refuse(message: str) -> None:
        raise SafetyError(message + "; " + remedy)

    if not commits:
        refuse("existing auto-lint branch has no unique commits")
    for record in commits:
        author = record.get("author")
        committer = record.get("committer")
        commit = record.get("commit")
        if not isinstance(author, dict) or author.get("login") != BOT_LOGIN:
            refuse("auto-lint branch has a non-bot author")
        if not isinstance(commit, dict):
            refuse("auto-lint branch commit metadata is missing")
        raw_author = commit.get("author")
        raw_committer = commit.get("committer")
        verification = commit.get("verification")
        signature = record.get("github_signature")
        if not isinstance(raw_author, dict):
            refuse("auto-lint branch author metadata is missing")
        if raw_author.get("email") != BOT_EMAIL:
            refuse("auto-lint branch author email is not the bot")
        if not isinstance(verification, dict):
            refuse("auto-lint branch signature metadata is missing")
        if verification.get("verified") is not True:
            refuse("auto-lint branch commit is not verified")
        if not isinstance(signature, dict):
            refuse("auto-lint branch has no GitHub signature proof")
        if signature.get("isValid") is not True:
            refuse("auto-lint branch GitHub signature is invalid")
        if signature.get("wasSignedByGitHub") is not True:
            refuse("auto-lint branch was not signed by GitHub")
        # Deliberately after the signature checks. GitHub's signing
        # identity is only an acceptable committer because the three
        # assertions above have already established that GitHub, and
        # not a person, produced this commit.
        if not isinstance(committer, dict) or not isinstance(raw_committer, dict):
            refuse("auto-lint branch committer metadata is missing")
        if not committed_by_bot_or_github(
            committer.get("login"),
            raw_committer.get("email"),
        ):
            refuse(
                "auto-lint branch has a non-bot committer: "
                + str(committer.get("login"))
            )


def github_commit_signature(
    repository_name: str,
    oid: str,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Return GitHub-specific signature proof for one commit."""

    owner, name = normalize_repository(repository_name).split("/", 1)
    query = """\
query($owner: String!, $name: String!, $oid: GitObjectID!) {
  repository(owner: $owner, name: $name) {
    object(oid: $oid) {
      ... on Commit {
        oid
        signature {
          isValid
          wasSignedByGitHub
        }
      }
    }
  }
}
"""
    value = gh_api(
        ["graphql"],
        environment,
        input_value={
            "query": query,
            "variables": {
                "name": name,
                "oid": oid,
                "owner": owner,
            },
        },
    )
    if not isinstance(value, dict):
        raise CommandError("signature query did not return an object")
    data = value.get("data")
    if not isinstance(data, dict):
        raise CommandError("signature query has no data")
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise CommandError("signature query has no repository")
    commit = repository.get("object")
    if not isinstance(commit, dict) or commit.get("oid") != oid:
        raise CommandError("signature query has the wrong commit")
    signature = commit.get("signature")
    if not isinstance(signature, dict):
        raise SafetyError("commit has no GitHub signature")
    return signature


def commit_changed_paths(
    repository_name: str,
    oid: str,
    environment: Mapping[str, str],
) -> set[str]:
    """Return the complete bounded path set changed by one commit."""

    repository_name = normalize_repository(repository_name)
    value = gh_api(
        [
            "--paginate",
            "--slurp",
            "--method",
            "GET",
            f"repos/{repository_name}/commits/{oid}?per_page=100",
        ],
        environment,
    )
    if not isinstance(value, list) or not value:
        raise CommandError("commit path query returned no pages")
    paths = set()
    for page in value:
        if not isinstance(page, dict) or page.get("sha") != oid:
            raise CommandError("commit path query returned the wrong commit")
        files = page.get("files")
        if not isinstance(files, list):
            raise CommandError("commit path query has no file inventory")
        for record in files:
            if not isinstance(record, dict):
                raise CommandError("commit path record is invalid")
            filename = record.get("filename")
            if not isinstance(filename, str) or filename == "":
                raise CommandError("commit path record has no filename")
            paths.add(filename)
            previous_filename = record.get("previous_filename")
            if previous_filename is not None:
                if not isinstance(previous_filename, str) or previous_filename == "":
                    raise CommandError(
                        "commit path record has an invalid previous filename"
                    )
                paths.add(previous_filename)
    if not paths:
        raise SafetyError("auto-lint branch commit has no changed paths")
    if len(paths) >= 3000:
        raise SafetyError("auto-lint branch commit exceeds the path audit bound")
    return paths


def branch_commits(
    repository_name: str,
    base: str,
    tip: str,
    prepared_paths: set[str],
    environment: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Return the bounded branch-only commit inventory."""

    value = gh_api(
        [
            "--method",
            "GET",
            f"repos/{repository_name}/compare/{base}...{tip}" "?per_page=100&page=1",
        ],
        environment,
    )
    if not isinstance(value, dict):
        raise CommandError("branch comparison did not return an object")
    merge_base = value.get("merge_base_commit")
    if value.get("status") != "ahead":
        message = (
            "auto-lint branch does not descend from the exact base; "
            "the base branch moved after this branch was created; "
            + STALE_BRANCH_REMEDY
        )
        merge_base_sha = None
        if isinstance(merge_base, dict):
            candidate = merge_base.get("sha")
            if isinstance(candidate, str) and candidate != "":
                merge_base_sha = candidate
        if merge_base_sha is None:
            # Without a merge base there is nothing to audit, so the
            # refusal stands and recovery is not offered.
            raise SafetyError(message)
        raise StaleBranchError(message, merge_base_sha)
    if not isinstance(merge_base, dict) or merge_base.get("sha") != base:
        raise SafetyError(
            "auto-lint branch has a different merge base; " + STALE_BRANCH_REMEDY
        )
    commits = value.get("commits")
    total = value.get("total_commits")
    if not isinstance(commits, list) or not isinstance(total, int):
        raise CommandError("branch comparison has invalid commit metadata")
    if total != len(commits):
        raise SafetyError("auto-lint branch exceeds the 100-commit audit bound")
    for record in commits:
        oid = record.get("sha")
        if not isinstance(oid, str):
            raise CommandError("branch comparison commit has no object ID")
        record["github_signature"] = github_commit_signature(
            repository_name,
            oid,
            environment,
        )
        paths = commit_changed_paths(
            repository_name,
            oid,
            environment,
        )
        unexpected = paths - prepared_paths
        if unexpected:
            raise SafetyError(
                "auto-lint branch commit changed paths outside the prepared delta"
            )
        record["changed_paths"] = sorted(paths)
    validate_branch_commits(commits)
    return commits


def audit_stale_branch_commits(
    repository_name: str,
    merge_base: str,
    tip: str,
    environment: Mapping[str, str],
) -> None:
    """Audit ownership of every commit unique to a stale branch.

    The comparison runs from the merge base rather than the new
    base head, because a stale branch by definition does not
    descend from the current base; the merge base is where its
    unique commits begin. The changed-paths check that
    `branch_commits` applies does not run here: those commits
    carried a previous run's prepared delta, and their content is
    about to be discarded by the reset, so ownership and GitHub
    signatures are the load-bearing checks. The identity gates and
    the 100-commit audit bound are the same ones every other branch
    audit uses.
    """

    value = gh_api(
        [
            "--method",
            "GET",
            f"repos/{repository_name}/compare/{merge_base}...{tip}"
            "?per_page=100&page=1",
        ],
        environment,
    )
    if not isinstance(value, dict):
        raise CommandError("branch comparison did not return an object")
    commits = value.get("commits")
    total = value.get("total_commits")
    if not isinstance(commits, list) or not isinstance(total, int):
        raise CommandError("branch comparison has invalid commit metadata")
    if total != len(commits):
        raise SafetyError("auto-lint branch exceeds the 100-commit audit bound")
    if not commits:
        # A branch with no unique commits is behind the base: its tip
        # is already an ancestor of main, so a reset discards nothing.
        # This is the routine shape after a merged auto-lint pull
        # request whose branch was never deleted, and it needs no
        # ownership proof because there is no unique content to own.
        return
    for record in commits:
        oid = record.get("sha")
        if not isinstance(oid, str):
            raise CommandError("branch comparison commit has no object ID")
        record["github_signature"] = github_commit_signature(
            repository_name,
            oid,
            environment,
        )
    validate_branch_commits(commits)


def recover_stale_branch(
    repository_name: str,
    branch: str,
    base_head: str,
    tip: str,
    merge_base: str,
    environment: Mapping[str, str],
) -> None:
    """Reset a fully audited stale auto-lint branch to the base head.

    This tool still never deletes a branch. It may move its own
    branch, and only after every branch-only commit has proved bot
    ownership and a GitHub signature through the audit above; a
    single commit that fails that audit leaves the existing refusal
    in place. The ref update must be forced because the stale
    branch does not descend from the base. Re-reading the tip
    afterwards closes the race window the forced update opens: a
    writer that moves the branch between the reset and the re-read
    is detected here, and one that moves it after the re-read still
    fails atomically at `create_signed_commit`'s expected head.
    """

    audit_stale_branch_commits(
        repository_name,
        merge_base,
        tip,
        environment,
    )
    gh_api(
        [
            "--method",
            "PATCH",
            f"repos/{repository_name}/git/refs/heads/{branch}",
        ],
        environment,
        input_value={"force": True, "sha": base_head},
    )
    reset_tip = remote_tip(repository_name, branch, environment)
    if reset_tip != base_head:
        raise SafetyError(
            "auto-lint branch moved during its stale-branch reset; "
            "no commit attempted"
        )
    print(
        f"stale-branch recovery: reset {branch} from stale tip {tip} "
        f"to base {base_head} after every branch-only commit passed "
        "the bot-ownership and signature audit",
        file=sys.stderr,
    )


def git_blob_object_id(payload: bytes) -> str:
    """Return the SHA-1 object ID used by current GitHub repositories."""

    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload, usedforsecurity=False).hexdigest()


def remote_tree(
    repository_name: str,
    commit: str,
    environment: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    """Return one complete, untruncated remote commit tree."""

    commit_value = gh_api(
        [
            "--method",
            "GET",
            f"repos/{repository_name}/git/commits/{commit}",
        ],
        environment,
    )
    if not isinstance(commit_value, dict):
        raise CommandError("remote commit query did not return an object")
    tree_record = commit_value.get("tree")
    if not isinstance(tree_record, dict):
        raise CommandError("remote commit has no tree")
    tree_sha = tree_record.get("sha")
    if not isinstance(tree_sha, str):
        raise CommandError("remote commit tree has no object ID")
    value = gh_api(
        [
            "--method",
            "GET",
            f"repos/{repository_name}/git/trees/{tree_sha}?recursive=1",
        ],
        environment,
    )
    if not isinstance(value, dict):
        raise CommandError("remote tree query did not return an object")
    if value.get("truncated") is True:
        raise SafetyError("remote tree exceeds the GitHub audit bound")
    entries = value.get("tree")
    if not isinstance(entries, list):
        raise CommandError("remote tree has no entries")
    result = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise CommandError("remote tree entry is invalid")
        entry_type = entry.get("type")
        if entry_type not in {"blob", "commit"}:
            continue
        path = entry.get("path")
        mode = entry.get("mode")
        sha = entry.get("sha")
        if not isinstance(path, str):
            raise CommandError("remote tree entry has no path")
        if not isinstance(mode, str) or not isinstance(sha, str):
            raise CommandError("remote tree entry metadata is invalid")
        if path in result:
            raise SafetyError("remote tree contains a duplicate path")
        result[path] = {
            "mode": mode,
            "sha": sha,
            "type": entry_type,
        }
    return result


def desired_tree_record(record: dict[str, str]) -> dict[str, str] | None:
    """Return the expected remote record for one prepared path."""

    if record["kind"] == "deleted":
        return None
    payload = prepared_payload(record)
    return {
        "mode": record["mode"],
        "sha": git_blob_object_id(payload),
        "type": "blob",
    }


def validate_base_tree(
    base_tree: dict[str, dict[str, str]],
    records: list[dict[str, str]],
) -> None:
    """Reject mode changes and unsupported new path kinds."""

    for record in records:
        path = record["path"]
        current = base_tree.get(path)
        desired = desired_tree_record(record)
        if record["kind"] == "deleted":
            if current is None:
                raise SafetyError(f"prepared deletion is absent from base: {path}")
            if current["type"] != "blob":
                raise SafetyError(f"prepared deletion is not a blob: {path}")
            if current["mode"] != "100644":
                raise SafetyError(f"base path has an unsupported mode: {path}")
            continue
        validate_prepared_modes([record])
        if current is not None:
            if current["type"] != "blob":
                raise SafetyError(f"prepared path is not a blob in base: {path}")
            if current["mode"] != "100644":
                raise SafetyError(f"base path has an unsupported mode: {path}")
        if current == desired:
            raise SafetyError(f"prepared path does not change base content: {path}")


def validate_existing_branch_tree(
    base_tree: dict[str, dict[str, str]],
    branch_tree: dict[str, dict[str, str]],
    records: list[dict[str, str]],
) -> bool:
    """Require branch residue and the next commit to match prepared paths."""

    changed = set()
    for path in set(base_tree) | set(branch_tree):
        if base_tree.get(path) != branch_tree.get(path):
            changed.add(path)
    prepared_paths = {record["path"] for record in records}
    if changed != prepared_paths:
        raise SafetyError("existing branch paths differ from the prepared delta")

    matched = 0
    for record in records:
        desired = desired_tree_record(record)
        if branch_tree.get(record["path"]) == desired:
            matched += 1
    if matched == len(records):
        return True
    if matched:
        raise SafetyError("existing branch contains only part of the prepared delta")
    return False


def create_remote_branch(
    repository_name: str,
    branch: str,
    base: str,
    environment: Mapping[str, str],
) -> None:
    """Create the exact temporary publication branch."""

    try:
        gh_api(
            [
                "--method",
                "POST",
                f"repos/{repository_name}/git/refs",
                "-f",
                f"ref=refs/heads/{branch}",
                "-f",
                f"sha={base}",
            ],
            environment,
        )
    except AutoLintError as error:
        if "HTTP 403" in error_chain_text(error):
            raise SafetyError(CONTENTS_WRITE_MESSAGE) from error
        raise SafetyError(
            "branch creation outcome is ambiguous; no commit attempted"
        ) from error


def create_signed_commit(
    repository_name: str,
    branch: str,
    expected_head: str,
    records: list[dict[str, str]],
    message: str,
    environment: Mapping[str, str],
) -> tuple[str, Any]:
    """Atomically append a GitHub-signed commit with exact file changes."""

    query = """\
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit {
      oid
      signature {
        isValid
        wasSignedByGitHub
      }
    }
    ref { target { oid } }
  }
}
"""
    variables = {
        "input": {
            "branch": {
                "branchName": branch,
                "repositoryNameWithOwner": repository_name,
            },
            "expectedHeadOid": expected_head,
            "fileChanges": publication_file_changes(records),
            "message": {"headline": message},
        }
    }
    value = gh_api(
        ["graphql"],
        environment,
        input_value={"query": query, "variables": variables},
    )
    if not isinstance(value, dict):
        raise CommandError("commit mutation did not return an object")
    data = value.get("data")
    if not isinstance(data, dict):
        raise CommandError("commit mutation has no data")
    payload = data.get("createCommitOnBranch")
    if not isinstance(payload, dict):
        raise CommandError("commit mutation has no payload")
    commit = payload.get("commit")
    ref = payload.get("ref")
    if not isinstance(commit, dict) or not isinstance(ref, dict):
        raise CommandError("commit mutation result is incomplete")
    oid = commit.get("oid")
    target = ref.get("target")
    if not isinstance(oid, str) or not isinstance(target, dict):
        raise CommandError("commit mutation object IDs are invalid")
    if target.get("oid") != oid:
        raise SafetyError("commit mutation ref does not match its commit")
    return oid, commit.get("signature")


def verify_created_commit(
    repository_name: str,
    oid: str,
    signature: Any,
    environment: Mapping[str, str],
) -> None:
    """Verify REST and GraphQL provenance after retaining the new object ID."""

    metadata = gh_api(
        [
            "--method",
            "GET",
            f"repos/{repository_name}/commits/{oid}",
        ],
        environment,
    )
    if not isinstance(metadata, dict):
        raise CommandError("created commit metadata is invalid")
    metadata["github_signature"] = signature
    validate_branch_commits([metadata], remedy=CREATED_COMMIT_REMEDY)


def apply_labels_and_reviewers(
    repository_name: str,
    number: int,
    labels: list[str],
    reviewers: list[str],
    environment: Mapping[str, str],
) -> None:
    if labels:
        arguments = [
            "--method",
            "POST",
            f"repos/{repository_name}/issues/{number}/labels",
        ]
        for label in labels:
            arguments.extend(["-f", f"labels[]={label}"])
        gh_api(arguments, environment)
    if reviewers:
        arguments = [
            "--method",
            "POST",
            f"repos/{repository_name}/pulls/{number}/requested_reviewers",
        ]
        for reviewer in reviewers:
            arguments.extend(["-f", f"reviewers[]={reviewer}"])
        gh_api(arguments, environment)


def run_publish(arguments: argparse.Namespace) -> dict[str, Any]:
    refuse_pull_request_target(os.environ)
    state_path = transaction_state_path(arguments)
    state = transaction_state(state_path)
    validate_transaction_binding(arguments, state)
    repository = transaction_repository(arguments, state)
    verify_transaction_checkout(repository, state, restore=False)
    verify_prior_receipt(
        arguments,
        state_path,
        state,
        repository,
    )
    if not state["changed"]:
        result = {"changed": False, "pull_request": None, "schema": 1}
        write_action_output("changed", "false")
        return result
    repository_name = arguments.repository
    if repository_name is None:
        repository_name = state.get("repository")
    if repository_name is None:
        repository_name = os.environ.get("GITHUB_REPOSITORY")
    if not isinstance(repository_name, str) or repository_name == "":
        raise SafetyError("publish requires --repository or GITHUB_REPOSITORY")
    repository_name = normalize_repository(repository_name)
    environment = require_token()
    require_remote_base_tip(
        repository_name,
        state["base"],
        state["base_head"],
        environment,
    )
    base_tree = remote_tree(
        repository_name,
        state["base_head"],
        environment,
    )
    validate_base_tree(base_tree, state["delta"])

    branch = state["branch"]
    pulls = open_pull_requests(repository_name, branch, environment)
    pull_request = select_existing_pull_request(
        pulls,
        base=state["base"],
        branch=branch,
        repository_name=repository_name,
    )
    tip = remote_tip(
        repository_name,
        branch,
        environment,
    )
    if pull_request is not None and tip is None:
        raise SafetyError("open auto-lint PR has no remote head branch")
    if pull_request is not None:
        require_matching_pull_tip(pull_request, tip)

    published_commit = None
    expected_head = state["base_head"]
    if tip is not None:
        if tip == state["base_head"] and pull_request is None:
            raise SafetyError(
                "existing auto-lint branch has no ownership proof; "
                + STALE_BRANCH_REMEDY
            )
        else:
            recovered_from_stale_branch = False
            try:
                branch_commits(
                    repository_name,
                    state["base_head"],
                    tip,
                    {record["path"] for record in state["delta"]},
                    environment,
                )
            except StaleBranchError as stale:
                # The audited reset leaves the branch at the base
                # head, so publication proceeds exactly as it does
                # for a freshly created branch: `expected_head` stays
                # at the base head and the signed commit lands on the
                # reset branch. An open matching pull request, already
                # validated above, simply follows the branch.
                recover_stale_branch(
                    repository_name,
                    branch,
                    state["base_head"],
                    tip,
                    stale.merge_base,
                    environment,
                )
                recovered_from_stale_branch = True
            if not recovered_from_stale_branch:
                branch_tree = remote_tree(repository_name, tip, environment)
                if validate_existing_branch_tree(
                    base_tree,
                    branch_tree,
                    state["delta"],
                ):
                    if pull_request is None:
                        published_commit = tip
                    else:
                        number = pull_request.get("number")
                        if not isinstance(number, int):
                            raise CommandError(
                                "pull request response is missing its number"
                            )
                        require_remote_base_tip(
                            repository_name,
                            state["base"],
                            state["base_head"],
                            environment,
                        )
                        apply_labels_and_reviewers(
                            repository_name,
                            number,
                            arguments.label,
                            arguments.reviewer,
                            environment,
                        )
                        pull_request = revalidate_publication(
                            repository_name,
                            state["base"],
                            state["base_head"],
                            branch,
                            tip,
                            environment,
                        )
                        number = pull_request.get("number")
                        if not isinstance(number, int):
                            raise CommandError(
                                "pull request response is missing its number"
                            )
                        result = {
                            "changed": False,
                            "pull_request": number,
                            "schema": 1,
                        }
                        write_action_output("changed", "false")
                        write_action_output("pull-request", str(number))
                        return result
                expected_head = tip
    else:
        create_remote_branch(
            repository_name,
            branch,
            state["base_head"],
            environment,
        )

    if published_commit is None:
        try:
            published_commit, published_signature = create_signed_commit(
                repository_name,
                branch,
                expected_head,
                state["delta"],
                "Apply automated formatting",
                environment,
            )
            verify_created_commit(
                repository_name,
                published_commit,
                published_signature,
                environment,
            )
            published_tree = remote_tree(
                repository_name,
                published_commit,
                environment,
            )
            if not validate_existing_branch_tree(
                base_tree,
                published_tree,
                state["delta"],
            ):
                raise SafetyError("published tree differs from the prepared delta")
            published_tip = remote_tip(
                repository_name,
                branch,
                environment,
            )
            if published_tip != published_commit:
                raise SafetyError("published commit is not the branch tip")
        except AutoLintError as error:
            raise SafetyError(
                "commit outcome is ambiguous; publication branch retained"
            ) from error

    require_remote_base_tip(
        repository_name,
        state["base"],
        state["base_head"],
        environment,
    )

    if pull_request is not None:
        pull_request = select_existing_pull_request(
            open_pull_requests(repository_name, branch, environment),
            base=state["base"],
            branch=branch,
            repository_name=repository_name,
        )
        if pull_request is None:
            raise SafetyError("updated auto-lint branch lost its pull request")
        require_matching_pull_tip(
            pull_request,
            published_commit,
        )

    if pull_request is None:
        try:
            response = gh_api(
                [
                    "--method",
                    "POST",
                    f"repos/{repository_name}/pulls",
                    "-f",
                    f"title={arguments.title}",
                    "-f",
                    f"head={branch}",
                    "-f",
                    f"base={state['base']}",
                    "-f",
                    f"body={arguments.body}",
                ],
                environment,
            )
            if not isinstance(response, dict):
                raise CommandError("pull request creation returned invalid data")
            pull_request = select_existing_pull_request(
                [response],
                base=state["base"],
                branch=branch,
                repository_name=repository_name,
            )
            if pull_request is None:
                raise CommandError("pull request creation returned no pull request")
            require_matching_pull_tip(
                pull_request,
                published_commit,
            )
        except AutoLintError as error:
            try:
                pull_request = select_existing_pull_request(
                    open_pull_requests(repository_name, branch, environment),
                    base=state["base"],
                    branch=branch,
                    repository_name=repository_name,
                )
                if pull_request is not None:
                    require_matching_pull_tip(
                        pull_request,
                        published_commit,
                    )
            except AutoLintError as reconciliation_error:
                if PR_CREATION_FORBIDDEN_MARKER in error_chain_text(error):
                    raise SafetyError(PR_CREATION_SETTING_MESSAGE) from error
                # The reconciliation error carries the chain; the
                # creation error would otherwise be lost with it, so
                # its text is folded into the message.
                raise SafetyError(
                    "pull request creation outcome is ambiguous; "
                    "publication branch retained; creation failed "
                    "with: " + error_chain_text(error)
                ) from reconciliation_error
            if pull_request is None:
                if PR_CREATION_FORBIDDEN_MARKER in error_chain_text(error):
                    raise SafetyError(PR_CREATION_SETTING_MESSAGE) from error
                raise SafetyError(
                    "pull request creation outcome is ambiguous; "
                    "publication branch retained"
                ) from error
    number = pull_request.get("number")
    if not isinstance(number, int):
        raise CommandError("pull request response is missing its number")
    apply_labels_and_reviewers(
        repository_name,
        number,
        arguments.label,
        arguments.reviewer,
        environment,
    )
    pull_request = revalidate_publication(
        repository_name,
        state["base"],
        state["base_head"],
        branch,
        published_commit,
        environment,
    )
    number = pull_request.get("number")
    if not isinstance(number, int):
        raise CommandError("pull request response is missing its number")
    result = {"changed": True, "pull_request": number, "schema": 1}
    write_action_output("changed", "true")
    write_action_output("pull-request", str(number))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    global RECORD_DIRECTORY
    arguments = parser().parse_args(argv)
    if arguments.record is not None:
        RECORD_DIRECTORY = Path(arguments.record)
    try:
        result: dict[str, Any]
        if arguments.phase == "prepare":
            result = run_prepare(arguments)
        elif arguments.phase == "verify":
            result = run_verify(arguments)
        elif arguments.phase == "publish":
            result = run_publish(arguments)
        else:
            raise SafetyError("unsupported transaction phase")
    except AutoLintError as error:
        print(str(error), file=sys.stderr)
        cause = error.__cause__
        while cause is not None:
            print("caused by: " + str(cause), file=sys.stderr)
            cause = cause.__cause__
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
