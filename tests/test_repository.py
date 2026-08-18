from __future__ import annotations

import datetime
import html
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import verify_repo  # noqa: E402


def run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


class ActionMetadataTest(unittest.TestCase):
    def test_token_is_only_in_publish_step(self) -> None:
        text = (ROOT / "action.yml").read_text(encoding="utf-8")
        prepare, publish = text.split(
            "    - name: Publish exact verified delta",
            maxsplit=1,
        )

        self.assertNotIn("${{ inputs.token }}", prepare)
        self.assertIn('GH_TOKEN: ""', prepare)
        self.assertEqual(2, prepare.count('INPUT_TOKEN: ""'))
        self.assertIn(
            'GH_TOKEN: "${{ inputs.token }}"',
            publish,
        )

    def test_action_has_typed_transaction_inputs(self) -> None:
        text = (ROOT / "action.yml").read_text(encoding="utf-8")
        required = (
            "modified:",
            "paths:",
            "files-from0:",
            "languages:",
            "hook:",
            "labels:",
            "reviewers:",
            "state-path:",
            "verification-path:",
        )
        for value in required:
            self.assertIn(value, text)


class RepositoryPathTest(unittest.TestCase):
    def test_windows_git_symlink_placeholder_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills" / "auto-lint-pr").mkdir(parents=True)
            skill = root / "skill"
            skill.write_text("skills/auto-lint-pr\n", encoding="utf-8")

            verify_repo.verify_skill_entry(skill, "nt")

            with self.assertRaisesRegex(
                ValueError,
                "skill must be a symbolic link",
            ):
                verify_repo.verify_skill_entry(skill, "posix")


class WorkflowMetadataTest(unittest.TestCase):
    def test_reusable_workflow_has_safety_controls(self) -> None:
        path = ROOT / ".github" / "workflows" / "auto-lint-pr.yml"
        text = path.read_text(encoding="utf-8")

        self.assertIn("workflow_call:", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("pull-requests: write", text)
        self.assertIn("issues: write", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("secrets.checkout_token", text)
        self.assertIn("secrets.registry_token", text)
        self.assertIn("packages: read", text)
        self.assertIn("Prefetch private images by exact digest", text)
        self.assertIn(
            'DOCKER_CONFIG="$auth" "$docker_path" logout ghcr.io',
            text,
        )
        self.assertIn("grep -F -q 'ghcr.io'", text)
        self.assertIn('DOCKER_CONFIG: "${{ runner.temp }}/docker-clean"', text)
        self.assertIn("workspace-root: workspace", text)
        self.assertIn('token: "${{ github.token }}"', text)
        self.assertIn(
            'repository: "${{ job.workflow_repository }}"',
            text,
        )
        self.assertIn('ref: "${{ job.workflow_sha }}"', text)
        self.assertNotIn("github.workflow_sha", text)
        self.assertNotIn("secrets.token", text)
        self.assertNotIn("pull_request_target", text)
        self.assertIn("needs: prepare", text)
        self.assertIn("actions/upload-artifact@", text)
        self.assertIn("actions/download-artifact@", text)
        prepare, remaining = text.split("\n  verify:\n", maxsplit=1)
        verify, publish = remaining.split("\n  publish:\n", maxsplit=1)
        self.assertNotIn('token: "${{ github.token }}"', prepare)
        self.assertNotIn('token: "${{ github.token }}"', verify)
        self.assertIn('token: "${{ github.token }}"', publish)
        self.assertEqual(2, verify.count("actions/checkout@"))
        self.assertIn("phase: verify", verify)
        self.assertIn("consumer-base.bundle", verify)
        self.assertIn("auto-lint-pr.tar.gz", verify)
        consumer_checkout = verify.split(
            "- name: Check out exact consumer base",
            maxsplit=1,
        )[1].split("- name: Check out this action revision", maxsplit=1)[0]
        self.assertIn("fetch-depth: 0", consumer_checkout)
        self.assertNotIn("actions/checkout@", publish)
        self.assertNotIn("secrets.checkout_token", publish)
        self.assertNotIn("secrets.registry_token", publish)
        self.assertIn("auto-lint-pr-publication", publish)
        self.assertIn('GH_TOKEN: ""', publish)
        self.assertIn('GITHUB_TOKEN: ""', publish)
        for output in (
            "action-archive-sha",
            "consumer-bundle-sha",
            "state-sha",
            "verification-sha",
        ):
            self.assertIn(output, publish)

    def test_reusable_workflow_treats_languages_as_shell_data(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "auto-lint-pr.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('LINT_LANGUAGES: "${{ inputs.languages }}"', workflow)
        self.assertIn('--languages "$LINT_LANGUAGES"', workflow)
        self.assertNotIn('--languages "${{ inputs.languages }}"', workflow)
        self.assertIn("docker_path=/usr/bin/docker", workflow)
        self.assertIn("unset REGISTRY_TOKEN", workflow)
        self.assertNotIn('DOCKER_CONFIG="$auth" docker ', workflow)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            github_path = root / "github-path"
            arguments = root / "arguments"
            payload = f'$(printf injected >>"{github_path}")'
            environment = dict(os.environ)
            environment.update(
                {
                    "ARGUMENT_LOG": str(arguments),
                    "GITHUB_PATH": str(github_path),
                    "LINT_LANGUAGES": payload,
                }
            )
            subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    'set -- --languages "$LINT_LANGUAGES"; '
                    'printf "%s\\0" "$@" >"$ARGUMENT_LOG"',
                ],
                check=True,
                env=environment,
            )

            self.assertFalse(github_path.exists())
            self.assertEqual(
                [b"--languages", payload.encode("utf-8"), b""],
                arguments.read_bytes().split(b"\0"),
            )

    def test_consumer_base_bundle_is_complete_and_restorable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            run_git(source, "init", "-q", "-b", "main")
            run_git(source, "config", "user.name", "Fixture Author")
            run_git(
                source,
                "config",
                "user.email",
                "fixture@example.invalid",
            )
            (source / "first.txt").write_text(
                "first\n",
                encoding="utf-8",
                newline="\n",
            )
            run_git(source, "add", "first.txt")
            run_git(source, "commit", "-qm", "first")
            (source / "second.txt").write_text(
                "second\n",
                encoding="utf-8",
                newline="\n",
            )
            run_git(source, "add", "second.txt")
            run_git(source, "commit", "-qm", "second")
            base = run_git(source, "rev-parse", "HEAD")
            parent = run_git(source, "rev-parse", "HEAD^")

            workspace = root / "workspace"
            subprocess.run(
                ["git", "clone", "-q", str(source), str(workspace)],
                check=True,
            )
            run_git(
                workspace,
                "update-ref",
                "refs/auto-lint-pr/base",
                base,
            )
            bundle = root / "consumer-base.bundle"
            run_git(
                workspace,
                "bundle",
                "create",
                str(bundle),
                "refs/auto-lint-pr/base",
            )

            restored = root / "restored"
            restored.mkdir()
            run_git(restored, "init", "-q")
            run_git(
                restored,
                "fetch",
                "-q",
                str(bundle),
                "refs/auto-lint-pr/base",
            )

            self.assertEqual(base, run_git(restored, "rev-parse", "FETCH_HEAD"))
            run_git(restored, "cat-file", "-e", f"{parent}^{{commit}}")

    def test_checksum_bound_files_checkout_with_lf(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

        self.assertEqual("* text=auto eol=lf\n", attributes)

    def test_readme_requires_caller_write_permissions(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        marker = "The calling job must grant the workflow's write permissions"
        self.assertIn(marker, text)
        reusable = text.split("## Reusable workflow", maxsplit=1)[1]
        self.assertIn("contents: write", reusable)
        self.assertIn("issues: write", reusable)
        self.assertIn("pull-requests: write", reusable)
        self.assertIn("Allow GitHub Actions to create and approve", reusable)
        self.assertIn("pull requests", reusable)

    def test_ci_exercises_token_free_auto_lint_transaction(self) -> None:
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("python -m unittest discover", text)
        self.assertIn("python tools/verify_repo.py", text)
        self.assertNotIn("make verify", text)
        self.assertIn("python3 auto_lint_pr.py prepare", text)
        self.assertIn("--lint-root ../lint", text)
        self.assertIn("--cwd fixtures/integration", text)
        self.assertIn("--language requirements", text)
        self.assertIn("--local", text)
        self.assertIn(
            "python3 ../verification-source/auto_lint_pr.py verify",
            text,
        )
        self.assertIn("--restore", text)
        self.assertIn("verification-source", text)
        self.assertIn("auto-lint-pr-verified.json", text)
        self.assertIn("state_sha256", text)
        self.assertIn("token-free verification receipt", text)
        self.assertIn('GITHUB_TOKEN: ""', text)
        self.assertIn('GH_TOKEN: ""', text)
        self.assertIn(
            '"fixtures/integration/requirements.txt"',
            text,
        )
        self.assertNotIn("uses: trycopilotai/lint@", text)

    def test_consumer_workflows_pin_the_same_lint_ref(self) -> None:
        dependency = json.loads(
            (ROOT / "lint-dependency.json").read_text(encoding="utf-8")
        )
        ref = dependency["ref"]
        for name in ("auto-lint-pr.yml", "ci.yml"):
            workflow = ROOT / ".github" / "workflows" / name
            self.assertIn(ref, workflow.read_text(encoding="utf-8"))

    def test_ci_prefetches_then_runs_token_free_exact_digest(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("pinned-lint-docker:", workflow)
        self.assertIn("Authenticated exact-digest prefetch", workflow)
        self.assertIn("tools/prefetch_images.py", workflow)
        self.assertIn('docker pull "$image"', workflow)
        self.assertIn("docker logout ghcr.io", workflow)
        self.assertIn("grep -F -q 'ghcr.io'", workflow)
        self.assertIn(
            'DOCKER_CONFIG: "${{ runner.temp }}/docker-clean"',
            workflow,
        )
        self.assertIn("formatter-must-not-receive", workflow)
        self.assertIn('state["lint_release"]["images"][name]', workflow)
        self.assertNotIn("lint-requirements:v", workflow)

    def test_transitional_lint_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "lint-release-manifest.json"
            value = json.loads(
                (ROOT / "lint-release-manifest.json").read_text(encoding="utf-8")
            )
            value["schema_version"] = 2
            manifest.write_text(
                json.dumps(value),
                encoding="utf-8",
                newline="\n",
            )

            with self.assertRaisesRegex(
                ValueError,
                "repinned to final schema 1",
            ):
                verify_repo.verify_lint_manifest(manifest)

    def test_external_actions_use_commit_references(self) -> None:
        pattern = re.compile(r"uses:\s+([^@\s]+)@([^\s]+)")
        workflows = (ROOT / ".github" / "workflows").glob("*.yml")
        matches = 0
        for path in workflows:
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                matches += 1
                self.assertRegex(match.group(2), r"^[0-9a-f]{40}$")

        self.assertGreater(matches, 0)

    def test_release_verifies_the_ssh_signed_tag(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        allowed = (ROOT / ".github" / "release-allowed-signers").read_text(
            encoding="utf-8"
        )

        self.assertIn("gpg.format=ssh", workflow)
        self.assertIn(
            "gpg.ssh.allowedSignersFile=",
            workflow,
        )
        self.assertIn(
            "ref: cc4d422edf9f081ffcba6efa003b4340cd132167",
            workflow,
        )
        self.assertIn("path: release-trust", workflow)
        self.assertIn("sparse-checkout-cone-mode: false", workflow)
        self.assertIn("working-directory: release-source", workflow)
        self.assertIn(
            "$GITHUB_WORKSPACE/release-trust/.github/release-allowed-signers",
            workflow,
        )
        self.assertIn('verify-tag "$RELEASE_REF"', workflow)
        self.assertIn("release-commit:", workflow)
        self.assertIn("release-tag-object:", workflow)
        self.assertIn("needs.verify.outputs.release-commit", workflow)
        self.assertIn("needs.verify.outputs.release-tag-object", workflow)
        self.assertIn('ref: "${{ needs.verify.outputs.release-commit }}"', workflow)
        self.assertIn('"$RELEASE_COMMIT"', workflow)
        self.assertIn('"$RELEASE_TAG_OBJECT"', workflow)
        self.assertIn('rev-parse "$RELEASE_REF^{tag}"', workflow)
        self.assertIn(
            'test "$remote_tag_object" = "$RELEASE_TAG_OBJECT"',
            workflow,
        )
        self.assertIn("python3 tools/verify_release.py --ref", workflow)
        self.assertIn('"$RELEASE_REF"', workflow)
        self.assertRegex(
            allowed,
            r"^trycopilotai-release ssh-ed25519 " r"[A-Za-z0-9+/]+={0,2}\n$",
        )
        self.assertEqual(1, len(allowed.splitlines()))
        self.assertEqual(3, len(allowed.rstrip("\n").split(" ")))

    def test_release_version_closure_matches_v010(self) -> None:
        valid = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "verify_release.py"),
                "--ref",
                "v0.1.0",
            ],
            cwd=ROOT,
            check=False,
        )
        invalid = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "verify_release.py"),
                "--ref",
                "v0.1.1",
            ],
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(0, valid.returncode)
        self.assertNotEqual(0, invalid.returncode)


class InstallMetadataTest(unittest.TestCase):
    def test_product_installs_pin_the_release_tag(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(text.count("release=v0.1.0"), 2)
        self.assertIn(
            "/auto-lint-pr/archive/refs/tags/$release.tar.gz",
            text,
        )

    def test_reusable_workflow_install_is_executable(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("<full-commit-sha>", text)
        self.assertIn(
            "trycopilotai/auto-lint-pr/.github/workflows/" "auto-lint-pr.yml@v0.1.0",
            text,
        )

    def test_readme_requires_prepare_for_positional_paths(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("Positional paths require the", text)
        self.assertIn("python3 auto_lint_pr.py prepare", text)


class LaunchSurfaceTest(unittest.TestCase):
    def test_readme_has_icon_and_runnable_workflow_badges(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn('src="assets/icon.svg"', text)
        for workflow in (
            "ci.yml",
            "release.yml",
        ):
            badge = f"actions/workflows/{workflow}/badge.svg"
            self.assertEqual(1, text.count(badge))
        reusable_badge = "actions/workflows/auto-lint-pr.yml/badge.svg"
        self.assertNotIn(reusable_badge, text)

    def test_demo_is_transcript_derived_and_accessible(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        transcript = (ROOT / "evidence" / "demo-transcript.txt").read_text(
            encoding="utf-8"
        )
        demo = (ROOT / "assets" / "demo.svg").read_text(encoding="utf-8")

        self.assertIn("assets/demo.svg", readme)
        self.assertIn("assets/poster.svg", readme)
        self.assertIn("<picture>", readme)
        self.assertIn(
            'media="(prefers-reduced-motion: reduce)"',
            readme,
        )
        self.assertIn('srcset="assets/poster.svg"', readme)
        self.assertIn("Reconstructed", readme)
        lines = transcript.rstrip("\n").splitlines()
        for line in lines:
            self.assertIn(html.escape(line), demo)
        self.assertEqual(len(lines), demo.count("@keyframes reveal-"))
        self.assertIn("prefers-reduced-motion: reduce", demo)
        self.assertIn("animation: none", demo)

        manifest = json.loads(
            (ROOT / "evidence" / "demo-manifest.json").read_text(encoding="utf-8")
        )
        run = manifest["run"]
        self.assertNotEqual("generate_demo.py", run["agent"])
        self.assertNotEqual("", run["agent"])
        self.assertNotEqual("", run["agent_version"])
        self.assertRegex(run["date"], r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
        datetime.date.fromisoformat(run["date"])
        self.assertFalse(run["edited"])
        self.assertRegex(run["input_commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(run["fixture_consumer_commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(run["fixture_lint_commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(run["output_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(run["protocol_sha256"], r"^[0-9a-f]{64}$")
        invocation = shlex.split(run["invocation"])
        self.assertEqual(
            run["agent"],
            invocation[invocation.index("--agent") + 1],
        )
        self.assertEqual(
            run["agent_version"],
            invocation[invocation.index("--agent-version") + 1],
        )
        self.assertEqual(
            run["input_commit"],
            invocation[invocation.index("--input-commit") + 1],
        )
        self.assertEqual(
            run["date"],
            invocation[invocation.index("--evidence-date") + 1],
        )
        self.assertEqual(
            0,
            subprocess.run(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    run["input_commit"],
                    "HEAD",
                ],
                cwd=ROOT,
                check=False,
            ).returncode,
        )
        self.assertEqual(
            run_git(ROOT, "rev-parse", "HEAD^"),
            run["input_commit"],
        )
        self.assertEqual(
            manifest["output"]["sha256"],
            run["output_sha256"],
        )
        self.assertEqual(
            manifest["skill"]["sha256"],
            run["protocol_sha256"],
        )

    def test_demo_check_ignores_host_git_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "gitconfig"
            config.write_text(
                "[commit]\n\tgpgSign = true\n"
                "[init]\n\tdefaultObjectFormat = sha256\n",
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment["GIT_AUTHOR_NAME"] = "Host Override"
            environment["GIT_COMMITTER_NAME"] = "Host Override"
            environment["GIT_CONFIG_GLOBAL"] = str(config)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "generate_demo.py"),
                    "--check",
                ],
                cwd=ROOT,
                env=environment,
                check=False,
            )

        self.assertEqual(0, completed.returncode)

    def test_demo_write_requires_invocation_identity_and_candidate_commit(
        self,
    ) -> None:
        manifest_before = (ROOT / "evidence" / "demo-manifest.json").read_bytes()
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "generate_demo.py"),
                "--write",
            ],
            cwd=ROOT,
            check=False,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("--write requires --agent", completed.stderr)
        self.assertEqual(
            manifest_before,
            (ROOT / "evidence" / "demo-manifest.json").read_bytes(),
        )

    def test_demo_write_hashes_new_artifacts_and_then_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "candidate"
            shutil.copytree(
                ROOT,
                candidate,
                ignore=shutil.ignore_patterns(".git", "__pycache__"),
            )
            run_git(candidate, "init", "-q", "-b", "main", "--object-format=sha1")
            run_git(candidate, "config", "user.name", "Evidence Test")
            run_git(
                candidate,
                "config",
                "user.email",
                "evidence@example.invalid",
            )
            run_git(candidate, "config", "commit.gpgSign", "false")
            run_git(candidate, "add", "-A")
            run_git(candidate, "commit", "-qm", "candidate")
            input_commit = run_git(candidate, "rev-parse", "HEAD")
            transcript = candidate / "evidence" / "demo-transcript.txt"
            demo = candidate / "assets" / "demo.svg"
            transcript.write_text(
                "stale transcript\n",
                encoding="utf-8",
                newline="\n",
            )
            demo.write_text(
                "<svg><text>stale demo</text></svg>\n",
                encoding="utf-8",
                newline="\n",
            )
            write = subprocess.run(
                [
                    sys.executable,
                    "scripts/generate_demo.py",
                    "--write",
                    "--input-commit",
                    input_commit,
                    "--agent",
                    "Evidence Test",
                    "--agent-version",
                    "1",
                    "--evidence-date",
                    "2026-08-17",
                ],
                cwd=candidate,
                check=False,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(0, write.returncode, write.stderr)
            checked = subprocess.run(
                [
                    sys.executable,
                    "scripts/generate_demo.py",
                    "--check",
                ],
                cwd=candidate,
                check=False,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(0, checked.returncode, checked.stderr)
            manifest = json.loads(
                (candidate / "evidence" / "demo-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            transcript_sha256 = hashlib.sha256(transcript.read_bytes()).hexdigest()
            demo_sha256 = hashlib.sha256(demo.read_bytes()).hexdigest()
            self.assertEqual(
                transcript_sha256,
                manifest["output"]["sha256"],
            )
            self.assertEqual(
                transcript_sha256,
                manifest["run"]["output_sha256"],
            )
            self.assertEqual(
                demo_sha256,
                manifest["demo"]["sha256"],
            )

    def test_comparison_metric_and_article_are_present(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        article = ROOT / "docs" / "exact-delta-boundary.md"

        self.assertIn("Reviewed 2026-08-02", readme)
        self.assertIn(
            "peter-evans/create-pull-request/blob/"
            "11fa467881691ac900904a2eea702c5ea848ad13/README.md",
            readme,
        )
        self.assertIn(
            "Launch success is one external repository completing",
            readme,
        )
        self.assertTrue(article.is_file())
        self.assertIn(
            "This is a draft about that boundary.",
            article.read_text(encoding="utf-8"),
        )

    def test_issue_infrastructure_uses_documented_labels(self) -> None:
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
            self.assertEqual(1, labels.count(f"name: {label}"))
            for document in documents:
                self.assertIn(f"`{label}`", document)
        for name in (
            "config.yml",
            "consumer-integration.yml",
            "transaction-bug.yml",
        ):
            self.assertTrue((ROOT / ".github" / "ISSUE_TEMPLATE" / name).is_file())


class LaunchWordingTest(unittest.TestCase):
    """BLOCK-05: stale launch prose must not be able to return."""

    def test_current_launch_surface_is_clean(self) -> None:
        verify_repo.verify_public_wording()

    def test_phrase_wrapped_by_the_codec_is_still_detected(self) -> None:
        readme = ROOT / "README.md"
        original = readme.read_text(encoding="utf-8")
        try:
            readme.write_text(
                original
                + "\nMarketplace steps are deferred until a\n"
                + "separately approved public launch.\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                verify_repo.verify_public_wording()
        finally:
            readme.write_text(original, encoding="utf-8")
        verify_repo.verify_public_wording()

    def test_every_recipient_visible_document_is_scanned(self) -> None:
        for name in verify_repo.RECIPIENT_VISIBLE_DOCUMENTS:
            self.assertTrue((ROOT / name).is_file(), name)


class PluginClaimTest(unittest.TestCase):
    """BLOCK-11: the plugin description is the README claim."""

    def test_manifest_descriptions_match_the_readme_claim(self) -> None:
        verify_repo.verify_plugins()
        claim = verify_repo.readme_claim()
        self.assertIn("trycopilotai/lint", claim)
        for relative in (
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
        ):
            value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(claim, value["description"], relative)

    def test_a_drifted_description_is_rejected(self) -> None:
        path = ROOT / ".claude-plugin" / "plugin.json"
        original = path.read_text(encoding="utf-8")
        try:
            value = json.loads(original)
            drifted = original.replace(
                json.dumps(value["description"]),
                json.dumps("Something else entirely."),
                1,
            )
            path.write_text(drifted, encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_repo.verify_plugins()
        finally:
            path.write_text(original, encoding="utf-8")
        verify_repo.verify_plugins()


class EvidenceDateProvenanceTest(unittest.TestCase):
    """BLOCK-04: the capture date is an input, not a source constant."""

    def setUp(self) -> None:
        self.generator = _load_generator()

    def test_generator_carries_no_hard_coded_evidence_date(self) -> None:
        source = (ROOT / "scripts" / "generate_demo.py").read_text(encoding="utf-8")
        self.assertNotIn("EVIDENCE_DATE = ", source)

    def test_missing_date_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.generator.require_evidence_date(None)

    def test_malformed_dates_are_rejected(self) -> None:
        for value in ("2026-8-2", "20260802", "2026-13-01", "", "today"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.generator.require_evidence_date(value)

    def test_well_formed_date_is_accepted(self) -> None:
        self.assertEqual(
            "2026-08-17",
            self.generator.require_evidence_date("2026-08-17"),
        )

    def test_invocation_must_record_the_date(self) -> None:
        with self.assertRaises(ValueError):
            self.generator.require_invocation_date(
                "python3 scripts/generate_demo.py --write",
                "2026-08-17",
            )

    def test_invocation_date_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.generator.require_invocation_date(
                "python3 scripts/generate_demo.py --evidence-date 2026-08-16",
                "2026-08-17",
            )

    def test_invocation_agreement_is_accepted(self) -> None:
        self.generator.require_invocation_date(
            "python3 scripts/generate_demo.py --evidence-date 2026-08-17",
            "2026-08-17",
        )


def _load_generator():
    import importlib.util

    path = ROOT / "scripts" / "generate_demo.py"
    specification = importlib.util.spec_from_file_location(
        "generate_demo_under_test",
        path,
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
