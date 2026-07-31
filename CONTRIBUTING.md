# Contributing

Contributions should preserve the two-phase token boundary
and the exact-delta publication contract.

## Development

Use Python 3.11 or newer and run:

```sh
make verify
```

Python is formatted with Black 24.10.0. Markdown, YAML, and
JSON use Prettier 3.7.4 with a print width of 60, prose
wrapping always, and trailing commas off.

Behavior changes require tests. Security-sensitive changes
should cover both the permitted path and the refusal path.
Do not add a force push, `pull_request_target`, persistent
checkout credentials, or credentials to the prepare phase.

Issues labeled `good first issue` have a bounded change and
verification path. Use `transaction-boundary` for token,
delta, branch, and pull request invariants. Use
`consumer-integration` for reusable workflow, hook, or
metadata changes.
