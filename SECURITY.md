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
signed tag, and validates the exact tool and language-image
mappings before importing or running lint code.

Docker formatting uses only the image names and SHA-256
digests authenticated by that manifest. A version tag is not
accepted as a runtime image reference.

## Supported versions

Security fixes target the latest tagged release and the
default branch.
