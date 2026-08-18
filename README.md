# honua-evidence

**Every claim has a receipt. This repo is the receipts.**

honua-evidence is the aggregation and publication home for Honua's capability evidence. It joins versioned snapshots published by producer repos into a single `capability-matrix.v1.json` and renders it as a browsable evidence index (one page per capability), linking every claim down to a raw, independently verifiable artifact: proving tests and their CI runs, OGC CITE conformance results (with freshness — see below), real-client interop envelopes (`*.cert.json`), operation-level Esri parity cases, geobench performance runs, executable samples, terraform DR drill results, live/deployed-environment canary probes, and live demos.

## How it works

```
producers (pull, never push into us)
  honua-server   docs/gis/data/capability-keys.v1.json     canonical capability vocabulary (drift-gate source of truth)
                 docs/gis/data/capability-matrix.v1.json   Phase-A evidence snapshot: entry/test counts, CITE, parity,
                                                            esriAssess, interop, geobench, per capability key
                 issues, label cap/<category>               known-gaps preview (category-level, cached at build time)
                 docs/cite-status.md                        CITE freshness: "Last reviewed" date + commit sha (#8)
  honua-sdk-js         config/sdk-coverage.v1.json          per-capability SDK coverage
  honua-sdk-dotnet     contracts/sdk-coverage.v1.json       per-capability SDK coverage
  honua-sdk-python     compatibility/sdk-coverage.v1.json   per-capability SDK coverage
  honua-samples        run-samples workflow artifact        samples-coverage.v1.json (executable-sample evidence)
                        (honua-io/honua-samples#5)

producers (pushed envelopes -- an out-of-band job commits INTO this repo; we never fetch from theirs; #8)
  honua-terraform      data/producers/dr-drills/*.json      DR drill evidence (backup-restore/failover runbooks)
  honua-release        data/producers/live-canary/*.json    live/deployed-environment canary + cloud e2e results
                        (honua-io/honua-release#72; scheduled canaries actively push versioned envelopes)

aggregate (scripts/aggregate.py, stdlib Python only, CI: .github/workflows/aggregate.yml)
  data/capability-matrix.v1.json   ← every producer key validated against capability-keys.v1.json;
                                      an unknown key FAILS the build (the drift gate)
                                    ← per-producer freshness ledger: {fetchedAt, sourceVersion,
                                      freshnessBasis, observedAt, ageDays, sourceAgeDays, status}
                                      status is "fresh" | "stale" | "missing" -- a producer that can't
                                      be reached is recorded as missing, never silently dropped;
                                      status follows the OBSERVATION (ageDays), never the upstream
                                      artifact's own age (sourceAgeDays, informational)

publish (scripts/build-site.py, stdlib Python only, zero frameworks)
  site/                              GitHub Pages, deployed by aggregate.yml
    index.html                       all capabilities, filterable, receipts-first
    capabilities/<key>.html          one L2 page per capability: evidence by type → SDK coverage →
                                      samples → known gaps → raw receipt links
    freshness.html                   the producer freshness ledger, rendered
    data/capability-matrix.v1.json   the JSON download

  → https://evidence.honua.io/ (custom domain, live -- build-site.py emits the CNAME file;
    https://honua-io.github.io/honua-evidence/ redirects there via GitHub Pages. All links
    inside the site are relative, so both hosts serve identically.)

validate (scripts/validate-site.py, stdlib Python only, CI: validate.yml + aggregate.yml)
  internal links + fragments, static a11y pass (lang/title/headings/img alt/
  control labels/table captions+scope), CNAME/.nojekyll presence, and the
  issue-#2 receipt walk: index card -> capabilities/editing-featureserver-edits.html
  -> raw receipts, with every external hop HTTP-checked in the PR gate (--online)

watchdog (scripts/check-ledger-liveness.py, CI: .github/workflows/ledger-liveness.yml, every 3h)
  is the ledger still MOVING? -- capability-matrix.v1.json's own generatedAt age (>30h = stalled)
  plus any `aggregate` run parked un-started for >45m (the leading indicator). Cancels a run
  parked on an unapprovable deployment, files/updates one alert issue here, and fails red.
  Exists because the 2026-08-16 deadlock (#17) was invisible in this repo for 42h and surfaced
  three repos away as a wrong accusation against another producer.
```

### Producers

Freshness **basis** (see below) is per producer: `fetch` means the status
follows this pipeline's own successful pull, `observation` means it follows a
run/capture/review that really happened.

| Producer | Snapshot | Basis | Threshold |
|---|---|---|---|
| [honua-server](https://github.com/honua-io/honua-server) | [capability-keys.v1.json](https://github.com/honua-io/honua-server/blob/trunk/docs/gis/data/capability-keys.v1.json) (canonical vocabulary) | fetch | 3 days |
| [honua-server](https://github.com/honua-io/honua-server) | [capability-matrix.v1.json](https://github.com/honua-io/honua-server/blob/trunk/docs/gis/data/capability-matrix.v1.json) (Phase-A evidence) | fetch | 3 days |
| [honua-sdk-js](https://github.com/honua-io/honua-sdk-js) | [config/sdk-coverage.v1.json](https://github.com/honua-io/honua-sdk-js/blob/trunk/config/sdk-coverage.v1.json) | fetch | 3 days |
| [honua-sdk-dotnet](https://github.com/honua-io/honua-sdk-dotnet) | [contracts/sdk-coverage.v1.json](https://github.com/honua-io/honua-sdk-dotnet/blob/trunk/contracts/sdk-coverage.v1.json) | fetch | 3 days |
| [honua-sdk-python](https://github.com/honua-io/honua-sdk-python) | [compatibility/sdk-coverage.v1.json](https://github.com/honua-io/honua-sdk-python/blob/trunk/compatibility/sdk-coverage.v1.json) | fetch | 3 days |
| [honua-server issues](https://github.com/honua-io/honua-server/issues) | open issues by `cap/<category>` label | fetch | 3 days |
| [honua-samples](https://github.com/honua-io/honua-samples) | [run-samples](https://github.com/honua-io/honua-samples/actions/workflows/run-samples.yml) workflow artifact `samples-coverage` | observation | 3 days |
| [honua-server](https://github.com/honua-io/honua-server) | [cite-status.md](https://github.com/honua-io/honua-server/blob/trunk/docs/cite-status.md) "Last reviewed" date + commit sha | observation | 14 days |
| [honua-release](https://github.com/honua-io/honua-release) | pushed envelopes under [`data/producers/live-canary/`](data/producers/live-canary/) | observation | 3 days |
| [honua-iac](https://github.com/honua-io/honua-iac) | pushed envelopes under [`data/producers/dr-drills/`](data/producers/dr-drills/) — **not producing yet**, see [#20](https://github.com/honua-io/honua-evidence/issues/20) | observation | 45 days |

The last two rows are **pushed-envelope** producers, not network pulls — see
[`docs/producer-contracts.md`](docs/producer-contracts.md) for their schemas,
the forgiving (warn, not crash) unknown-capability-key contract that's
deliberately different from the drift gate below, and full worked examples.

### Freshness semantics

Each producer that has ever produced anything gets one entry in the `freshness` ledger of
`data/capability-matrix.v1.json`:

- **`fresh`** — read successfully and last **observed** within that producer's threshold.
- **`stale`** — read successfully, but not observed inside the threshold. Thresholds are the
  `DEFAULT_STALENESS_DAYS` dict at the top of `scripts/aggregate.py`; override any of them
  without editing the script via the `HONUA_EVIDENCE_STALENESS_JSON` env var (a JSON object,
  e.g. `{"samples": 5}`).
- **`missing`** — the read failed outright (network error, missing/expired artifact, no usable
  envelope left in a pushed-envelope directory, or no `HONUA_EVIDENCE_TOKEN` for a producer
  that needs cross-repo auth). Never silently omitted; the site renders the ledger and every
  capability's SDK/sample/DR/canary sections say so plainly.

**Observation, not content** (honua-io/honua-release#89). `status` is computed from when the
producer was last *observed* (`ageDays`, from `observedAt`), never from how old the upstream
artifact happens to be (`sourceAgeDays`, informational). For a `fetch`-basis producer the
observation is this pipeline's own successful pull, so honua-server not changing a file for a
month is `fresh` with `sourceAgeDays: 30` — a fact about honua-server's cadence, not stale
evidence. For an `observation`-basis producer the timestamp records a run that actually happened
(a CITE review, a canary run, a DR drill capture), so an upstream job that dies still ages out —
`fetch` basis is not a licence to never go stale either: a producer the aggregator stops being
able to read is `missing`, and one it has not read inside the threshold is `stale`.

A producer whose envelope schema and ingestion are wired up but that has **never produced
anything** carries no ledger row at all; it is declared in the matrix's `awaitingFirstEnvelope`
array, and its row returns automatically on the first envelope. `missing` is reserved for the
case that matters — a producer that used to report and stopped. An absent key in `freshness`
(no entry at all, and not in `awaitingFirstEnvelope`) should be treated by gate consumers the
same as `missing` — see `docs/producer-contracts.md`.

### How to add a producer

1. Add a `fetch_*` function in `scripts/aggregate.py` that returns a `Fetched` (data,
   `sourceVersion`, and an `error` on failure — never raise past the caller). For a
   pushed-envelope producer (files committed into `data/producers/<name>/` by an
   out-of-band job, rather than fetched over HTTP), see `docs/producer-contracts.md`'s
   "Adding a pushed-envelope producer" section instead — that path uses `join_local_producer`
   and a forgiving warn-not-crash contract for unknown keys, not the hard drift gate below.
2. Register its name in `DEFAULT_STALENESS_DAYS`, `FRESHNESS_BASIS` (`fetch` only if its
   `sourceVersion` is an upstream file's commit date — the default `observation` is stricter),
   and `SOURCE_REPO_LINKS` (build-site.py).
3. Join its per-capability keys into `capabilities_out` in `build_matrix()`, keyed to the
   canonical vocabulary. Any key it reports that isn't in `capability-keys.v1.json` fails the
   build — that's the drift gate; new keys land in honua-server first.
4. Add it to the `fetches` dict feeding `build_freshness_ledger()` and to the producer table
   above. If it is a pushed-envelope producer with no envelopes yet, also add it to
   `AWAITING_FIRST_ENVELOPE` and open an issue for the producer work, so it does not ship a
   permanently-`missing` row.
5. If it's cross-repo (not a public raw file), the honua-evidence workflow needs read access —
   see the `HONUA_EVIDENCE_TOKEN` note in `.github/workflows/aggregate.yml`.

Design rules:

- **One key vocabulary.** Capability keys are owned by [honua-server](https://github.com/honua-io/honua-server) (never forked here); everything in this repo is keyed to them and drift-gated.
- **Data flows one direction.** Producers publish versioned snapshots (pulled by us) or push
  versioned envelopes (committed by an out-of-band job into `data/producers/`, read by us).
  Either way, nothing here writes into a producer.
- **Nothing terminates in a claim.** Every rendered statement links one level down and bottoms out in an artifact a third party hosts or can re-run.
- **Gaps ship next to strengths.** Capabilities with partial or pending evidence say so, in writing, on the same page.

## Local usage

```sh
python3 scripts/aggregate.py            # pull producers, write data/capability-matrix.v1.json
python3 scripts/aggregate.py --check    # drift/freshness check only, non-zero exit on drift (CI mode)
python3 scripts/build-site.py           # render site/ from data/capability-matrix.v1.json
python3 scripts/validate-site.py        # offline site gate: links, a11y, structure, receipt walk
python3 scripts/validate-site.py --online  # + HTTP-check the external receipt-walk hops (CI PR gate)
python3 -m unittest discover -s tests   # unit tests (ingestion, freshness, drift/warning contracts)

# Per-prospect evidence briefs (#4): BUYER-SHAREABLE Markdown from the matrix.
python3 scripts/generate-brief.py brief --prospect "Acme County" \
    --caps serve.ogc-api-features,editing.featureserver-edits   # or --caps-url "...?caps=..."
python3 scripts/generate-brief.py brief --prospect "Acme County" \
    --caps-file honua-caps.json         # honua-esri-assess --emit honua-caps output
python3 scripts/generate-brief.py proof-counts --output -   # conformance-counts refresh block
```

All scripts are Python 3 standard library only — no `pip install`, no `npm install`. Network
access is required for `aggregate.py` (it pulls from `raw.githubusercontent.com` and the GitHub
API); `build-site.py` is offline and only reads the already-aggregated JSON, and
`validate-site.py` is offline unless given `--online`. The test suite is
`unittest`-only (no pytest dependency), but is pytest-discoverable too if pytest happens to be
installed.

## Evidence briefs

`scripts/generate-brief.py` (issue [#4](https://github.com/honua-io/honua-evidence/issues/4))
turns a capability key list — a comma-separated intake list, a shareable
`?caps=<keys>[&units=<estimate>]` catalog URL, or a `honua-caps.v1` JSON file
produced by honua-esri-assess's `--emit honua-caps` estate crosswalk — into a
per-prospect, BUYER-SHAREABLE Markdown evidence brief rendered offline from
the committed `data/capability-matrix.v1.json`: front matter with the
proof-asset classification and matrix version, one card per capability
(evidence table + link to its L2 page on evidence.honua.io), an edition
estimate, and the freshness ledger restated in full. Two hard rules, both
CI-tested:

- **Gaps cannot be removed.** Every card carries its gap disclosures (open
  issues, uncovered SDK lanes, missing/stale producers) and there is no flag
  to omit them. Delivery is human-in-the-loop only: the generator writes a
  file (default `dist/briefs/`, gitignored); a person reviews and sends it.
- **Buyer-shareable output guard.** This repo is public and briefs leave the
  building, so the rendered text is scanned for internal-only strings
  (private repo names, personal email addresses) before anything is written;
  a hit aborts without writing. Public contact only: info@honua.io
  (security@honua.io for security questions).

`generate-brief.py proof-counts` emits the marker-delimited protocol-
conformance counts block (per-suite CITE passed/total plus staleness and
unjoined-suite disclosures) used to refresh the sales proof-asset package's
conformance summary per release via a reviewed PR.

## Status

Phase B (honua-io/honua-server#2892) landed the aggregation pipeline, freshness ledger, and the
core evidence index (issues [#1](https://github.com/honua-io/honua-evidence/issues/1),
[#3](https://github.com/honua-io/honua-evidence/issues/3), and
[#2](https://github.com/honua-io/honua-evidence/issues/2): the evidence index site, live at
[evidence.honua.io](https://evidence.honua.io/), with its a11y/link/receipt-walk CI gate in
`scripts/validate-site.py`). Remaining scope — richer per-test/per-run evidence detail,
the honua-site L2-page redirect decision, dispatch senders on producer repos, and
full per-capability gaps ingestion — stays open on those issues and on
[#5](https://github.com/honua-io/honua-evidence/issues/5).

[#8](https://github.com/honua-io/honua-evidence/issues/8) added CITE freshness
(timestamp + source sha) and two new pushed-envelope producers, DR drills and
live/canary results (contracts documented in
[`docs/producer-contracts.md`](docs/producer-contracts.md)). honua-release now
commits scheduled live-canary envelopes; a producer such as DR drills still
reports `missing` until it actually contributes evidence — this repo never
fabricates evidence for a producer that has not produced anything yet.

## License

Apache-2.0.
