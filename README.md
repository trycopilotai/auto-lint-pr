<img src="assets/icon.svg" alt="" width="96" align="right">

# auto-lint-pr

[![Auto lint PR](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/auto-lint-pr.yml/badge.svg)](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/auto-lint-pr.yml)
[![CI](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/ci.yml/badge.svg)](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/ci.yml)
[![Release](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/release.yml/badge.svg)](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/release.yml)

Turn pinned
[`trycopilotai/lint`](https://github.com/trycopilotai/lint)
formatter output into a reviewable pull request without
granting the formatter step a write token.

The transaction has two credential phases:

1. A token-free phase verifies the pinned lint release,
   applies formatting, optionally runs one consumer command,
   and records the exact file delta.
2. A fresh publish job restores and re-verifies that delta
   without credentials. Only its following trusted substep
   receives the write token, creates a bot-authored commit,
   and creates or updates the matching pull request.

The tool refuses to reuse a remote branch unless there is
exactly one matching open pull request, its base and head
branches match, its reported head commit matches the remote
tip, and that tip is authored by `github-actions[bot]`.

![Reconstructed token-free prepare demo](assets/demo.svg)

_Reconstructed from the deterministic
[token-free transcript](evidence/demo-transcript.txt). A
[static poster](assets/poster.svg) is available when motion
is not useful._

## CLI

Provide an exact checkout of the lint release named by
`lint-release-manifest.json`:

```sh
python3 auto_lint_pr.py \
  --lint-root /absolute/path/to/lint \
  --repository owner/repository
```

The default formats all supported files in Docker write
mode. Use `--local` only when the exact local formatter
toolchain is already provisioned. Selection can instead use
`--modified`, positional paths, or `--files-from0`. Repeat
`--language` to restrict formatter families.

Use `--hook '<command>'` to run a consumer generator or
check after formatting. The hook and formatter share the
token-free environment. Their combined exact delta is the
only content eligible for the pull request.

`python3 auto_lint_pr.py` defaults to `prepare`. Local
operators can run the explicit verification and publication
phases as separate commands:

```sh
python3 auto_lint_pr.py prepare \
  --lint-root /absolute/path/to/lint
python3 auto_lint_pr.py verify \
  --restore \
  --state /absolute/path/to/state.json \
  --verification /absolute/path/to/verified.json
python3 auto_lint_pr.py publish \
  --state /absolute/path/to/state.json \
  --verification /absolute/path/to/verified.json
```

`prepare` writes a state record below `.git` by default.
`verify --restore` materializes that record into a clean
checkout and writes a receipt. `publish` fails if the state,
receipt, base checkout, or prepared bytes and modes differ.
The command-line phases assume a trusted local operator; use
the reusable workflow for adversarial job isolation.

## Composite action

The composite action exposes `phase: prepare` and
`phase: publish`. Run those phases in different jobs. The
prepare job has read-only permissions and runs the formatter
and optional hook. The publish job starts from fresh
checkouts, downloads the state artifact, and invokes the
publish phase:

```yaml
# Read-only prepare job, after pinned checkouts.
- uses: ./dependencies/auto-lint-pr
  with:
    phase: prepare
    lint-root: dependencies/lint
    cwd: workspace
    state-path: ${{ runner.temp }}/auto-lint-state.json
    verification-path: ${{ runner.temp }}/verified.json

# Fresh publish job, after downloading the state artifact.
- uses: ./dependencies/auto-lint-pr
  with:
    phase: publish
    token: ${{ github.token }}
    cwd: workspace
    state-path: ${{ runner.temp }}/auto-lint-state.json
    verification-path: ${{ runner.temp }}/verified.json
```

The action exposes `docker`, `modified`, `paths`,
`files-from0`, `languages`, `hook`, `labels`, `reviewers`,
`title`, and `body` inputs. The reusable workflow at
`.github/workflows/auto-lint-pr.yml` supplies the complete
two-job artifact bridge, fresh checkouts, its permission
ceiling, and per-repository/base concurrency. Its optional
`checkout_token` secret is used only to read private
dependency repositories. Publication always uses the calling
repository's `github.token`.

## Reusable workflow

Before the first run, turn on the repository or organization
Actions setting **Allow GitHub Actions to create and approve
pull requests**. Token permissions do not change that
independent setting.

The calling job must grant the workflow's write permissions
because a reusable workflow cannot elevate the caller's
token. `issues: write` is required for the optional labels
input. Pin the signed release tag shown below:

```yaml
permissions:
  contents: read

jobs:
  auto-lint:
    permissions:
      contents: write
      issues: write
      pull-requests: write
    uses: >-
      trycopilotai/auto-lint-pr/.github/workflows/auto-lint-pr.yml@v0.1.0
```

The called jobs check out this repository at
`job.workflow_sha`, so the local composite action and the
reusable workflow come from the same pinned commit. The
formatter job cannot carry command-file or action-checkout
changes into the fresh publisher job.

## Comparison

Reviewed 2026-07-31 against the official
[`peter-evans/create-pull-request` documentation](https://github.com/peter-evans/create-pull-request/blob/7ec5aae3c91d101b005af46adc760d265911886a/README.md).
Both tools create or update pull requests from repository
changes, but they own different transaction boundaries.

| Boundary        | `auto-lint-pr`                                                                                                                | `peter-evans/create-pull-request`                                                                               |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Change producer | Runs pinned lint and an optional hook in a token-free prepare job.                                                            | Consumes changes already present in the Actions workspace.                                                      |
| Selected delta  | Records and restores the exact regular-file delta produced during prepare.                                                    | Adds all new and modified files by default; `add-paths` can restrict paths.                                     |
| Credentials     | Gives the write token only to the trusted publish substep after fresh-job verification.                                       | Uses `token` for pull request operations and `branch-token` for branch updates; both default to `GITHUB_TOKEN`. |
| Existing branch | Requires one matching open pull request, audits branch-only commits and paths, verifies a staging commit, then fast-forwards. | Creates or updates the configured pull request branch.                                                          |

Choose based on which boundary the workflow needs. This
table does not claim that one tool is a drop-in replacement
for the other.

## Safety boundaries

- The action does not use `pull_request_target`.
- Every checkout sets `persist-credentials: false`.
- A private dependency checkout token never enters the
  publication environment.
- Formatting and the optional hook do not receive GitHub
  tokens, Actions runtime tokens, or runner command-file
  paths.
- Formatting and the optional hook run in a read-only job.
  Publication runs in a fresh job with fresh action and
  consumer checkouts.
- Before token injection, the fresh job restores the
  recorded regular-file delta and binds its exact state,
  bytes, modes, base commit, and checkout to a verification
  receipt.
- Publishing uses GitHub's expected-head commit mutation
  with the exact bytes recorded during token-free
  preparation.
- Additions and modifications are limited to regular
  `100644` files, the mode represented by that mutation.
  Deletions are limited to the same regular-file mode.
- Every published commit must be authored by the Actions bot
  and carry a valid GitHub-generated signature.
- An existing pull-request branch is updated only after its
  candidate commit passes on a transaction-specific staging
  branch. Promotion is a non-forced fast-forward, and the
  staging branch is then removed.
- Existing branches are reused only when their matching pull
  request belongs to the same repository, every branch-only
  commit passes those provenance checks, and their changed
  paths exactly match the prepared delta.
- The source checkout must be clean before preparation.
- A formatter or hook failure stops before branch or pull
  request operations.
- The lint checkout must be clean and its commit must match
  the vendored release manifest. Tracked, untracked, and
  ignored residue are all rejected.

This action intentionally needs `contents: write` and
`pull-requests: write` during publication, plus
`issues: write` when labels are requested. Review consumer
hooks as repository code because their output can become
part of the formatting pull request.

## Verification

```sh
make verify
```

This runs the CLI, transaction, action-adapter, metadata,
and repository-invariant tests without GitHub credentials.

The technical draft
[Publishing formatter output without formatter credentials](docs/exact-delta-boundary.md)
walks through the prepare, verify, and publish boundary.

## Contribution paths and launch metric

Issues labeled `good first issue` are bounded entry points.
Use `transaction-boundary` for token, delta, branch, or pull
request invariants and `consumer-integration` for reusable
workflow, hook, or metadata work.

Launch success is one external repository completing the
prepare-to-publish transaction and opening a pull request
whose changed paths equal the verified state receipt. Stars
are not part of that metric.

## Claude Code

After public launch, install the pinned standalone skill
without authentication:

```sh
release=v0.1.0
archive="$(mktemp -d)"
target="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/auto-lint-pr"
install -d "$target"
curl --fail --location \
  "https://github.com/trycopilotai/auto-lint-pr/archive/refs/tags/$release.tar.gz" \
  >"$archive/auto-lint-pr.tar.gz"
tar -xzf "$archive/auto-lint-pr.tar.gz" \
  --strip-components=1 \
  -C "$target"
cp "$target/skills/auto-lint-pr/SKILL.md" \
  "$target/SKILL.md"
```

The standalone invocation is `/auto-lint-pr`. A Claude
marketplace distribution uses `/auto-lint-pr:auto-lint-pr`.
Marketplace registration and marketplace install
verification are deferred until a separately approved public
launch; the block above is the standalone installation path.

## Codex

After public launch, install the same pinned skill into the
Codex skill store without authentication:

```sh
release=v0.1.0
archive="$(mktemp -d)"
target="${CODEX_HOME:-$HOME/.codex}/skills/auto-lint-pr"
install -d "$target"
curl --fail --location \
  "https://github.com/trycopilotai/auto-lint-pr/archive/refs/tags/$release.tar.gz" \
  >"$archive/auto-lint-pr.tar.gz"
tar -xzf "$archive/auto-lint-pr.tar.gz" \
  --strip-components=1 \
  -C "$target"
cp "$target/skills/auto-lint-pr/SKILL.md" \
  "$target/SKILL.md"
```

Invoke it as `$auto-lint-pr`.

Codex marketplace registration and marketplace install
verification are also deferred until that separately
approved public launch.

## License

MIT
