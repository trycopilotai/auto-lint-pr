#!/usr/bin/env python3
"""Generate deterministic token-boundary demo evidence."""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT_PATH = ROOT / "evidence" / "demo-transcript.txt"
DEMO_PATH = ROOT / "assets" / "demo.svg"
MANIFEST_PATH = ROOT / "evidence" / "demo-manifest.json"
EVIDENCE_DATE = "2026-08-02"
FIXTURE_DATE = "2026-08-02T00:00:00Z"
TOKEN_NAMES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
)
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def isolated_git_environment(
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("GIT_"):
            del environment[name]
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    if overrides is not None:
        environment.update(overrides)
    return environment


def load_implementation():
    path = ROOT / "auto_lint_pr.py"
    specification = importlib.util.spec_from_file_location(
        "auto_lint_pr_demo",
        path,
    )
    if specification is None:
        raise RuntimeError("could not create module specification")
    if specification.loader is None:
        raise RuntimeError("module specification has no loader")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


AUTO_LINT_PR = load_implementation()


def git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> str:
    isolated_environment = isolated_git_environment(environment)
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        env=isolated_environment,
        stdout=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def initialize_repository(repository: Path) -> None:
    repository.mkdir()
    git(
        repository,
        "init",
        "-q",
        "-b",
        "main",
        "--object-format=sha1",
    )
    git(repository, "config", "user.name", "Demo Author")
    git(
        repository,
        "config",
        "user.email",
        "demo@example.invalid",
    )


def commit_all(repository: Path) -> str:
    git(repository, "add", "-A")
    environment = {
        "GIT_AUTHOR_DATE": FIXTURE_DATE,
        "GIT_AUTHOR_EMAIL": "demo@example.invalid",
        "GIT_AUTHOR_NAME": "Demo Author",
        "GIT_COMMITTER_DATE": FIXTURE_DATE,
        "GIT_COMMITTER_EMAIL": "demo@example.invalid",
        "GIT_COMMITTER_NAME": "Demo Author",
    }
    git(
        repository,
        "commit",
        "-qm",
        "demo fixture",
        environment=environment,
    )
    return git(repository, "rev-parse", "HEAD")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_demo() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        lint_root = root / "lint"
        initialize_repository(lint_root)
        lint_script = lint_root / "lint.py"
        lint_script.write_text(
            """\
import os
import sys
from pathlib import Path

for name in (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
):
    if os.environ.get(name):
        raise SystemExit(90)

arguments = sys.argv[1:]
cwd = Path(arguments[arguments.index("--cwd") + 1])
(cwd / "sample.txt").write_text(
    "formatted\\n",
    encoding="utf-8",
    newline="\\n",
)
""",
            encoding="utf-8",
            newline="\n",
        )
        lint_commit = commit_all(lint_root)
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "images": {},
                    "release": "fixture",
                    "schema_version": 1,
                    "source": {
                        "archive": "lint-fixture.tar.gz",
                        "commit": lint_commit,
                        "sha256": sha256(lint_script),
                    },
                    "tools": {},
                }
            ),
            encoding="utf-8",
            newline="\n",
        )

        consumer = root / "consumer"
        initialize_repository(consumer)
        (consumer / "sample.txt").write_text(
            "needs-formatting\n",
            encoding="utf-8",
            newline="\n",
        )
        consumer_commit = commit_all(consumer)
        state_path = root / "state.json"
        arguments = AUTO_LINT_PR.parser().parse_args(
            [
                "prepare",
                "--lint-root",
                str(lint_root),
                "--manifest",
                str(manifest),
                "--cwd",
                str(consumer),
                "--state",
                str(state_path),
            ]
        )
        previous: dict[str, str | None] = {}
        for name in TOKEN_NAMES:
            previous[name] = os.environ.get(name)
            os.environ[name] = "demo-write-token"
        verified_manifest = json.loads(manifest.read_text(encoding="utf-8"))
        verified_manifest["verified_dependency"] = {
            "commit": lint_commit,
            "dependency_sha256": "1" * 64,
            "images": {
                "ghcr.io/trycopilotai/lint-requirements": "sha256:" + "2" * 64,
            },
            "manifest_sha256": sha256(manifest),
            "tag_object": "3" * 40,
            "tree": git(lint_root, "rev-parse", "HEAD^{tree}"),
        }

        def verify_fixture(*values):
            verified_root = values[4]
            AUTO_LINT_PR.materialize_git_tree(
                lint_root,
                lint_commit,
                verified_root,
                AUTO_LINT_PR.dependency_git_environment(os.environ),
            )
            return verified_manifest

        try:
            with mock.patch.object(
                AUTO_LINT_PR,
                "verify_lint_release",
                side_effect=verify_fixture,
            ):
                state = AUTO_LINT_PR.run_prepare(arguments)
            return {
                "fixture_consumer_commit": consumer_commit,
                "fixture_lint_commit": lint_commit,
                "state": state,
            }
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def transcript() -> tuple[str, dict[str, str]]:
    result = prepare_demo()
    state = result["state"]
    if not isinstance(state, dict):
        raise RuntimeError("demo preparation did not return state")
    paths = []
    for record in state["delta"]:
        paths.append(record["path"])
    lines = [
        "$ python3 auto_lint_pr.py prepare --lint-root <pinned-lint>",
        "prepare phase: success",
        "formatter credentials: absent",
        f"base commit recorded: {bool(state['base_head'])}",
        f"branch: {state['branch']}",
        f"changed: {str(state['changed']).lower()}",
        f"exact delta: {', '.join(paths)}",
        "publish phase: not run in this token-free demo",
    ]
    run = {
        "fixture_consumer_commit": str(result["fixture_consumer_commit"]),
        "fixture_lint_commit": str(result["fixture_lint_commit"]),
    }
    return "\n".join(lines) + "\n", run


def render_svg(payload: str) -> str:
    lines = payload.rstrip("\n").splitlines()
    text_elements: list[str] = []
    animation_rules: list[str] = []
    y = 132
    for index, line in enumerate(lines):
        escaped = html.escape(line)
        text_elements.append(
            f'  <text x="72" y="{y}" '
            f'class="terminal-line line-{index}">{escaped}</text>'
        )
        reveal = (index + 1) * 9
        animation_rules.append(
            f"""\
    .line-{index} {{
      animation: reveal-{index} 10s linear infinite;
    }}
    @keyframes reveal-{index} {{
      0%, {reveal}% {{ opacity: 0; }}
      {reveal + 0.1}%, 92% {{ opacity: 1; }}
      92.1%, 100% {{ opacity: 0; }}
    }}"""
        )
        y += 46
    body = "\n".join(text_elements)
    animation = "\n".join(animation_rules)
    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640"
  viewBox="0 0 1280 640" role="img"
  aria-labelledby="title description">
  <title id="title">auto-lint-pr token boundary demo</title>
  <desc id="description">
    A deterministic terminal transcript showing a token-free
    formatter phase and its exact file delta.
  </desc>
  <rect width="1280" height="640" fill="#0b1020"/>
  <rect x="42" y="42" width="1196" height="556" rx="24"
    fill="#111936" stroke="#33426f" stroke-width="2"/>
  <circle cx="82" cy="80" r="8" fill="#ff6b6b"/>
  <circle cx="110" cy="80" r="8" fill="#ffd166"/>
  <circle cx="138" cy="80" r="8" fill="#57d39b"/>
  <style>
    .terminal-line {{
      fill: #e7ecff;
      font: 25px ui-monospace, SFMono-Regular, Menlo, monospace;
      opacity: 0;
    }}
{animation}
    @media (prefers-reduced-motion: reduce) {{
      .terminal-line {{
        animation: none;
        opacity: 1;
      }}
    }}
  </style>
{body}
</svg>
"""


def artifact_record(relative: str) -> dict[str, str]:
    """Return one path and checksum record for deterministic evidence."""

    return {
        "path": relative,
        "sha256": sha256(ROOT / relative),
    }


def rendered_artifact_record(relative: str, payload: str) -> dict[str, str]:
    """Return one path and checksum record for newly rendered text."""

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "path": relative,
        "sha256": digest,
    }


def demo_manifest(
    run: dict[str, str],
    metadata: dict[str, str],
    transcript_payload: str,
    demo_payload: str,
) -> str:
    """Derive the evidence manifest from the artifacts that own each fact."""

    output = rendered_artifact_record(
        "evidence/demo-transcript.txt",
        transcript_payload,
    )
    value = {
        "demo": rendered_artifact_record("assets/demo.svg", demo_payload),
        "generator": artifact_record("scripts/generate_demo.py"),
        "implementation": artifact_record("auto_lint_pr.py"),
        "invocation": artifact_record("scripts/demo.sh"),
        "output": output,
        "poster": artifact_record("assets/poster.svg"),
        "run": {
            "agent": metadata["agent"],
            "agent_version": metadata["agent_version"],
            "date": EVIDENCE_DATE,
            "edited": False,
            "fixture_consumer_commit": run["fixture_consumer_commit"],
            "fixture_lint_commit": run["fixture_lint_commit"],
            "input_commit": metadata["input_commit"],
            "invocation": metadata["invocation"],
            "output_sha256": output["sha256"],
            "protocol_sha256": sha256(ROOT / "skills" / "auto-lint-pr" / "SKILL.md"),
        },
        "schema": 1,
        "skill": artifact_record("skills/auto-lint-pr/SKILL.md"),
        "social_preview": artifact_record("assets/social-preview.png"),
        "social_preview_source": artifact_record("assets/social-preview.svg"),
    }
    return json.dumps(value, indent=2) + "\n"


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser()
    mode = argument_parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    argument_parser.add_argument("--input-commit")
    argument_parser.add_argument("--agent")
    argument_parser.add_argument("--agent-version")
    return argument_parser


def require_candidate_commit(value: str) -> str:
    """Require a full candidate commit reachable from the current checkout."""

    if GIT_COMMIT_PATTERN.fullmatch(value) is None:
        raise ValueError("demo input commit must be a full SHA-1 commit")
    resolved = git(ROOT, "rev-parse", f"{value}^{{commit}}")
    if resolved != value:
        raise ValueError("demo input commit does not resolve exactly")
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", value, "HEAD"],
        cwd=ROOT,
        check=False,
        env=isolated_git_environment(),
    )
    if completed.returncode != 0:
        raise ValueError("demo input commit is not reachable from HEAD")
    return value


def recorded_invocation() -> str:
    """Record the real generator invocation in a repository-relative form."""

    script = Path(sys.argv[0]).resolve()
    try:
        script_value = script.relative_to(ROOT).as_posix()
    except ValueError:
        script_value = str(script)
    executable = Path(sys.executable).name
    return shlex.join([executable, script_value, *sys.argv[1:]])


def manifest_metadata(arguments: argparse.Namespace) -> dict[str, str]:
    """Load check metadata or bind a write to its actual invocation."""

    if arguments.check:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        run = manifest.get("run")
        if not isinstance(run, dict):
            raise ValueError("demo manifest run metadata is missing")
        metadata = {}
        for name in ("agent", "agent_version", "input_commit", "invocation"):
            value = run.get(name)
            if not isinstance(value, str) or value == "":
                raise ValueError(f"demo manifest has an invalid {name}")
            metadata[name] = value
    else:
        values = {
            "agent": arguments.agent,
            "agent_version": arguments.agent_version,
            "input_commit": arguments.input_commit,
        }
        metadata = {}
        for name, value in values.items():
            if not isinstance(value, str) or value == "":
                option = name.replace("_", "-")
                raise ValueError(f"--write requires --{option}")
            metadata[name] = value
        metadata["invocation"] = recorded_invocation()
    metadata["input_commit"] = require_candidate_commit(metadata["input_commit"])
    return metadata


def main() -> int:
    arguments = parser().parse_args()
    if not arguments.check and not arguments.write:
        payload, unused_run = transcript()
        print(payload, end="")
        return 0
    try:
        metadata = manifest_metadata(arguments)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    payload, run = transcript()
    svg = render_svg(payload)
    manifest = demo_manifest(run, metadata, payload, svg)
    if arguments.check:
        if TRANSCRIPT_PATH.read_text(encoding="utf-8") != payload:
            print("demo transcript is stale", file=sys.stderr)
            return 1
        if DEMO_PATH.read_text(encoding="utf-8") != svg:
            print("demo SVG is stale", file=sys.stderr)
            return 1
        if MANIFEST_PATH.read_text(encoding="utf-8") != manifest:
            print("demo manifest is stale", file=sys.stderr)
            return 1
        return 0
    TRANSCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEMO_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_PATH.write_text(
        payload,
        encoding="utf-8",
        newline="\n",
    )
    DEMO_PATH.write_text(
        svg,
        encoding="utf-8",
        newline="\n",
    )
    MANIFEST_PATH.write_text(
        manifest,
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
