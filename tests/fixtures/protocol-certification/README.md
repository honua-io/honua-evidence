The upstream inventory fixture is the original, unmodified input from
https://github.com/honua-io/honua-release/blob/32b68decc54c0db88780b4e439ecbcb00504bb36/docs/cloud-native-client-inventory.yaml.
Its SHA-256 is `a3533deb93348ea3f9d7f65b286cdbbf4c6ef7e857fa7d073723812631bfed75`.
It pins the Guide revision `0fabdf7c04ff135dd8160f39bf7a0fbb2f402f46`.
The completeness test compares every original format/tool pair with the normalized
inventory, so removing an inconvenient consumer or an entire section fails locally
and in PR CI without network access. Updating the source requires a reviewed revision
pin, the corresponding source bytes, and reconciliation of all roster entries.

`join-scenarios.json` is a deliberately small input fixture for ledger aggregation,
with manually enumerated result states across six producer families. The test creates
digest-bound normalized receipts, invokes the aggregation CLI, and compares the output
with hand-counted dimension and assertion totals. These are aggregation contract tests;
the data is not evidence that the actual clients or a release candidate executed.
