# Live canary / cloud e2e evidence envelopes

This directory is the pushed-artifact landing zone for live/deployed-environment
canary and cloud e2e probe results (honua-io/honua-evidence#8). honua-release's
scheduled demo canary and cloud e2e workflows (honua-io/honua-release#61) are
expected to commit one JSON manifest envelope per run here.

The scheduled demo canary now commits a versioned envelope after each live run.
`scripts/aggregate.py` selects the newest valid envelope and reports its
freshness without converting missing, stale, partial, or failed evidence into a
fabricated pass. Files in this directory are run receipts, not templates for
hand-authored evidence.

See [`docs/producer-contracts.md`](../../../docs/producer-contracts.md) for
the full envelope schema, an example, and the freshness semantics. Test
fixtures (including a deliberately malformed envelope, to exercise the
warn-not-crash path) live under
[`tests/fixtures/live-canary/`](../../../tests/fixtures/live-canary/).
