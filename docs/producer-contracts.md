# Producer contracts

This page is the stable contract for every capability-matrix producer:
network-pulled snapshots (documented in the [README](../README.md#producers))
and the pushed-envelope producers added by honua-io/honua-evidence#8. Gate
consumers (honua-release's docs/evidence-freshness gates) read
`data/capability-matrix.v1.json`'s top-level `freshness` block and each
capability's per-type evidence arrays; this document is what makes both a
stable, versioned contract rather than an implementation detail.

## Freshness ledger (all producers)

Every producer that has ever produced anything — network-pulled or
pushed-envelope — gets exactly one entry in `capability-matrix.v1.json`'s
`freshness` object, keyed by producer name:

```jsonc
"freshness": {
  "<producer-name>": {
    "fetchedAt": "2026-08-18T14:34:16Z",   // when THIS aggregate.py run last looked
    "sourceVersion": "a1b2c3d4e5f6@2026-07-24T00:44:08Z",  // "<sha>@<ISO8601>", producer-defined
    "freshnessBasis": "fetch",              // "fetch" | "observation" -- see below
    "observedAt": "2026-08-18T14:34:16Z",   // the timestamp `status` is computed from
    "ageDays": 0,                           // age of observedAt -- THIS drives status
    "sourceAgeDays": 25,                    // age of the upstream artifact -- INFORMATIONAL ONLY
    "status": "fresh",                      // "fresh" | "stale" | "missing"
    "detail": "..."                         // present only when status is "missing"
  }
}
```

- **`fresh`** — read successfully, and last *observed* within the producer's
  staleness threshold (`DEFAULT_STALENESS_DAYS` in `scripts/aggregate.py`,
  overridable via `HONUA_EVIDENCE_STALENESS_JSON`).
- **`stale`** — read successfully, but not *observed* inside the threshold.
- **`missing`** — nothing could be read: a network producer's fetch failed, or
  a pushed-envelope producer's directory no longer yields any usable envelope.
  `missing` is never silently dropped and never faked as a pass — this is the
  single most important invariant a gate consumer can rely on.

### Observation vs content: two facts, two fields

Freshness keys off the **observation**, not the **content**
(honua-io/honua-release#89). Those are different facts and the ledger keeps
them in different fields, neither pretending to be the other:

| field | means | can set `stale`? |
|---|---|---|
| `ageDays` (with `observedAt`, `freshnessBasis`) | how long since this producer was last **observed** | **yes** — this is the verdict |
| `sourceAgeDays` | how old the upstream **artifact** itself is | **never**, on its own |

What counts as "the observation" is per-producer, declared in
`FRESHNESS_BASIS` in `scripts/aggregate.py`:

- **`observation` basis** — the producer's `sourceVersion` timestamp records a
  run/capture/review that actually **happened**: a honua-samples CI run, a CITE
  review date, a canary `runAt`, a DR drill `capturedAt`. That timestamp *is*
  the observation, so it drives `status` (`observedAt` = that timestamp). If
  the upstream job dies, the producer ages out and goes `stale` — which is
  precisely what should happen, and is why a committed canary envelope cannot
  look fresh forever just because the aggregator can still read the file.
- **`fetch` basis** — the producer's `sourceVersion` timestamp records when an
  upstream **file last changed** (a git commit date). That is a fact about the
  other repo's release cadence, not about this pipeline's evidence: the
  aggregator pulled the file successfully today, so the evidence *is* current.
  `status` therefore follows this aggregator's own last successful `fetchedAt`
  (`observedAt` = `fetchedAt`), and the file's commit age is reported as
  `sourceAgeDays` only.

`fetch` basis is **not** a licence to never age out. A fetch-basis producer the
aggregator can no longer read is `missing` (the fetch failed), and one it has
not successfully read inside its threshold is `stale` (`fetchedAt` itself aged
out). A dead upstream still surfaces — it just surfaces as the thing that
actually broke, instead of as "honua-server has not changed a file in 23 days".

Current bases (`scripts/aggregate.py`, `FRESHNESS_BASIS`):

| producer | basis | why |
|---|---|---|
| `server-keys`, `server-matrix` | `fetch` | a file in honua-server; its commit date is content |
| `sdk-js`, `sdk-dotnet`, `sdk-python` | `fetch` | a coverage file in each SDK repo; ditto |
| `open-issues` | `fetch` | a live GitHub query with no artifact timestamp at all (`"live-query"`) — the fetch is the only observation there is |
| `samples` | `observation` | a successful `run-samples` workflow run really executed then |
| `cite` | `observation` | `cite-status.md`'s self-declared "Last reviewed" date is when the CITE suite was actually reviewed |
| `dr-drills`, `live-canary` | `observation` | the envelope's `capturedAt`/`runAt` is when the drill/canary ran |

A producer **not** listed in `FRESHNESS_BASIS` defaults to `observation`, the
stricter basis: opting into `fetch` is a deliberate, documented statement that
the producer's `sourceVersion` carries content rather than observation, so
forgetting to declare a new producer can never accidentally make it un-ageable.

Thresholds follow the basis. A fetch-basis threshold answers "how long may the
aggregator have failed to look at all" (3 days — the aggregate job runs daily,
so that tolerates two entirely missed runs); an observation-basis threshold
answers "how long since the upstream job last actually ran" and keeps each
producer's real cadence (CITE 14 days, DR drills 45, canary 3).

### Producers defined but not yet producing

A pushed-envelope producer that is fully defined — envelope schema documented
here, loader and capability join wired up in `scripts/aggregate.py` — but that
has **never had a single envelope pushed** carries no `freshness` row at all.
It is declared in the matrix's top-level `awaitingFirstEnvelope` array instead,
printed as a `::notice::` by the aggregate run, and listed on the site's
freshness page under "Defined, not yet producing":

```jsonc
"awaitingFirstEnvelope": ["dr-drills"]
```

The reason is that `missing` has to keep meaning something. A row that has
always been missing teaches nothing, and it dilutes `missing` for the case a
gate consumer must react to: a producer that used to report and **stopped**.
The row returns automatically, with no aggregator change, the moment that
producer's first envelope lands (`AWAITING_FIRST_ENVELOPE` in
`scripts/aggregate.py`).

This applies **only** to producers explicitly listed in
`AWAITING_FIRST_ENVELOPE`. Every other producer always gets a row, so one that
has reported can never quietly vanish from the ledger by having its directory
emptied — it goes `missing`, loudly.

Gate consumers should treat an **absent key** in `freshness` for a producer
they name in their own config as equivalent to `missing`/`blocked` — that was
exactly honua-release#62's `cite` producer state before this issue landed. A
producer named in `awaitingFirstEnvelope` is the one honest exception: it is
absent because it does not exist yet, and it says so.

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

### DR drills (`dr-drills`) — defined, not yet producing

> **No DR drill has pushed an envelope yet**, so `dr-drills` carries no
> `freshness` row; it is declared in `awaitingFirstEnvelope` instead (see
> "Producers defined but not yet producing" above). The schema, loader,
> capability join and fixtures below are all in place and the ledger row
> returns automatically on the first envelope. Producer work — honua-iac's DR
> drill emitting this envelope — is tracked in honua-io/honua-evidence#20.

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

Freshness: `observation` basis — `sourceVersion` =
`"<sourceRef, 12 hex chars>@<capturedAt of the most recent envelope>"` (falls
back to the bare `capturedAt` if `sourceRef` isn't a parseable sha), and that
`capturedAt` drives `status`. Staleness threshold: 45 days
(`DEFAULT_STALENESS_DAYS["dr-drills"]`) — DR drills run on an operator-driven
cadence, not continuously. A drill lane that stops running ages out; the
aggregator being able to re-read an old committed envelope does not make it
fresh.

### Live canary / cloud e2e (`live-canary`)

Schema: `honua-evidence.live-canary-envelope/v1`. One manifest file per canary
run under `data/producers/live-canary/*.json`, produced by honua-release's
scheduled demo canary / cloud e2e workflow. The producer reports `fresh`,
`stale`, or `missing` from the newest valid delivered envelope using the
three-day threshold below.

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

Freshness: `observation` basis — `sourceVersion` =
`"<sourceRef, 12 hex chars>@<runAt of the most recent manifest>"` (falls back
to the bare `runAt` if `sourceRef` isn't a parseable sha), and that `runAt`
drives `status`. Staleness threshold: 3 days
(`DEFAULT_STALENESS_DAYS["live-canary"]`) — canary runs are meant to be cheap
and frequent (daily-ish), same reasoning as the `samples` producer's 3-day
threshold. A canary that stops running goes `stale` even though its last
envelope is still sitting in the directory: that is the observation basis doing
its job.

## Adding a pushed-envelope producer

1. Define a versioned envelope schema (`honua-evidence.<name>-envelope/v1`)
   and document it on this page, including which fields are required.
2. Add a `data/producers/<name>/README.md` explaining the convention (not a
   `.json` file — `aggregate.py` globs `*.json`, so a README is invisible to
   ingestion).
3. Add a loader in `scripts/aggregate.py` (`_load_envelopes` + a small
   `fetch_<name>()` wrapping it in a `Fetched`), an item-extraction function,
   and a `join_local_producer(...)` call in `build_matrix()`.
4. Register a default staleness threshold in `DEFAULT_STALENESS_DAYS`, a
   freshness basis in `FRESHNESS_BASIS` (a pushed-envelope producer is almost
   always `observation` — its envelope timestamp is a real run), and a
   `sourceArtifacts` entry. Until the producer's first envelope is pushed, add
   it to `AWAITING_FIRST_ENVELOPE` so it does not ship a permanently-`missing`
   ledger row, and open an issue for the producer work.
5. Add example fixtures under `tests/fixtures/<name>/` (a valid envelope and a
   deliberately malformed one) and extend `tests/test_aggregate.py`.
6. Unknown capability keys and malformed envelopes from this new producer must
   warn, not crash — follow the pattern in `join_local_producer`.
