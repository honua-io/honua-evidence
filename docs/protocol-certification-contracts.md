# Protocol certification evidence contracts

The authoritative denominator is `honua.protocol-certification-requirements/v1` in `honua-release`.
Evidence producers push immutable fragments using this envelope:

```json
{
  "schema": "honua.protocol-certification-fragment/v1",
  "producer": "honua-server-cng",
  "generated_at": "2026-08-20T10:06:00Z",
  "candidate": {
    "source_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "image_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "cut_at": "2026-08-20T09:00:00Z"
  },
  "observations": [{
    "surface": "cog",
    "operation": "window-read",
    "canonical_client": "Rasterio",
    "client_version": "1.4.3",
    "deployment_target": "local-docker",
    "result": "pass",
    "skip_reason": null,
    "source_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "producer_source_sha": "cccccccccccccccccccccccccccccccccccccccc",
    "image_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "fixture_revision": "fixture-cog-v1",
    "contract_revision": "cog-1.0",
    "auth_policy_revision": "anonymous-v1",
    "evidence_uri": "https://github.com/honua-io/honua-server/actions/runs/1",
    "started_at": "2026-08-20T10:00:00Z",
    "completed_at": "2026-08-20T10:05:00Z"
  }]
}
```

## Join rules

- Candidate source SHA, image digest, and cut timestamp form an exact server identity.
- Every observation separately records the exact producer repository SHA that generated it.
- A cell identity is surface, operation, canonical client, client version, and deployment target.
- The newest observation from one producer wins for a cell.
- Two producers claiming the same cell are ambiguous and fail aggregation.
- Observations absent from the requirements catalog fail aggregation.
- Requirements absent from all observations are emitted as `skip`, never omitted or marked passing.
- Non-addressable requirements are emitted as `not-addressable` with their catalog rationale.
- Producer observations cannot override maturity, tier, client lane, facets, or invalidation revisions.

The output is `data/protocol-certification.v1.json`, consumed by `honua-release`'s
`gate-protocol-certification` workflow. Release evaluation binds it to the exact candidate and fails on every
required skip, stale licensed run, mismatched digest, or pre-cut observation.

The aggregator also publishes `data/protocol-certification-summary.v1.json`. It reports result counts by
surface, canonical client, deployment target, and required tier; scenario-facet counts; supported-operation
coverage; and per-client operation depth. These metrics are diagnostic only: percentages never override a
failed, skipped, stale, mismatched, or otherwise invalid required cell in the release ledger.
