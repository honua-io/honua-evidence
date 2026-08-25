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

## Certification receipt contract

Passing and failing observations carry an immutable receipt with schema
`honua.certification-evidence-receipt/v1`. Ordinary unlicensed v1 receipts keep the original five
top-level fields—`schema`, `identity`, `result`, `facets`, and `payload_base64`—and MUST NOT add an
`entitlement` member or `identity.entitlement_policy_revision`.

A requirement with `licensed: true` uses the same v1 receipt envelope with both
`identity.entitlement_policy_revision` and this closed `entitlement` object:

```json
{
  "policy_revision": "honua-pro-feature-subscriptions-v1",
  "capability_key": "realtime.feature-subscriptions",
  "deployment_target": "licensed-release",
  "verification": "live-server-capability-probe-v1",
  "status": "active",
  "checked_at": "2026-08-20T10:02:00Z",
  "license_fingerprint": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
}
```

The field meanings are normative:

| Field | Contract |
| --- | --- |
| `policy_revision` | Exact governed entitlement-policy revision; it must equal both the requirement and `identity.entitlement_policy_revision`. |
| `capability_key` | Exact governed capability exercised by this receipt. |
| `deployment_target` | Exact governed licensed target on which the live check ran. |
| `verification` | Fixed verifier contract, currently `live-server-capability-probe-v1`; synthetic or client-only checks are not accepted. |
| `status` | Must be `active` at verification time. |
| `checked_at` | RFC 3339 live-verification timestamp, within the observation's inclusive `started_at` / `completed_at` interval. |
| `license_fingerprint` | Lowercase `sha256:` digest of a producer-defined, non-secret stable license identifier. It is correlation evidence only: never hash or publish a license key, token, private key, or raw entitlement credential. |

The object is closed: missing or additional fields fail validation. Policy, capability, and target
must match the authoritative requirement, and a licensed receipt is accepted only for a governed
policy/target/auth tuple. The canonical examples are
`tests/fixtures/protocol-certification/licensed-entitlement-receipt.v1.json` and
`unlicensed-receipt.v1.json`.

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
