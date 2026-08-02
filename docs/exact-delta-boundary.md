# Publishing formatter output without formatter credentials

The difficult part of automated formatting is not running a
formatter. It is proving that the process holding a
repository write token publishes only the bytes the
formatter produced.

`auto-lint-pr` treats that proof as a transaction with two
jobs. The first job prepares a delta without publication
credentials. The second begins from fresh trusted checkouts.
Its verifier subprocess clears token variables, restores the
recorded delta, and verifies it before the following
publication substep receives the job-scoped token as an
explicit input.

This is a draft about that boundary. It is not a release
announcement.

## Prepare records an object, not an intention

The prepare phase checks that the consumer and pinned lint
checkouts are clean. It records the consumer base commit,
the lint commit, the pull request branch, and every changed
regular file. Each addition or modification includes its
path, mode, and exact bytes. A deletion records the path and
the supported prior mode.

An optional consumer hook runs in the same token-free
environment as the formatter. Its output can join the delta,
but it cannot gain a broader publication channel. The state
file is therefore a description of the complete proposed
commit rather than a request to run arbitrary code later.

The prepare process also removes GitHub tokens, Actions
runtime tokens, and runner command-file paths from formatter
and hook child environments. That removal is enforced in
code and refusal-path tests; it is not delegated to workflow
convention.

## Verify reconstructs the boundary

The publish job does not reuse the formatter workspace. It
starts from fresh action and consumer checkouts, downloads
the state artifact, and runs the restore-and-verify phase in
a subprocess whose GitHub token variables are cleared. The
trusted checkout actions may use the job-scoped token, but
do not persist it.

Verification binds the state record to the expected base
checkout, recreates the regular-file delta, and writes a
receipt for the restored bytes and modes. Publication
refuses a stale base, a changed state file, a changed
receipt, unsupported file kinds or modes, and residue
outside the prepared paths.

The workflow passes the job-scoped token explicitly only to
the following composite action substep. That credentialed
process consumes the already verified state and receipt. It
does not rerun the formatter or the hook.

## Publish uses the verified bytes

For a new branch, publication creates the branch at the
recorded base and uses GitHub's expected-head commit
mutation with the exact prepared additions and deletions. It
then reads the resulting commit and tree back from GitHub,
checks bot authorship and the GitHub-generated signature,
and confirms that the branch tip is the new commit before
creating the pull request.

Updating an existing branch adds another boundary. The tool
accepts only one matching open pull request whose base, head
repository, head branch, and reported head commit agree with
the remote ref. It audits the branch-only commits and
changed paths. GitHub then appends the exact replacement
delta only if its expected-head commit still matches the
branch tip.

Ambiguous mutation failures retain the publication branch so
a later run can audit and reconcile it. The transaction does
not attempt a race-prone automatic branch deletion.

If the existing branch already contains the exact prepared
delta, a retry does not create another commit. It still
reapplies requested labels and reviewers so a metadata
failure does not become permanent after the content
transaction succeeds.

## What the boundary does not claim

The optional hook remains trusted repository code because
its bytes are eligible for publication. The publisher also
needs repository write permissions. The boundary narrows
when and where those permissions exist; it does not make
formatter or hook behavior intrinsically trustworthy.

The executable contract lives in
[`auto_lint_pr.py`](../auto_lint_pr.py),
[`action.yml`](../action.yml), and the
[`auto-lint-pr.yml`](../.github/workflows/auto-lint-pr.yml)
reusable workflow. The refusal paths and branch transaction
are covered by
[`tests/test_auto_lint_pr.py`](../tests/test_auto_lint_pr.py).
Run `make verify` to re-derive the repository checks.
