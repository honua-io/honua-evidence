# Protocol certification producer fragments

Evidence producers may commit immutable `*.json` fragments below this directory. The daily aggregator also fetches
registered Actions artifacts into the non-committed `fetched/` subtree using
`config/protocol-certification-producers.v1.json`. The contract is documented in
[`docs/protocol-certification-contracts.md`](../../../docs/protocol-certification-contracts.md).

No JSON placeholder is committed before a producer runs. The aggregator joins actual observations against the
release-owned requirements catalog and emits missing required observations as explicit `skip` cells.
