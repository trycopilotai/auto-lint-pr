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
runtime credentials or runner command-file paths. Run
prepare in a read-only job. Run publish in a fresh job with
fresh checkouts after transferring only the prepared state
artifact. Its first token-free substep restores and verifies
the exact delta; its next substep may receive the write
token.

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
  issues: write
  pull-requests: write
```

Place these permissions on the calling job. A reusable
workflow cannot elevate the caller's token permissions.
`issues: write` is required when labels are requested.

Before the first run, confirm the repository or organization
Actions setting **Allow GitHub Actions to create and approve
pull requests** is on. Token permissions do not change this
independent setting.

Set `persist-credentials: false` on every checkout. Use
concurrency keyed by repository and base branch. Do not use
`pull_request_target`.

Update an existing branch only through an expected-head
commit mutation that atomically compares its audited tip.
Retain the branch after an ambiguous mutation result so a
later run can audit and reconcile it; do not delete it with
a separate check-then-delete sequence.

Use a separate read-only checkout credential when the
dependencies are private. Never pass that credential to the
composite action; publication must use the consumer
repository's `github.token` so the resulting commit is
authored by `github-actions[bot]`.

Do not place the prepare and publish phases in one job. Do
not replace the cross-job state artifact with `GITHUB_ENV`,
`GITHUB_PATH`, a workspace action checkout, or another
formatter-writable channel.

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
