#!/usr/bin/env python3
"""Turn pinned lint output into a reviewable pull request."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "lint-release-manifest.json"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
TOKEN_NAMES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
)
REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/" r"[A-Za-z0-9_.-]+"
)


class AutoLintError(Exception):
    """Base error for the auto-lint transaction."""


class SafetyError(AutoLintError):
    """A branch, pull request, or delta failed a safety check."""


class DependencyError(AutoLintError):
    """The pinned lint dependency could not be verified."""


class CommandError(AutoLintError):
    """A formatter, hook, Git, or GitHub command failed."""


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        description="Format a repository and publish the exact delta as a PR",
    )
    argument_parser.add_argument(
        "phase",
        nargs="?",
        choices=("run", "prepare", "publish"),
        default="run",
    )
    argument_parser.add_argument("paths", nargs="*")
    argument_parser.add_argument("--cwd", default=".")
    argument_parser.add_argument("--lint-root", required=True)
    argument_parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
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
        default=".git/auto-lint-pr-state.json",
    )
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
    for name in TOKEN_NAMES:
        isolated.pop(name, None)
    return isolated


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


def lint_command(arguments: argparse.Namespace) -> list[str]:
    validate_selection(arguments)
    lint_root = Path(arguments.lint_root).resolve()
    command = [
        sys.executable,
        str(lint_root / "lint.py"),
        "--write",
        "--cwd",
        str(Path(arguments.cwd).resolve()),
    ]
    if arguments.docker:
        command.append("--docker")
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


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DependencyError(f"could not load {path}") from error
    if not isinstance(value, dict):
        raise DependencyError(f"{path} must contain a JSON object")
    return value


def git(
    repository: Path,
    *arguments: str,
    check: bool = True,
    environment: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *arguments],
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
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != 1:
        raise DependencyError("unsupported lint release manifest schema")
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise DependencyError("lint release manifest is missing source")
    expected_commit = source.get("commit")
    if not isinstance(expected_commit, str):
        raise DependencyError("lint release manifest is missing commit")
    actual_commit = git(lint_root, "rev-parse", "HEAD").stdout.strip()
    if actual_commit != expected_commit:
        raise DependencyError(
            f"lint checkout is {actual_commit}; expected {expected_commit}"
        )
    tracked = git(
        lint_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
    ).stdout
    if tracked != "":
        raise DependencyError("lint checkout has tracked modifications")
    return manifest


def repository_root(cwd: Path) -> Path:
    completed = git(cwd, "rev-parse", "--show-toplevel")
    return Path(completed.stdout.strip()).resolve()


def changed_paths(repository: Path) -> list[str]:
    tracked = git(
        repository,
        "diff",
        "--name-only",
        "-z",
        "HEAD",
    ).stdout
    untracked = git(
        repository,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
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
        target = os.readlink(path)
        record["kind"] = "symlink"
        record["sha256"] = hashlib.sha256(target.encode("utf-8")).hexdigest()
        return record
    if path.is_file():
        record["kind"] = "file"
        record["sha256"] = sha256(path)
        return record
    if not path.exists():
        record["kind"] = "deleted"
        record["sha256"] = ""
        return record
    raise SafetyError(f"changed path is not a file: {relative}")


def delta_records(repository: Path) -> list[dict[str, str]]:
    return [file_record(repository, relative) for relative in changed_paths(repository)]


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
    cwd = Path(arguments.cwd).resolve()
    repository = repository_root(cwd)
    ensure_clean(repository)
    lint_root = Path(arguments.lint_root).resolve()
    manifest = verify_lint_release(
        lint_root,
        Path(arguments.manifest).resolve(),
    )
    isolated = token_free_environment(os.environ)
    run_checked(
        lint_command(arguments),
        cwd=repository,
        environment=isolated,
    )
    if arguments.hook is not None:
        run_checked(
            arguments.hook,
            cwd=repository,
            environment=isolated,
            shell=True,
        )
    records = delta_records(repository)
    state = {
        "base": arguments.base,
        "base_head": git(repository, "rev-parse", "HEAD").stdout.strip(),
        "branch": branch_name(arguments.base),
        "changed": bool(records),
        "cwd": str(repository),
        "delta": records,
        "lint_commit": manifest["source"]["commit"],
        "repository": arguments.repository,
        "schema": 1,
    }
    write_canonical(Path(arguments.state).resolve(), state)
    write_action_output("changed", str(bool(records)).lower())
    write_action_output("branch", state["branch"])
    return state


def select_existing_pull_request(
    pull_requests: list[dict[str, Any]],
    base: str,
    branch: str,
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
        raise SafetyError("existing pull request does not match base and head")
    return pull_request


def require_bot_tip(email: str) -> None:
    if email != BOT_EMAIL:
        raise SafetyError("existing auto-lint branch tip is not bot-authored")


def require_token() -> dict[str, str]:
    environment = dict(os.environ)
    token = environment.get("GH_TOKEN")
    if token is None or token == "":
        token = environment.get("GITHUB_TOKEN")
    if token is None or token == "":
        raise SafetyError("publish requires GH_TOKEN or GITHUB_TOKEN")
    environment["GH_TOKEN"] = token
    return environment


def normalize_repository(value: str) -> str:
    if REPOSITORY_PATTERN.fullmatch(value) is None:
        raise SafetyError("repository must use the owner/name form")
    return value


def gh_api(
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> Any:
    completed = subprocess.run(
        ["gh", "api", *arguments],
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise CommandError(f"GitHub API failed: {completed.stderr.strip()}")
    if completed.stdout.strip() == "":
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CommandError("GitHub API returned invalid JSON") from error


def authenticated_git(
    repository: Path,
    environment: Mapping[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return git(
        repository,
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        *arguments,
        environment=environment,
    )


def remote_tip(
    repository: Path,
    repository_name: str,
    branch: str,
    environment: Mapping[str, str],
) -> str | None:
    repository_name = normalize_repository(repository_name)
    url = f"https://github.com/{repository_name}.git"
    completed = authenticated_git(
        repository,
        environment,
        "ls-remote",
        "--heads",
        url,
        f"refs/heads/{branch}",
    )
    output = completed.stdout.strip()
    if output == "":
        return None
    fields = output.split()
    if len(fields) != 2:
        raise SafetyError("remote branch query returned an invalid record")
    return fields[0]


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


def commit_delta(
    repository: Path,
    parent: str,
    message: str,
) -> str:
    git(repository, "add", "-A", "--", ".")
    staged = git(
        repository,
        "diff",
        "--cached",
        "--name-only",
        "-z",
    ).stdout
    staged_paths = sorted(value for value in staged.split("\0") if value)
    if staged_paths != changed_paths(repository):
        raise SafetyError("staged paths differ from the prepared delta")
    tree = git(repository, "write-tree").stdout.strip()
    parent_tree = git(repository, "rev-parse", f"{parent}^{{tree}}").stdout.strip()
    if tree == parent_tree:
        return parent
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": BOT_NAME,
            "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT_NAME,
            "GIT_COMMITTER_EMAIL": BOT_EMAIL,
        }
    )
    completed = git(
        repository,
        "commit-tree",
        tree,
        "-p",
        parent,
        environment=environment,
        input_text=message + "\n",
    )
    return completed.stdout.strip()


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
    state = load_json(Path(arguments.state).resolve())
    if state.get("schema") != 1:
        raise SafetyError("unsupported state schema")
    repository = Path(state["cwd"]).resolve()
    if git(repository, "rev-parse", "HEAD").stdout.strip() != state["base_head"]:
        raise SafetyError("base checkout changed after preparation")
    assert_delta(repository, state["delta"])
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
    branch = state["branch"]
    pulls = open_pull_requests(repository_name, branch, environment)
    pull_request = select_existing_pull_request(
        pulls,
        base=state["base"],
        branch=branch,
    )
    tip = remote_tip(
        repository,
        repository_name,
        branch,
        environment,
    )
    if pull_request is None and tip is not None:
        raise SafetyError("remote auto-lint branch has no matching open PR")
    if pull_request is not None and tip is None:
        raise SafetyError("open auto-lint PR has no remote head branch")
    if pull_request is not None:
        require_matching_pull_tip(pull_request, tip)
    parent = state["base_head"]
    if tip is not None:
        url = f"https://github.com/{repository_name}.git"
        authenticated_git(
            repository,
            environment,
            "fetch",
            "--no-tags",
            url,
            tip,
        )
        fetched = git(repository, "rev-parse", "FETCH_HEAD").stdout.strip()
        if fetched != tip:
            raise SafetyError("fetched branch tip differs from remote query")
        email = git(
            repository,
            "show",
            "-s",
            "--format=%ae",
            tip,
        ).stdout.strip()
        require_bot_tip(email)
        parent = tip
    commit = commit_delta(
        repository,
        parent,
        "Apply automated formatting",
    )
    if commit == parent and pull_request is not None:
        result = {
            "changed": False,
            "pull_request": pull_request["number"],
            "schema": 1,
        }
        write_action_output("changed", "false")
        return result
    url = f"https://github.com/{repository_name}.git"
    authenticated_git(
        repository,
        environment,
        "push",
        url,
        f"{commit}:refs/heads/{branch}",
    )
    if pull_request is None:
        pull_request = gh_api(
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
        if not isinstance(pull_request, dict):
            raise CommandError("pull request creation returned invalid data")
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
    result = {"changed": True, "pull_request": number, "schema": 1}
    write_action_output("changed", "true")
    write_action_output("pull-request", str(number))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        result: dict[str, Any]
        if arguments.phase == "prepare":
            result = run_prepare(arguments)
        elif arguments.phase == "publish":
            result = run_publish(arguments)
        else:
            run_prepare(arguments)
            result = run_publish(arguments)
    except AutoLintError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
