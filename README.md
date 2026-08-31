<img src="assets/icon.svg" alt="" width="96" align="right">

# auto-lint-pr

[![CI](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/ci.yml/badge.svg)](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/ci.yml)
[![Release](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/release.yml/badge.svg)](https://github.com/trycopilotai/auto-lint-pr/actions/workflows/release.yml)

Turn pinned
[`trycopilotai/lint`](https://github.com/trycopilotai/lint)
formatter output into a reviewable pull request without
granting the formatter step a write token.

The transaction uses three jobs with narrow credential
boundaries:

1. A read-only prepare job verifies the controller-bound
   lint dependency ledger and resolves the complete signed
   image digest set. When Docker is active, one isolated
   substep logs into GHCR, prefetches only those exact
   `image@sha256:...` references, logs out, and removes its
   Docker configuration. The following token-free substep
   re-verifies the signed tag, commit, tree, and
   reproducible archive checksum, executes lint from a
   materialized copy of that exact commit, optionally runs
   one consumer command, and records the exact file delta.
2. A fresh read-only job restores and verifies that delta,
   then packages the exact base commit and pinned action
   source with path-bound receipts and checksums.
3. A write-scoped job downloads only that immutable package,
   validates its checksums, reconstructs the base without a
   network checkout, and re-verifies with token variables
   cleared. Only the following project publication substep
   gets the write token as an explicit input.

The tool tolerates at most one matching open pull request.
When one exists, its base, head repository, head branch, and
reported head commit must match the remote ref. Reusing any
existing branch also requires every branch-only commit to be
authored and committed by `github-actions[bot]` with its
documented bot email and a valid GitHub-generated signature.

<picture>
  <source media="(prefers-reduced-motion: reduce)"
    srcset="assets/poster.svg">
  <img src="assets/demo.svg"
    alt="Reconstructed token-free prepare demo">
</picture>

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
`--modified` or `--files-from0`. Repeat `--language` to
restrict formatter families. Positional paths require the
explicit `prepare` phase so they are not parsed as a phase
name:

```sh
python3 auto_lint_pr.py prepare \
  --lint-root /absolute/path/to/lint \
  src/example.py
```

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
the reusable workflow for adversarial job isolation. Release
verification rejects executable repository-local Git
configuration and compares actual checkout bytes and modes
to the signed commit without trusting index flags.

## Composite action

The composite action exposes `phase: prepare`,
`phase: verify`, and `phase: publish`. Run those phases in
separate jobs. Prepare runs the formatter and optional hook
with read-only permissions. Verify restores the state in a
fresh read-only checkout and packages the exact base and
action source. Publish reconstructs those inputs, reverifies
them, and then supplies the write token:

```yaml
# Read-only prepare job, after pinned checkouts.
- uses: ./dependencies/auto-lint-pr
  with:
    phase: prepare
    lint-root: dependencies/lint
    cwd: workspace
    workspace-root: workspace
    state-path: ${{ runner.temp }}/auto-lint-state.json
    verification-path: ${{ runner.temp }}/verified.json

# Fresh read-only verification job.
- uses: ./dependencies/auto-lint-pr
  with:
    phase: verify
    cwd: workspace
    workspace-root: workspace
    state-path: ${{ runner.temp }}/auto-lint-state.json
    verification-path: ${{ runner.temp }}/verified.json

# Write-scoped publication job, after package validation.
- uses: ./dependencies/auto-lint-pr
  with:
    phase: publish
    token: ${{ github.token }}
    cwd: workspace
    workspace-root: workspace
    state-path: ${{ runner.temp }}/auto-lint-state.json
    verification-path: ${{ runner.temp }}/verified.json
```

The action exposes `workspace-root`, `docker`, `modified`,
`paths`, `files-from0`, `languages`, `hook`, `labels`,
`reviewers`, `title`, and `body` inputs. Action phases
reject a `cwd` that resolves outside `workspace-root`. The
reusable workflow at `.github/workflows/auto-lint-pr.yml`
supplies the complete three-job artifact bridge, fresh
read-only checkouts, its permission ceiling, and
per-repository/base concurrency. Its optional
`checkout_token` secret is used only to read private
dependency repositories. Its optional `registry_token`
secret must have package-read access and is used only for
the isolated GHCR prefetch. Publication always uses the
calling repository's `github.token`.

## Reusable workflow

Before the first run, turn on the repository or organization
Actions setting **Allow GitHub Actions to create and approve
pull requests**. Token permissions do not change that
independent setting.

The calling job must grant the workflow's write permissions
because a reusable workflow cannot elevate the caller's
token. `issues: write` is required for the optional labels
input. `packages: read` is also required because the prepare
job prefetches the pinned formatter images, and a calling
job that lists any permissions gets `none` for every
unlisted scope. Pin the signed release tag shown below:

```yaml
permissions:
  contents: read

jobs:
  auto-lint:
    permissions:
      contents: write
      issues: write
      packages: read
      pull-requests: write
    uses: >-
      trycopilotai/auto-lint-pr/.github/workflows/auto-lint-pr.yml@v0.1.1
```

The called jobs check out this repository at
`job.workflow_sha`, so the local composite action and the
reusable workflow come from the same pinned commit. The
formatter job cannot carry command-file or action-checkout
changes into the fresh verifier. The write-scoped job has no
network checkout and accepts only the checksum-bound package
from that verifier. These workflow identity fields are
documented for GitHub Cloud and are not available on GitHub
Enterprise Server. The publish phase also hardcodes
`GH_HOST=github.com` when it accepts the write token
(`require_token` in `auto_lint_pr.py`). Those are two
independent GitHub Cloud dependencies, so GitHub Enterprise
Server is unsupported end-to-end.

### Post-format hook example

The optional `hook` input runs one consumer command in the
prepare job after formatting. A repository whose generated
files must track formatted sources can regenerate them in
the same transaction, so the formatter delta and the
regenerated files land in one pull request. Add a `with:`
block to the pinned reusable-workflow call shown above:

```yaml
with:
  hook: "python3 tools/build_index.py"
```

The hook shares the formatter's token-free child
environment. `GITHUB_TOKEN`, `GH_TOKEN`,
`ACTIONS_ID_TOKEN_REQUEST_TOKEN`, and
`ACTIONS_RUNTIME_TOKEN` are removed before it starts, along
with the runner command-file paths, so a hook that needs a
credential cannot run in the prepare phase. The write token
exists only in the publish job, and that job does not rerun
the formatter or the hook. This repository's CI pins the
contract with a fixture hook that fails unless every one of
those token variables is unset (`.github/workflows/ci.yml`).

Files the hook rewrites join the recorded delta exactly like
formatter output. The hook must leave its changes
uncommitted: prepare fails when the base checkout `HEAD`
moves during token-free preparation, so a hook that commits
stops the transaction.

A non-zero hook exit fails the prepare job before any branch
or pull request operation, so a generator or check that
rejects its input stops the run with no publication. Review
the hook as repository code because its output can become
part of the formatting pull request.

### Docker-mode prerequisites

The default `docker: true` mode formats with the pinned
language images, so the runner must provide a Docker CLI and
daemon at `/usr/bin/docker`; the prefetch step fails when it
is absent. A runner without Docker must pass `docker: false`
and provision the exact local formatter toolchain instead.

The `ghcr.io/trycopilotai/lint-<language>` images are public
and anonymously pullable, so the `registry_token` secret is
needed only for private forks of those images. The prefetch
step pulls only the exact `image@sha256:...` references
recorded in `lint-release-manifest.json`.

## Comparison

Reviewed 2026-08-02 against the official
[`peter-evans/create-pull-request` documentation](https://github.com/peter-evans/create-pull-request/blob/11fa467881691ac900904a2eea702c5ea848ad13/README.md).
Both tools create or update pull requests from repository
changes, but they own different transaction boundaries.

| Boundary        | `auto-lint-pr`                                                                                                                                           | `peter-evans/create-pull-request`                                                                               |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Change producer | Runs pinned lint and an optional hook in a token-free prepare job.                                                                                       | Consumes changes already present in the Actions workspace.                                                      |
| Selected delta  | Records and restores the exact regular-file delta produced during prepare.                                                                               | Adds all new and modified files by default; `add-paths` can restrict paths.                                     |
| Credentials     | Keeps formatter and packaging jobs read-only; clears token variables during final verification; passes the write token only to project publication code. | Uses `token` for pull request operations and `branch-token` for branch updates; both default to `GITHUB_TOKEN`. |
| Existing branch | Audits branch-only commits and paths, then atomically appends the exact delta only while the expected head still matches.                                | Creates or updates the configured pull request branch.                                                          |

Choose based on which boundary the workflow needs. This
table does not claim that one tool is a drop-in replacement
for the other.

## Safety boundaries

- The action does not use `pull_request_target`.
- Every checkout sets `persist-credentials: false`.
- A private dependency checkout token is never persisted or
  passed to the composite action or publication substep.
- A private registry token is passed only to the
  exact-digest prefetch substep. That substep uses an
  isolated Docker configuration, logs out, and removes the
  configuration before formatting starts.
- Formatting and the optional hook do not receive GitHub
  tokens, Actions runtime tokens, or runner command-file
  paths.
- Formatting and the optional hook run in a read-only job.
  Verification runs in a second read-only job with fresh
  action and consumer checkouts.
- The write-scoped job performs no network checkout. It
  verifies the read-only job's checksums, reconstructs the
  exact base and pinned action source, then restores the
  recorded delta in a subprocess with token variables
  cleared.
- Publishing uses GitHub's expected-head commit mutation
  with the exact bytes recorded during token-free
  preparation.
- The remote base ref is checked against the prepared commit
  again immediately before pull request creation or metadata
  updates.
- Additions and modifications are limited to regular
  `100644` files, the mode represented by that mutation.
  Deletions are limited to the same regular-file mode.
- Every published commit must be authored and committed by
  the Actions bot using its documented bot identity and must
  carry a valid GitHub-generated signature.
- An existing pull-request branch is updated by the same
  expected-head commit mutation. A concurrent ref change
  makes that mutation fail.
- Ambiguous mutation failures retain the publication branch
  for an audited retry. The action never performs a
  race-prone automatic branch deletion.
- Existing branches are reused only when every branch-only
  commit passes those provenance checks and their changed
  paths exactly match the prepared delta. At most one
  matching pull request may exist; when present, its base,
  head repository, head branch, and reported tip must match.
- The source checkout must be clean before preparation.
- A formatter or hook failure stops before branch or pull
  request operations.
- The lint checkout must exactly match the signed commit.
  Its annotated tag, operator signature, commit, tree,
  reproducible source archive, tool map, and complete
  language-image digest set must match
  `lint-dependency.json` and `lint-release-manifest.json`.
  The trusted signer input is held by this controller rather
  than accepted from the lint checkout. Actual bytes, modes,
  tracked paths, untracked paths, and ignored residue are
  checked independently of Git index flags.
- Lint executes from an isolated materialization of the
  authenticated commit rather than from mutable checkout
  files.
- Default Docker execution passes the verified release
  manifest to lint and runs only exact
  `ghcr.io/trycopilotai/lint-<language>@sha256:...`
  references. Version tags are not runtime authority.

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

Install the pinned standalone skill without authentication:

```sh
release=v0.1.1
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
verification have not been performed; the block above is the
standalone installation path.

## Codex

Install the same pinned skill into the Codex skill store
without authentication:

```sh
release=v0.1.1
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
verification have not been performed either.

## License

MIT
