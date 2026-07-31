---
name: auto-lint-pr
description: >-
  Prepare or operate an on-demand GitHub Actions workflow
  that turns pinned trycopilotai/lint output into a
  reviewable pull request while keeping the formatter and
  consumer hook token-free. Use when the user asks to add,
  inspect, or run automated formatting pull requests.
---

# auto-lint-pr

Use this skill for deliberate formatting pull requests, not
routine read-only lint checks.

## Inspect the consumer

Before changing a workflow, identify:

- the base branch;
- the exact lint and auto-lint-pr revisions;
- whether selection is all files, modified files, explicit
  paths, or a NUL-delimited path list;
- repository-only generators or checks that belong in the
  token-free hook;
- existing formatting labels and reviewer policy.

Do not move private checks into the public lint dependency.

## Preserve the transaction

Keep the formatter and optional hook in the prepare phase.
They must not receive `GITHUB_TOKEN`, `GH_TOKEN`, or Actions
runtime credentials. The publish phase may receive the write
token only after the exact delta has been recorded.

Use a local checkout of the composite action when direct
cross-repository workflow references are not approved. Check
out dependencies outside the consumer repository so they
cannot enter the prepared delta.

## Configure the workflow

Use `workflow_dispatch` for an on-demand consumer workflow.
Grant only:

```yaml
permissions:
  contents: write
  pull-requests: write
```

Set `persist-credentials: false` on every checkout. Use
concurrency keyed by repository and base branch. Do not use
`pull_request_target`.

Use a separate read-only checkout credential when the
dependencies are private. Never pass that credential to the
composite action; publication must use the consumer
repository's `github.token` so the resulting commit is
authored by `github-actions[bot]`.

## Verify

Run the consumer's exact repository verification command.
Confirm:

- no-change exits without a branch or pull request;
- formatter or hook failure stops before publication;
- the prepared paths equal the committed paths;
- an unrelated or human-authored remote branch is refused;
- routine lint remains read-only.

Report the workflow path, pinned revisions, selection, hook,
and exact verification command.
