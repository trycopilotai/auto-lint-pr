# Publishing checklist

This checklist separates the release verification that runs
on every candidate from the launch operations that have not
been performed.

## Release verification

- Verify the signed tag and exact commit against the signer
  policy pinned by immutable commit in the release workflow;
  never read the trust policy from the release tag itself.
- Run the complete repository check.
- Create the deterministic source archive and checksums.
- Keep the GitHub Release in draft state.
- Confirm that no `latest` alias exists.
- Confirm prepare, verify, and publish use separate
  permission boundaries, with the state artifact as their
  only formatter-controlled bridge.
- Confirm the read-only verifier packages checksum-bound
  publication inputs and the write-scoped job performs no
  network checkout.
- Confirm the write-scoped job validates and reverifies the
  package before its credentialed project substep.
- Confirm the target repository's Actions setting permits
  GitHub Actions to create pull requests.

Rerun GitHub-hosted artifact attestations against the public
repository; attestations produced before publication do not
carry the published repository's identity.

## Not performed

The following operations have not been performed and each
requires a separate approval:

- rerun release attestations;
- publish the draft GitHub Release;
- register marketplace entries;
- configure the social preview;
- announce the release.

Repeat the complete disclosure scan against pushed history
and inspect the repository as an anonymous recipient before
each of the operations above.
