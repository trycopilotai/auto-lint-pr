# Publishing checklist

This checklist separates the release verification that runs
on every candidate from the launch operations that have not
been performed.

## Release verification

- Verify the signed tag and exact commit against the signer
  policy pinned by immutable commit in the release workflow;
  never read the trust policy from the release tag itself.
- Run the complete repository check.
- Confirm the end-to-end verification path is green: the CI
  `pinned-lint` and `pinned-lint-docker` fixture jobs run
  the token-free transaction on every push, and a private
  consumer repository used as an end-to-end harness runs the
  complete prepare-to-publish transaction against GitHub.
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

Publication targets github.com only: `require_token` in
`auto_lint_pr.py` hardcodes `GH_HOST=github.com`, and the
reusable workflow's identity fields are documented for
GitHub Cloud, so GitHub Enterprise Server is unsupported
end-to-end.

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
