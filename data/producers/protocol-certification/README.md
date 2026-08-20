# Protocol certification producer fragments

Evidence producers commit immutable `*.json` fragments below this directory. The contract is documented in
[`docs/protocol-certification-contracts.md`](../../../docs/protocol-certification-contracts.md).

No JSON placeholder is committed before a producer runs. The aggregator joins actual observations against the
release-owned requirements catalog and emits missing required observations as explicit `skip` cells.
