# Publishing checklist

This checklist separates private release verification from
later public launch operations.

## Private release verification

- Verify the signed tag and exact commit against the signer
  policy pinned by immutable commit in the release workflow;
  never read the trust policy from the release tag itself.
- Run the complete repository check.
- Create the deterministic source archive and checksums.
- Keep the GitHub Release in draft state.
- Keep repository and package visibility private.
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

GitHub-hosted artifact attestations are rerun after public
visibility because the private repository plan does not
support that service surface.

## Deferred public launch

The following operations require a separate approval:

- change repository visibility;
- change package visibility;
- rerun release attestations;
- publish the draft GitHub Release;
- register marketplace entries;
- configure the social preview;
- announce the release.

Before any visibility change, repeat the complete disclosure
scan against pushed history and inspect the repository as an
anonymous recipient.
