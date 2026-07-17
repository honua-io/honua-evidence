# honua-evidence

**Every claim has a receipt. This repo is the receipts.**

honua-evidence is the aggregation and publication home for Honua's capability evidence. It joins versioned snapshots published by producer repos into a single `capability-matrix.v1.json` and renders it as a browsable evidence index (one page per capability), linking every claim down to a raw, independently verifiable artifact: proving tests and their CI runs, OGC CITE conformance results, real-client interop envelopes (`*.cert.json`), operation-level Esri parity cases, geobench performance runs, executable samples, and live demos.

## How it works

```
producers (pull, never push into us)
  honua-server        → server-capabilities.v1.json   (canonical capability keys + crosswalks + test/CITE evidence)
  honua-sdk-js/.net/py → sdk-coverage.v1.json          (per-capability SDK coverage)
  honua-samples       → sample manifests + run results (executable evidence)
  geobench            → benchmark run summaries

aggregate (this repo, CI)
  capability-matrix.v1.json   ← validated against the canonical key list; unknown keys fail the build

publish
  evidence index (static site)    one page per capability: summary → evidence by type → known gaps & roadmap → raw receipts
  per-prospect evidence briefs    BUYER-SHAREABLE Markdown, generated from a capability selection
```

Design rules:

- **One key vocabulary.** Capability keys are owned by [honua-server](https://github.com/honua-io/honua-server) (never forked here); everything in this repo is keyed to them and drift-gated.
- **Data flows one direction.** Producers publish versioned snapshots; this repo pulls. Nothing here writes into a producer.
- **Nothing terminates in a claim.** Every rendered statement links one level down and bottoms out in an artifact a third party hosts or can re-run.
- **Gaps ship next to strengths.** Capabilities with partial or pending evidence say so, in writing, on the same page.

## Status

Bootstrap. Phase A of the rollout runs aggregation inside honua-server CI; this repo takes over in Phase B (lift-and-shift of the CI job — the versioned schema is the contract). Coordination: [honua-server#2892](https://github.com/honua-io/honua-server/issues/2892).

## License

Apache-2.0.
