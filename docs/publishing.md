# Publishing checklist

This checklist separates private release verification from
later public launch operations.

## Private release verification

- Verify the signed tag and exact commit.
- Run the complete repository check.
- Create the deterministic source archive and checksums.
- Keep the GitHub Release in draft state.
- Keep repository and package visibility private.
- Confirm that no `latest` alias exists.

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
