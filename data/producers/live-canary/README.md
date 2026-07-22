# Live canary / cloud e2e evidence envelopes

This directory is the pushed-artifact landing zone for live/deployed-environment
canary and cloud e2e probe results (honua-io/honua-evidence#8). honua-release's
scheduled demo canary and cloud e2e workflows (honua-io/honua-release#61) are
expected to commit one JSON manifest envelope per run here.

**This directory starts empty on purpose.** As of this ingestion landing,
honua-release#61's scheduled canary workflow does not exist yet, so
`scripts/aggregate.py` correctly reports the `live-canary` producer's freshness
status as `missing` — never a fabricated pass. Nothing here is a template to
copy-paste as fake evidence.

See [`docs/producer-contracts.md`](../../../docs/producer-contracts.md) for
the full envelope schema, an example, and the freshness semantics. Test
fixtures (including a deliberately malformed envelope, to exercise the
warn-not-crash path) live under
[`tests/fixtures/live-canary/`](../../../tests/fixtures/live-canary/).
