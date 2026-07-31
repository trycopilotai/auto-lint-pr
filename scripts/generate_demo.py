#!/usr/bin/env python3
"""Generate deterministic token-boundary demo evidence."""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT_PATH = ROOT / "evidence" / "demo-transcript.txt"
DEMO_PATH = ROOT / "assets" / "demo.svg"
MANIFEST_PATH = ROOT / "evidence" / "demo-manifest.json"
TOKEN_NAMES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
)


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


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def initialize_repository(repository: Path) -> None:
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Demo Author")
    git(
        repository,
        "config",
        "user.email",
        "demo@example.invalid",
    )


def commit_all(repository: Path) -> str:
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", "demo fixture")
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
(cwd / "sample.txt").write_text("formatted\\n", encoding="utf-8")
""",
            encoding="utf-8",
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
        )

        consumer = root / "consumer"
        initialize_repository(consumer)
        (consumer / "sample.txt").write_text(
            "needs-formatting\n",
            encoding="utf-8",
        )
        commit_all(consumer)
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
        try:
            return AUTO_LINT_PR.run_prepare(arguments)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def transcript() -> str:
    state = prepare_demo()
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
    return "\n".join(lines) + "\n"


def render_svg(payload: str) -> str:
    lines = payload.rstrip("\n").splitlines()
    text_elements: list[str] = []
    y = 132
    for line in lines:
        escaped = html.escape(line)
        text_elements.append(
            f'  <text x="72" y="{y}" ' 'class="terminal-line">' f"{escaped}</text>"
        )
        y += 46
    body = "\n".join(text_elements)
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


def demo_manifest() -> str:
    """Derive the evidence manifest from the artifacts that own each fact."""

    value = {
        "demo": artifact_record("assets/demo.svg"),
        "generator": artifact_record("scripts/generate_demo.py"),
        "invocation": artifact_record("scripts/demo.sh"),
        "output": artifact_record("evidence/demo-transcript.txt"),
        "poster": artifact_record("assets/poster.svg"),
        "schema": 1,
        "skill": artifact_record("skills/auto-lint-pr/SKILL.md"),
        "social_preview": artifact_record("assets/social-preview.png"),
        "social_preview_source": artifact_record("assets/social-preview.svg"),
    }
    return json.dumps(value, indent=2) + "\n"


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--check", action="store_true")
    argument_parser.add_argument("--write", action="store_true")
    return argument_parser


def main() -> int:
    arguments = parser().parse_args()
    payload = transcript()
    svg = render_svg(payload)
    if arguments.check:
        if TRANSCRIPT_PATH.read_text(encoding="utf-8") != payload:
            print("demo transcript is stale", file=sys.stderr)
            return 1
        if DEMO_PATH.read_text(encoding="utf-8") != svg:
            print("demo SVG is stale", file=sys.stderr)
            return 1
        if MANIFEST_PATH.read_text(encoding="utf-8") != demo_manifest():
            print("demo manifest is stale", file=sys.stderr)
            return 1
        return 0
    if not arguments.write:
        print(payload, end="")
        return 0
    TRANSCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEMO_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRANSCRIPT_PATH.write_text(payload, encoding="utf-8")
    DEMO_PATH.write_text(svg, encoding="utf-8")
    MANIFEST_PATH.write_text(demo_manifest(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
