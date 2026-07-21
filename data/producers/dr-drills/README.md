# DR drill evidence envelopes

This directory is the pushed-artifact landing zone for terraform disaster-recovery
drill evidence (honua-io/honua-evidence#8). honua-terraform owns DR by design
(the server itself is stateless) and is expected to commit one JSON envelope
per drill run here — via `capture-dr-drill-evidence.sh` or an operator's manual
run of the backup-restore / failover runbooks.

**This directory starts empty on purpose.** `scripts/aggregate.py` reads every
`*.json` file here; if none exist yet, the `dr-drills` producer honestly
reports freshness status `missing` — never a fabricated pass. Nothing here is
a template to copy-paste as fake evidence.

See [`docs/producer-contracts.md`](../../../docs/producer-contracts.md) for
the full envelope schema, an example, and the freshness semantics. Test
fixtures (including a deliberately malformed envelope, to exercise the
warn-not-crash path) live under
[`tests/fixtures/dr-drills/`](../../../tests/fixtures/dr-drills/).
