# auto-lint-pr

Turn pinned
[`trycopilotai/lint`](https://github.com/trycopilotai/lint)
formatter output into a reviewable pull request without
granting the formatter step a write token.

The transaction has two phases:

1. A token-free phase verifies the pinned lint release,
   applies formatting, optionally runs one consumer command,
   and records the exact file delta.
2. A publish phase receives the token, re-verifies that
   delta, creates a bot-authored commit, and creates or
   updates the matching pull request.

The tool refuses to reuse a remote branch unless there is
exactly one matching open pull request, its base and head
branches match, its reported head commit matches the remote
tip, and that tip is authored by `github-actions[bot]`.

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

The explicit phases are also available:

```sh
python3 auto_lint_pr.py prepare \
  --lint-root /absolute/path/to/lint
python3 auto_lint_pr.py publish \
  --lint-root /absolute/path/to/lint
```

`prepare` writes a state record below `.git` by default.
`publish` fails if the base checkout or prepared file bytes
have changed.

## Composite action

Check out this repository and the pinned lint release
outside the consumer repository, then invoke the local
action:

```yaml
- uses: actions/checkout@<full-commit-sha>
  with:
    persist-credentials: false
    path: workspace
- uses: actions/checkout@<full-commit-sha>
  with:
    repository: trycopilotai/lint
    ref: <lint-commit>
    persist-credentials: false
    path: dependencies/lint
- uses: actions/checkout@<full-commit-sha>
  with:
    repository: trycopilotai/auto-lint-pr
    ref: <auto-lint-pr-commit>
    persist-credentials: false
    path: dependencies/auto-lint-pr
- uses: ./dependencies/auto-lint-pr
  with:
    token: ${{ secrets.GITHUB_TOKEN }}
    lint-root: dependencies/lint
    cwd: workspace
```

The action exposes `docker`, `modified`, `paths`,
`files-from0`, `languages`, `hook`, `labels`, `reviewers`,
`title`, and `body` inputs. The reusable workflow at
`.github/workflows/auto-lint-pr.yml` supplies checkout,
permissions, and per-repository/base concurrency. Its
optional `checkout_token` secret is used only to read
private dependency repositories. Publication always uses the
calling repository's `github.token`.

## Safety boundaries

- The action does not use `pull_request_target`.
- Every checkout sets `persist-credentials: false`.
- A private dependency checkout token never enters the
  publication environment.
- Formatting and the optional hook do not receive GitHub
  tokens or Actions runtime tokens.
- Publishing uses GitHub's expected-head commit mutation
  with the exact bytes recorded during token-free
  preparation.
- Every published commit must be authored by the Actions bot
  and carry a valid GitHub-generated signature.
- Existing branches are reused only when their matching pull
  request belongs to the same repository, every branch-only
  commit passes those provenance checks, and their changed
  paths exactly match the prepared delta.
- The source checkout must be clean before preparation.
- A formatter or hook failure stops before branch or pull
  request operations.
- The lint checkout must be clean and its commit must match
  the vendored release manifest.

This action intentionally needs `contents: write` and
`pull-requests: write` during publication. Review consumer
hooks as repository code because their output can become
part of the formatting pull request.

## Verification

```sh
make verify
```

This runs the CLI, transaction, action-adapter, metadata,
and repository-invariant tests without GitHub credentials.

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

## License

MIT
