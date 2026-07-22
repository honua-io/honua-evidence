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
                        (honua-io/honua-release#61, not yet landed -- reports "missing" until it pushes envelopes)

aggregate (scripts/aggregate.py, stdlib Python only, CI: .github/workflows/aggregate.yml)
  data/capability-matrix.v1.json   ← every producer key validated against capability-keys.v1.json;
                                      an unknown key FAILS the build (the drift gate)
                                    ← per-producer freshness ledger: {fetchedAt, sourceVersion, status}
                                      status is "fresh" | "stale" | "missing" -- a producer that can't
                                      be reached is recorded as missing, never silently dropped

publish (scripts/build-site.py, stdlib Python only, zero frameworks)
  site/                              GitHub Pages, deployed by aggregate.yml
    index.html                       all capabilities, filterable, receipts-first
    capabilities/<key>.html          one L2 page per capability: evidence by type → SDK coverage →
                                      samples → known gaps → raw receipt links
    freshness.html                   the producer freshness ledger, rendered
    data/capability-matrix.v1.json   the JSON download

  → https://honua-io.github.io/honua-evidence/ (default Pages URL; no custom domain yet --
    DNS/CNAME is a pending decision, see honua-io/honua-server#2892. All links inside the
    site are relative so a domain can be added later without a rewrite.)
```

### Producers

| Producer | Snapshot | Freshness threshold |
|---|---|---|
| [honua-server](https://github.com/honua-io/honua-server) | [capability-keys.v1.json](https://github.com/honua-io/honua-server/blob/trunk/docs/gis/data/capability-keys.v1.json) (canonical vocabulary) | 14 days |
| [honua-server](https://github.com/honua-io/honua-server) | [capability-matrix.v1.json](https://github.com/honua-io/honua-server/blob/trunk/docs/gis/data/capability-matrix.v1.json) (Phase-A evidence) | 14 days |
| [honua-sdk-js](https://github.com/honua-io/honua-sdk-js) | [config/sdk-coverage.v1.json](https://github.com/honua-io/honua-sdk-js/blob/trunk/config/sdk-coverage.v1.json) | 30 days |
| [honua-sdk-dotnet](https://github.com/honua-io/honua-sdk-dotnet) | [contracts/sdk-coverage.v1.json](https://github.com/honua-io/honua-sdk-dotnet/blob/trunk/contracts/sdk-coverage.v1.json) | 30 days |
| [honua-sdk-python](https://github.com/honua-io/honua-sdk-python) | [compatibility/sdk-coverage.v1.json](https://github.com/honua-io/honua-sdk-python/blob/trunk/compatibility/sdk-coverage.v1.json) | 30 days |
| [honua-samples](https://github.com/honua-io/honua-samples) | [run-samples](https://github.com/honua-io/honua-samples/actions/workflows/run-samples.yml) workflow artifact `samples-coverage` | 3 days |
| [honua-server issues](https://github.com/honua-io/honua-server/issues) | open issues by `cap/<category>` label | live query (no fixed version) |
| [honua-server](https://github.com/honua-io/honua-server) | [cite-status.md](https://github.com/honua-io/honua-server/blob/trunk/docs/cite-status.md) "Last reviewed" date + commit sha | 14 days |
| [honua-terraform](https://github.com/honua-io/honua-terraform) | pushed envelopes under [`data/producers/dr-drills/`](data/producers/dr-drills/) | 45 days |
| [honua-release](https://github.com/honua-io/honua-release) | pushed envelopes under [`data/producers/live-canary/`](data/producers/live-canary/) (not yet producing any — honua-io/honua-release#61) | 3 days |

The last two rows are **pushed-envelope** producers, not network pulls — see
[`docs/producer-contracts.md`](docs/producer-contracts.md) for their schemas,
the forgiving (warn, not crash) unknown-capability-key contract that's
deliberately different from the drift gate below, and full worked examples.

### Freshness semantics

Each producer gets one entry in the `freshness` ledger of `data/capability-matrix.v1.json`:

- **`fresh`** — the source was pulled successfully and its age (commit date for repo files,
  workflow-run date for the samples artifact) is within that producer's threshold.
- **`stale`** — pulled successfully, but older than its threshold. Thresholds are the
  `DEFAULT_STALENESS_DAYS` dict at the top of `scripts/aggregate.py`; override any of them
  without editing the script via the `HONUA_EVIDENCE_STALENESS_JSON` env var (a JSON object,
  e.g. `{"samples": 5}`).
- **`missing`** — the pull failed outright (network error, missing/expired artifact, a
  pushed-envelope directory with no envelopes yet, or no `HONUA_EVIDENCE_TOKEN` for a
  producer that needs cross-repo auth). Never silently omitted; the site renders the
  ledger and every capability's SDK/sample/DR/canary sections say so plainly. An absent
  key in `freshness` (no entry at all) should be treated by gate consumers the same as
  `missing` — see `docs/producer-contracts.md`.

### How to add a producer

1. Add a `fetch_*` function in `scripts/aggregate.py` that returns a `Fetched` (data,
   `sourceVersion`, and an `error` on failure — never raise past the caller). For a
   pushed-envelope producer (files committed into `data/producers/<name>/` by an
   out-of-band job, rather than fetched over HTTP), see `docs/producer-contracts.md`'s
   "Adding a pushed-envelope producer" section instead — that path uses `join_local_producer`
   and a forgiving warn-not-crash contract for unknown keys, not the hard drift gate below.
2. Register its name in `DEFAULT_STALENESS_DAYS` and `SOURCE_REPO_LINKS` (build-site.py).
3. Join its per-capability keys into `capabilities_out` in `build_matrix()`, keyed to the
   canonical vocabulary. Any key it reports that isn't in `capability-keys.v1.json` fails the
   build — that's the drift gate; new keys land in honua-server first.
4. Add an entry to the `freshness` dict and to the producer table above.
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
python3 -m unittest discover -s tests   # unit tests (ingestion, freshness, drift/warning contracts)
```

Both scripts are Python 3 standard library only — no `pip install`, no `npm install`. Network
access is required for `aggregate.py` (it pulls from `raw.githubusercontent.com` and the GitHub
API); `build-site.py` is offline and only reads the already-aggregated JSON. The test suite is
`unittest`-only (no pytest dependency), but is pytest-discoverable too if pytest happens to be
installed.

## Status

Phase B (honua-io/honua-server#2892) landed the aggregation pipeline, freshness ledger, and the
core evidence index (issues [#1](https://github.com/honua-io/honua-evidence/issues/1),
[#3](https://github.com/honua-io/honua-evidence/issues/3), and the core of
[#2](https://github.com/honua-io/honua-evidence/issues/2)). Remaining scope — richer per-test/
per-run evidence detail, DNS cutover from honua-site, dispatch senders on producer repos, and
full per-capability gaps ingestion — stays open on those issues and on
[#5](https://github.com/honua-io/honua-evidence/issues/5).

[#8](https://github.com/honua-io/honua-evidence/issues/8) added CITE freshness
(timestamp + source sha) and two new pushed-envelope producers, DR drills and
live/canary results (contracts documented in
[`docs/producer-contracts.md`](docs/producer-contracts.md)). Both new
pushed-envelope producers report `missing` until honua-terraform and
honua-release (#61) actually start committing envelopes — this repo never
fabricates evidence for a producer that hasn't produced anything yet.

## License

Apache-2.0.
