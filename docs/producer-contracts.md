# Producer contracts

This page is the stable contract for every capability-matrix producer:
network-pulled snapshots (documented in the [README](../README.md#producers))
and the pushed-envelope producers added by honua-io/honua-evidence#8. Gate
consumers (honua-release's docs/evidence-freshness gates) read
`data/capability-matrix.v1.json`'s top-level `freshness` block and each
capability's per-type evidence arrays; this document is what makes both a
stable, versioned contract rather than an implementation detail.

## Freshness ledger (all producers)

Every producer — network-pulled or pushed-envelope — gets exactly one entry
in `capability-matrix.v1.json`'s `freshness` object, keyed by producer name:

```jsonc
"freshness": {
  "<producer-name>": {
    "fetchedAt": "2026-07-20T21:51:09Z",   // when THIS aggregate.py run last looked
    "sourceVersion": "a1b2c3d4e5f6@2026-07-15T03:12:44Z",  // "<sha>@<ISO8601>", producer-defined
    "ageDays": 5,                           // omitted when status is "missing"
    "status": "fresh",                      // "fresh" | "stale" | "missing"
    "detail": "..."                         // present only when status is "missing"
  }
}
```

- **`fresh`** — pulled/read successfully and within the producer's staleness
  threshold (`DEFAULT_STALENESS_DAYS` in `scripts/aggregate.py`, overridable
  via `HONUA_EVIDENCE_STALENESS_JSON`).
- **`stale`** — read successfully but older than the threshold.
- **`missing`** — nothing could be read: a network producer's fetch failed, or
  a pushed-envelope producer's directory has no envelopes yet. `missing` is
  never silently dropped and never faked as a pass — this is the single most
  important invariant a gate consumer can rely on.

Gate consumers should treat an **absent key** in `freshness` (the producer
isn't listed at all) as equivalent to `missing`/`blocked` — that was exactly
honua-release#62's `cite` producer state before this issue landed.

### Per-capability degradation markers (no fabricated evidence)

The ledger is producer-granular; two per-capability fields additionally carry
their own explicit degradation markers so a missing producer can never be
mistaken for a real coverage claim:

- `capabilities[*].sdks.<js|dotnet|python>` is `{"status": "producer-missing"}`
  when that SDK's coverage snapshot could not be fetched at all (e.g.
  honua-sdk-dotnet before `contracts/sdk-coverage.v1.json` first lands on its
  trunk). `{"status": "not-covered"}` is reserved for a snapshot that loaded
  successfully and genuinely does not list the key.
- `capabilities[*].samples` is `null` when the honua-samples coverage artifact
  was unavailable this run (coverage unknown); `[]` means the artifact was
  fetched and lists no sample for the key.

A **stale**-but-readable snapshot keeps its real data in the capability rows
and is flagged only in the ledger — old evidence is still evidence.

## CITE freshness (`cite`)

Ingested from honua-server's hand-maintained
[`docs/cite-status.md`](https://github.com/honua-io/honua-server/blob/trunk/docs/cite-status.md),
the same file honua-server's own
`scripts/ci/check-cite-status-freshness.sh` asserts against. `aggregate.py`
parses the `Last reviewed: YYYY-MM-DD` line and pairs it with the file's
current commit sha:

- `sourceVersion` = `"<cite-status.md commit sha, 12 hex chars>@<Last reviewed date>T00:00:00Z"`.
  Deliberately uses the **reviewed date the document itself claims**, not the
  commit's own date — the reviewed date is what actually reflects when the
  CITE suite last ran.
- Staleness threshold: 14 days (`DEFAULT_STALENESS_DAYS["cite"]`), matching
  both honua-server's own freshness check and honua-release's
  `certification/evidence-freshness.yaml` `cite.maxAgeHours: 336`.
- No per-capability join changes: each capability's existing `cite` array
  (suite/profile/passed/total/passRate, joined from honua-server's Phase-A
  `capability-matrix.v1.json`) is untouched. This issue only adds the
  timestamp/sha freshness signal that gate consumers were missing.

## Pushed-envelope producers

Unlike the producers above, DR drills and live-canary results are not fetched
over the network — an out-of-band operator or automation job commits
versioned JSON envelope files directly into this repo, under
`data/producers/<name>/`. `scripts/aggregate.py` reads whatever `*.json` files
exist there on each run; it never reaches out to honua-terraform or
honua-release to pull them.

**Unknown-capability-key contract (deliberately different from the drift
gate).** The server-matrix/SDK/samples producers' drift gate (issue #1) FAILS
the build if any of them reference a capability key absent from
honua-server's canonical `capability-keys.v1.json`. Pushed envelopes get a
forgiving contract instead: an unknown key is a **warning**, collected in the
matrix's top-level `ingestionWarnings` array and printed to the build log
(`::warning::...`), never a build failure. A typo in a hand-authored evidence
envelope must not take down the whole aggregation pipeline the way a real
producer regression should.

**Malformed-envelope contract.** A `*.json` file that isn't valid JSON, isn't
a JSON object, or is missing a required field is skipped and recorded in
`ingestionWarnings` the same way — never a crash. An empty/absent directory is
not malformed; it is the normal `missing` state.

### DR drills (`dr-drills`)

Schema: `honua-evidence.dr-drill-envelope/v1`. One file per drill run under
`data/producers/dr-drills/*.json`, wrapping honua-terraform's
[`docs/devops/dr-evidence-template.json`](https://github.com/honua-io/honua-terraform/blob/trunk/docs/devops/dr-evidence-template.json)
fields with an explicit capability-key join (the template itself has no
opinion on capability keys — that mapping belongs here, not in honua-terraform).

```jsonc
{
  "schema": "honua-evidence.dr-drill-envelope/v1",
  "id": "2026-07-15-backup-restore-aws-ecs",      // required, unique per envelope
  "capabilityKeys": ["dr.backup-automation", "dr.rto-rpo-reporting"],  // required, non-empty
  "drill": "backup-restore",                       // required: "backup-restore" | "failover"
  "cloud": "aws",                                  // optional
  "target": "aws-ecs",                             // optional: target stack/deployment
  "environment": "validation",                     // optional
  "capturedAt": "2026-07-15T03:12:44Z",            // required, ISO 8601 UTC -- drives freshness age
  "verdict": "pass",                               // required: "pass" | "fail" | "not-evaluated"
  "measurements": { "...": "passthrough from honua-terraform's dr-evidence-template.json, optional" },
  "targets": { "rto_target_seconds": 900, "rpo_target_seconds": 300 },  // optional
  "checks": [ { "name": "restore_within_rto", "verdict": "pass" } ],    // optional
  "sourceRepo": "honua-io/honua-terraform",        // optional
  "sourceRef": "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4",  // optional, honua-terraform commit sha
  "sourceRunUrl": "https://github.com/honua-io/honua-terraform/actions/runs/123456789"  // optional
}
```

Required fields: `schema`, `id`, `capabilityKeys`, `drill`, `capturedAt`,
`verdict`. A full worked example is at
[`tests/fixtures/dr-drills/example-backup-restore.json`](../tests/fixtures/dr-drills/example-backup-restore.json).

Join: for each capability key in `capabilityKeys`, every matching capability
in the matrix gets an entry appended to its `dr` array:

```jsonc
"dr": [
  {
    "id": "2026-07-15-backup-restore-aws-ecs",
    "drill": "backup-restore",
    "cloud": "aws",
    "target": "aws-ecs",
    "environment": "validation",
    "capturedAt": "2026-07-15T03:12:44Z",
    "verdict": "pass",
    "sourceRunUrl": "https://github.com/honua-io/honua-terraform/actions/runs/123456789"
  }
]
```

Freshness: `sourceVersion` = `"<sourceRef, 12 hex chars>@<capturedAt of the most recent envelope>"`
(falls back to the bare `capturedAt` if `sourceRef` isn't a parseable sha).
Staleness threshold: 45 days (`DEFAULT_STALENESS_DAYS["dr-drills"]`) — DR
drills run on an operator-driven cadence, not continuously.

### Live canary / cloud e2e (`live-canary`)

Schema: `honua-evidence.live-canary-envelope/v1`. One manifest file per canary
run under `data/producers/live-canary/*.json`, produced by honua-release#61's
planned scheduled demo canary / cloud e2e workflow (**not yet landed as of
this ingestion** — the `live-canary` producer will correctly report `missing`
until that workflow starts pushing envelopes here).

```jsonc
{
  "schema": "honua-evidence.live-canary-envelope/v1",
  "manifestId": "demo-canary-2026-07-20T06:00:00Z",  // required, unique per run
  "targetEnvironment": "demo.honua.io",               // required
  "targetUrl": "https://demo.honua.io",               // optional
  "runAt": "2026-07-20T06:00:03Z",                    // required, ISO 8601 UTC -- drives freshness age
  "overallStatus": "green",                           // optional: "green" | "red" | "partial"
  "sourceRepo": "honua-io/honua-release",             // optional
  "sourceRef": "b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5",  // optional, honua-release commit sha
  "sourceRunUrl": "https://github.com/honua-io/honua-release/actions/runs/987654321",  // optional
  "probes": [                                          // required, list (may be empty)
    {
      "probeName": "ogc-api-features-smoke",           // required per probe
      "capabilityKeys": ["serve.ogc-api-features"],    // required per probe, non-empty
      "status": "green",                               // required per probe: "green" | "red"
      "lastGreenAt": "2026-07-20T06:00:03Z",            // required per probe
      "detail": "GET /ogc/collections -> 200, 12 collections"  // optional
    }
  ]
}
```

Required top-level fields: `schema`, `manifestId`, `targetEnvironment`,
`runAt`, `probes`. A probe missing `capabilityKeys` is skipped with a warning,
not the whole manifest. A full worked example is at
[`tests/fixtures/live-canary/example-demo-canary.json`](../tests/fixtures/live-canary/example-demo-canary.json).

Join: for each probe's `capabilityKeys`, every matching capability in the
matrix gets an entry appended to its `liveCanary` array:

```jsonc
"liveCanary": [
  {
    "manifestId": "demo-canary-2026-07-20T06:00:00Z",
    "probeName": "ogc-api-features-smoke",
    "targetEnvironment": "demo.honua.io",
    "status": "green",
    "lastGreenAt": "2026-07-20T06:00:03Z",
    "sourceRunUrl": "https://github.com/honua-io/honua-release/actions/runs/987654321"
  }
]
```

Freshness: `sourceVersion` = `"<sourceRef, 12 hex chars>@<runAt of the most recent manifest>"`
(falls back to the bare `runAt` if `sourceRef` isn't a parseable sha).
Staleness threshold: 3 days (`DEFAULT_STALENESS_DAYS["live-canary"]`) —
canary runs are meant to be cheap and frequent (daily-ish), same reasoning as
the `samples` producer's 3-day threshold.

## Adding a pushed-envelope producer

1. Define a versioned envelope schema (`honua-evidence.<name>-envelope/v1`)
   and document it on this page, including which fields are required.
2. Add a `data/producers/<name>/README.md` explaining the convention (not a
   `.json` file — `aggregate.py` globs `*.json`, so a README is invisible to
   ingestion).
3. Add a loader in `scripts/aggregate.py` (`_load_envelopes` + a small
   `fetch_<name>()` wrapping it in a `Fetched`), an item-extraction function,
   and a `join_local_producer(...)` call in `build_matrix()`.
4. Register a default staleness threshold in `DEFAULT_STALENESS_DAYS` and a
   `sourceArtifacts` entry.
5. Add example fixtures under `tests/fixtures/<name>/` (a valid envelope and a
   deliberately malformed one) and extend `tests/test_aggregate.py`.
6. Unknown capability keys and malformed envelopes from this new producer must
   warn, not crash — follow the pattern in `join_local_producer`.
