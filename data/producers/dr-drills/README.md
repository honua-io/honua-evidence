# DR drill evidence envelopes

This directory is the pushed-artifact landing zone for terraform disaster-recovery
drill evidence (honua-io/honua-evidence#8). honua-iac (formerly honua-terraform) owns DR by design
(the server itself is stateless) and is expected to commit one JSON envelope
per drill run here — via `capture-dr-drill-evidence.sh` or an operator's manual
run of the backup-restore / failover runbooks.

**This directory starts empty on purpose.** `scripts/aggregate.py` reads every
`*.json` file here; nothing here is a template to copy-paste as fake evidence.

Because no drill has ever pushed an envelope, `dr-drills` carries **no
freshness ledger row** at all (honua-io/honua-release#89): it is declared in
`capability-matrix.v1.json`'s `awaitingFirstEnvelope` array instead. A
`missing` row that has always been missing teaches nothing and dilutes
`missing` for the case that matters — a producer that used to report and
stopped. The row returns automatically, with no aggregator change, on the first
envelope committed here. Producer work is tracked in
[honua-io/honua-evidence#20](https://github.com/honua-io/honua-evidence/issues/20).

See [`docs/producer-contracts.md`](../../../docs/producer-contracts.md) for
the full envelope schema, an example, and the freshness semantics. Test
fixtures (including a deliberately malformed envelope, to exercise the
warn-not-crash path) live under
[`tests/fixtures/dr-drills/`](../../../tests/fixtures/dr-drills/).
