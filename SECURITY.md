# Security policy

## Reporting a vulnerability

Use GitHub private vulnerability reporting for reports that
could affect credentials, branch ownership, pull request
reuse, dependency verification, or exact-delta enforcement.
Do not disclose an unresolved vulnerability in a public
issue.

Include the affected revision, the shortest reproduction
available, and the expected security boundary.

Maintainers use `transaction-boundary` for non-sensitive
hardening work and `consumer-integration` for non-sensitive
consumer setup problems. The `good first issue` label never
marks an unresolved vulnerability.

## Dependency trust boundary

The formatter checkout cannot nominate its own trust root.
`lint-dependency.json` binds the expected annotated tag
object, commit, tree, release-manifest checksum, and the
checksum and principal of the controller-held
allowed-signers file. Preparation verifies the operator
signature, reproduces the source archive checksum from the
signed commit, rejects executable repository-local Git
configuration, and compares checkout bytes and modes without
trusting index flags. It validates the exact tool and
language-image mappings before running lint from an isolated
materialization of the authenticated commit.

Docker formatting uses only the image names and SHA-256
digests authenticated by that manifest. A version tag is not
accepted as a runtime image reference. The reusable workflow
uses a package-read credential only to prefetch those exact
references with an isolated Docker configuration, then logs
out and removes that configuration before the token-free
formatter substep.

## Supported versions

Security fixes target the latest tagged release and the
default branch.
