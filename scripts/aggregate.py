#!/usr/bin/env python3
"""Phase-B capability-matrix aggregation (honua-io/honua-evidence#1, #3, #8).

This is the honua-evidence take-over of the aggregation job that ran inside
honua-server during Phase A (honua-server#2893, scripts/ci/generate-capability-matrix.py).
Phase B does NOT re-derive the server's per-capability counts: it PULLS the
already-published capability-matrix.v1.json from honua-server as one producer
snapshot among several, validates every capability key it sees against the
canonical vocabulary, and ENRICHES the result with:

  * per-capability SDK coverage (honua-sdk-js / -dotnet / -python)
  * per-capability executable-sample coverage (honua-samples CI artifact)
  * per-capability known-gaps preview (open honua-server issues, cap/* labels),
    joined at the individual capability-KEY level when an issue's advisory
    "Capability Key(s)" issue-form field parses to a canonical key, falling
    back to the coarser category-level attachment otherwise (issue #5)
  * CITE freshness (honua-server docs/cite-status.md "Last reviewed" date + sha)
  * cross-repo pushed-envelope producers: terraform DR drills and live/canary
    probe results (see docs/producer-contracts.md)
  * a per-producer freshness ledger (fetchedAt, sourceVersion, status)

Dependency-light by design: Python 3 standard library only (json, re, ssl,
urllib, zipfile, argparse, dataclasses). No pip install, no dotnet/npm build.
Cross-repo GitHub API calls (commit metadata, Actions artifacts) use a token
from the HONUA_EVIDENCE_TOKEN or GITHUB_TOKEN environment variable when
present; anonymous requests are used otherwise (subject to GitHub's lower
unauthenticated rate limit) and any producer that can't be reached is
recorded as "missing" rather than silently omitted or failing the run.

Honest degradation is a hard rule at BOTH granularities. Producer level: the
freshness ledger records "fresh" | "stale" | "missing" for every producer,
always. Per-capability level: when an SDK coverage snapshot could not be
fetched at all (e.g. honua-sdk-dotnet before its contracts/sdk-coverage.v1.json
first lands on trunk), each capability's `sdks.<sdk>` entry is the explicit
marker {"status": "producer-missing"} -- NEVER the fabricated coverage claim
{"status": "not-covered"}, which is reserved for a successfully fetched
snapshot that genuinely does not list the key. Likewise, when the samples
artifact is unavailable each capability's `samples` field is null (coverage
unknown), never [] (a positive "zero samples" claim). A stale-but-readable
snapshot keeps its real data and is flagged only in the ledger -- old evidence
is still evidence; fabricated evidence is not.

Unknown capability keys -- present in the honua-server matrix, an SDK
snapshot, or the samples artifact, but absent from honua-server's canonical
capability-keys.v1.json -- FAIL the build. This is the drift gate (issue #1's
acceptance criteria). Pushed-envelope producers (DR drills, live canary --
issue #8) get a DELIBERATELY different, more forgiving contract: an unknown
capability key referenced by an operator/automation-pushed envelope is a
WARNING (surfaced in the matrix's `ingestionWarnings` and printed visibly),
never a build failure -- a typo in a hand-authored evidence envelope must not
take down the whole aggregation pipeline. See docs/producer-contracts.md.

Same forgiving contract for the per-capability known-gaps join (issue #5):
honua-server's bug/feature/tech-debt issue forms carry an advisory, free-text
"Capability Key(s)" field (comma-separated, not validated at issue-creation
time). A token in that field that doesn't match honua-server's canonical
capability-keys.v1.json is a WARNING, never a build failure -- it's ordinary
human-authored issue-tracker text, not a producer contract.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "data" / "capability-matrix.v1.json"
SCHEMA_VERSION = "2.4.0"
USER_AGENT = "honua-evidence-aggregate/1 (+https://github.com/honua-io/honua-evidence)"

SERVER_KEYS_URL = "https://raw.githubusercontent.com/honua-io/honua-server/trunk/docs/gis/data/capability-keys.v1.json"
SERVER_MATRIX_URL = "https://raw.githubusercontent.com/honua-io/honua-server/trunk/docs/gis/data/capability-matrix.v1.json"
CITE_STATUS_URL = "https://raw.githubusercontent.com/honua-io/honua-server/trunk/docs/cite-status.md"
CITE_STATUS_PATH = "docs/cite-status.md"
SDK_URLS = {
    "js": "https://raw.githubusercontent.com/honua-io/honua-sdk-js/trunk/config/sdk-coverage.v1.json",
    "dotnet": "https://raw.githubusercontent.com/honua-io/honua-sdk-dotnet/trunk/contracts/sdk-coverage.v1.json",
    "python": "https://raw.githubusercontent.com/honua-io/honua-sdk-python/trunk/compatibility/sdk-coverage.v1.json",
}
SAMPLES_REPO = "honua-io/honua-samples"
SAMPLES_WORKFLOW = "run-samples.yml"
GAPS_REPO = "honua-io/honua-server"

# Pushed-envelope producers (honua-io/honua-evidence#8): unlike every producer
# above, these are not fetched over the network -- an out-of-band operator or
# automation job (honua-terraform's capture-dr-drill-evidence.sh, a future
# honua-release live-canary workflow) commits versioned JSON envelopes
# directly into these directories. Ingestion reads whatever is there; an empty
# directory is an honest "missing" producer, never fabricated evidence. See
# docs/producer-contracts.md for the envelope schemas.
DR_DRILLS_DIR = REPO_ROOT / "data" / "producers" / "dr-drills"
LIVE_CANARY_DIR = REPO_ROOT / "data" / "producers" / "live-canary"
DR_DRILL_REQUIRED_FIELDS = ("schema", "id", "capabilityKeys", "drill", "capturedAt", "verdict")
LIVE_CANARY_REQUIRED_FIELDS = ("schema", "manifestId", "targetEnvironment", "runAt", "probes")

# What each producer's freshness is COMPUTED FROM (honua-io/honua-release#89).
#
# Two different facts used to be conflated into one `status`. A producer's
# `sourceVersion` timestamp means one of two things, depending on the producer:
#
#   "observation"  the timestamp records when a run/capture/review actually
#                  HAPPENED -- a samples CI run, a CITE review, a canary run,
#                  a DR drill capture. That IS an observation, so it drives
#                  `status`: if the upstream job dies, the producer ages out
#                  and goes "stale", which is exactly what should happen.
#
#   "fetch"        the timestamp records when an upstream FILE last CHANGED (a
#                  git commit date). That is a fact about the upstream repo's
#                  release cadence, not about this pipeline's evidence -- the
#                  aggregator pulled the file successfully today, so the
#                  evidence IS current. Content age is kept as the purely
#                  informational `sourceAgeDays` field and can never set
#                  "stale" on its own; `status` is driven by this aggregator's
#                  own last successful `fetchedAt` instead.
#
# "fetch" is not a licence to never age out: a producer the aggregator can no
# longer read is "missing" (the fetch failed), and one it has not successfully
# read inside its threshold is "stale" (fetchedAt itself aged out). A dead
# upstream therefore still surfaces -- it just surfaces as the thing that
# actually broke.
#
# Unlisted producers default to "observation", the stricter basis: opting a
# producer into "fetch" is a deliberate, documented statement that its
# sourceVersion carries content, not observation. See docs/producer-contracts.md.
OBSERVATION_BASIS = "observation"
FETCH_BASIS = "fetch"
DEFAULT_FRESHNESS_BASIS = OBSERVATION_BASIS
FRESHNESS_BASIS = {
    # Upstream files pulled from another repo; sourceVersion is that file's
    # commit -- content, not observation.
    "server-keys": FETCH_BASIS,
    "server-matrix": FETCH_BASIS,
    "sdk-js": FETCH_BASIS,
    "sdk-dotnet": FETCH_BASIS,
    "sdk-python": FETCH_BASIS,
    # A live GitHub query with no artifact timestamp at all ("live-query"):
    # the fetch is the only observation there is.
    "open-issues": FETCH_BASIS,
    # A successful honua-samples CI run really did execute at that timestamp.
    "samples": OBSERVATION_BASIS,
    # cite-status.md's self-declared "Last reviewed" date is when the CITE
    # suite was actually reviewed -- an observation, and the signal
    # honua-release's evidence-freshness gate scores.
    "cite": OBSERVATION_BASIS,
    "dr-drills": OBSERVATION_BASIS,
    "live-canary": OBSERVATION_BASIS,
}

# Staleness thresholds (days) per producer, applied to whichever timestamp
# that producer's basis (above) says is its last observation: `fetchedAt` for
# "fetch"-basis producers, the sourceVersion timestamp for "observation"-basis
# ones. Configurable: override any entry via a JSON object in the
# HONUA_EVIDENCE_STALENESS_JSON env var, e.g. '{"samples": 5}'. A producer
# whose fetch fails outright is always "missing" regardless of these.
DEFAULT_STALENESS_DAYS = {
    # Fetch-basis producers: this is "how long may the aggregator have failed
    # to look at all". The aggregate job runs daily (cron, plus producer
    # dispatches), so 3 days tolerates two entirely missed runs before the row
    # ages out. It is deliberately NOT a statement about how often
    # honua-server or an SDK is expected to change -- that is sourceAgeDays,
    # which is informational.
    "server-keys": 3,
    "server-matrix": 3,
    "sdk-js": 3,
    "sdk-dotnet": 3,
    "sdk-python": 3,
    "open-issues": 3,
    # Observation-basis producers: this is "how long since the upstream job
    # last actually ran".
    "samples": 3,
    # CITE suite runs are expensive and not per-commit; 14 days matches both
    # honua-server's own scripts/ci/check-cite-status-freshness.sh default and
    # honua-release's certification/evidence-freshness.yaml `cite` threshold.
    "cite": 14,
    # DR drills run on an operator-driven cadence (backup-restore/failover
    # runbooks), not continuously; 45 days gives headroom between drills
    # without letting evidence go stale for a whole quarter.
    "dr-drills": 45,
    # Live canary/demo probes are meant to run on a daily-ish schedule
    # (honua-release#61); 3 days matches the "samples" producer's threshold
    # for the same reason -- a fast-moving, cheap-to-refresh producer.
    "live-canary": 3,
}
DEFAULT_THRESHOLD_DAYS = 14

# Pushed-envelope producers that are fully DEFINED (envelope schema documented,
# loader wired, capability join ready) but have never had a single envelope
# pushed. These get no freshness ledger row at all until their first envelope
# lands: a "missing" row that has ALWAYS been missing teaches nothing, and it
# dilutes "missing" for the case that matters -- a producer that used to report
# and then stopped. honua-io/honua-release#89.
#
# The row returns automatically on the first pushed envelope; the aggregate run
# prints a ::notice:: for every producer in this state, and the matrix declares
# them in its top-level `awaitingFirstEnvelope` array, so a row is dropped from
# the ledger without being hidden. This applies ONLY to producers listed here,
# so a producer that HAS reported can never quietly vanish from the ledger by
# having its directory emptied -- it goes "missing", loudly, as before.
#
#   dr-drills -- producer work tracked in honua-io/honua-evidence#20
#                (honua-iac DR drill emits honua-evidence.dr-drill-envelope/v1).
AWAITING_FIRST_ENVELOPE = {"dr-drills"}


def staleness_thresholds() -> dict[str, int]:
    thresholds = dict(DEFAULT_STALENESS_DAYS)
    override = os.environ.get("HONUA_EVIDENCE_STALENESS_JSON")
    if override:
        thresholds.update(json.loads(override))
    return thresholds


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_token() -> str | None:
    return os.environ.get("HONUA_EVIDENCE_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Cross-host redirects (GitHub -> blob storage) must drop the GitHub
    Authorization header; a presigned blob URL rejects the extra header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            new_req.remove_header("Authorization")
            new_req.remove_header("Accept")
        return new_req


_OPENER = urllib.request.build_opener(_StripAuthOnRedirect)


def http_get(url: str, *, api: bool = False, accept: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    if api:
        token = gh_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with _OPENER.open(req, timeout=30) as resp:
        return resp.read()


def http_get_json(url: str, *, api: bool = False) -> Any:
    accept = "application/vnd.github+json" if api else None
    return json.loads(http_get(url, api=api, accept=accept))


class Fetched:
    """Result of pulling one producer snapshot."""

    def __init__(self, name: str, data: Any = None, source_version: str | None = None,
                 fetched_at: str | None = None, error: str | None = None,
                 warnings: list[str] | None = None):
        self.name = name
        self.data = data
        self.source_version = source_version
        self.fetched_at = fetched_at or now_iso()
        self.error = error
        # Non-fatal ingestion problems (e.g. a malformed pushed envelope, or an
        # envelope referencing an unknown capability key). These never affect
        # `.ok`/the freshness ledger -- they are surfaced separately in the
        # matrix's `ingestionWarnings` and printed visibly. See issue #8.
        self.warnings = warnings or []

    @property
    def ok(self) -> bool:
        return self.error is None

    def ledger_entry(self, thresholds: dict[str, int]) -> dict[str, Any]:
        """One freshness ledger row. Two facts, two fields, neither pretending
        to be the other (honua-io/honua-release#89):

          * `ageDays`/`status` -- how long since this producer was last
            OBSERVED, per its FRESHNESS_BASIS. This is the only thing that can
            say "stale", i.e. "do not trust this".
          * `sourceAgeDays` -- how old the upstream artifact itself is. Purely
            informational: an upstream that simply has not changed in a while
            is a fact about its release cadence, not a defect in this evidence
            pipeline, and must never be reported as stale evidence.
        """
        if not self.ok:
            return {
                "fetchedAt": self.fetched_at,
                "sourceVersion": None,
                "status": "missing",
                "detail": self.error,
            }
        basis = FRESHNESS_BASIS.get(self.name, DEFAULT_FRESHNESS_BASIS)
        observed_at = self.fetched_at if basis == FETCH_BASIS else source_timestamp(self.source_version)
        age_days = age_in_days(observed_at)
        threshold = thresholds.get(self.name, DEFAULT_THRESHOLD_DAYS)
        status = "stale" if age_days is not None and age_days > threshold else "fresh"
        return {
            "fetchedAt": self.fetched_at,
            "sourceVersion": self.source_version,
            "freshnessBasis": basis,
            "observedAt": observed_at,
            "ageDays": age_days,
            "sourceAgeDays": commit_age_days(self.source_version),
            "status": status,
        }


_COMMIT_DATE_CACHE: dict[str, str] = {}


def source_timestamp(source_version: str | None) -> str | None:
    """The ISO 8601 timestamp encoded in a sourceVersion -- either
    'sha@ISO8601DATE' (our own encoding, cheap path) or a bare timestamp.
    None when the value carries no parseable timestamp at all (open-issues'
    'live-query', or a producer whose commit metadata lookup failed)."""
    if not source_version:
        return None
    candidate = source_version.split("@", 1)[-1]
    try:
        datetime.strptime(candidate, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return candidate


def age_in_days(timestamp: str | None) -> int | None:
    """Whole days between an ISO 8601 UTC timestamp and now, or None if it is
    absent/unparseable."""
    if not timestamp:
        return None
    try:
        dt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


def commit_age_days(source_version: str | None) -> int | None:
    """Age in whole days of the upstream artifact a sourceVersion points at.
    Informational only -- see Fetched.ledger_entry; this value never decides
    `status`."""
    return age_in_days(source_timestamp(source_version))


def fetch_raw_with_commit(name: str, raw_url: str, owner: str, repo: str, path: str, ref: str = "trunk") -> Fetched:
    try:
        data = json.loads(http_get(raw_url))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        return Fetched(name, error=f"fetch failed: {exc}")
    source_version = None
    try:
        commits = http_get_json(
            f"https://api.github.com/repos/{owner}/{repo}/commits?path={path}&sha={ref}&per_page=1",
            api=True,
        )
        if commits:
            sha = commits[0]["sha"][:12]
            date = commits[0]["commit"]["committer"]["date"]
            source_version = f"{sha}@{date}"
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, json.JSONDecodeError):
        pass  # commit metadata is best-effort; the snapshot itself still loaded.
    return Fetched(name, data=data, source_version=source_version)


def fetch_server_keys() -> Fetched:
    return fetch_raw_with_commit(
        "server-keys", SERVER_KEYS_URL, "honua-io", "honua-server", "docs/gis/data/capability-keys.v1.json"
    )


def fetch_server_matrix() -> Fetched:
    return fetch_raw_with_commit(
        "server-matrix", SERVER_MATRIX_URL, "honua-io", "honua-server", "docs/gis/data/capability-matrix.v1.json"
    )


CITE_LAST_REVIEWED_RE = re.compile(r"Last reviewed:\s*(\d{4}-\d{2}-\d{2})")


def fetch_cite_status() -> Fetched:
    """Pulls honua-server's docs/cite-status.md and parses its hand-maintained
    "Last reviewed: YYYY-MM-DD" line (same regex intent as honua-server's own
    scripts/ci/check-cite-status-freshness.sh) plus the file's current commit
    sha. honua-io/honua-evidence#8: this is what unblocks honua-release's
    evidence-freshness gate's `cite` producer, which was reporting `blocked`
    because this producer didn't exist in the freshness ledger at all.

    Deliberately: the ledger's `sourceVersion` pairs the file's commit sha with
    the *reviewed date the document itself claims* (not the commit's own
    date) -- that reviewed date is what actually reflects when the CITE suite
    last ran, which is the freshness signal that matters here.
    """
    try:
        text = http_get(CITE_STATUS_URL).decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, UnicodeDecodeError) as exc:
        return Fetched("cite", error=f"fetch failed: {exc}")

    match = CITE_LAST_REVIEWED_RE.search(text)
    if not match:
        return Fetched(
            "cite",
            error=f"{CITE_STATUS_PATH} has no parseable 'Last reviewed: YYYY-MM-DD' line",
        )
    last_reviewed = match.group(1)

    sha = None
    try:
        commits = http_get_json(
            f"https://api.github.com/repos/honua-io/honua-server/commits?path={CITE_STATUS_PATH}&sha=trunk&per_page=1",
            api=True,
        )
        if commits:
            sha = commits[0]["sha"][:12]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, IndexError, json.JSONDecodeError):
        pass  # commit metadata is best-effort; the reviewed date alone still drives freshness.

    source_version = f"{sha}@{last_reviewed}T00:00:00Z" if sha else f"unknown@{last_reviewed}T00:00:00Z"
    return Fetched(
        "cite",
        data={
            "lastReviewed": last_reviewed,
            "sourceSha": sha,
            "reportUrl": f"https://github.com/honua-io/honua-server/blob/trunk/{CITE_STATUS_PATH}",
        },
        source_version=source_version,
    )


def fetch_sdk(sdk: str) -> Fetched:
    url = SDK_URLS[sdk]
    path_by_sdk = {
        "js": "config/sdk-coverage.v1.json",
        "dotnet": "contracts/sdk-coverage.v1.json",
        "python": "compatibility/sdk-coverage.v1.json",
    }
    repo_by_sdk = {"js": "honua-sdk-js", "dotnet": "honua-sdk-dotnet", "python": "honua-sdk-python"}
    return fetch_raw_with_commit(
        f"sdk-{sdk}", url, "honua-io", repo_by_sdk[sdk], path_by_sdk[sdk]
    )


def fetch_samples() -> Fetched:
    try:
        runs = http_get_json(
            f"https://api.github.com/repos/{SAMPLES_REPO}/actions/workflows/{SAMPLES_WORKFLOW}/runs"
            "?branch=trunk&status=success&per_page=1",
            api=True,
        )
        workflow_runs = runs.get("workflow_runs") or []
        if not workflow_runs:
            return Fetched("samples", error="no successful run-samples run found on trunk")
        run = workflow_runs[0]
        artifacts = http_get_json(
            f"https://api.github.com/repos/{SAMPLES_REPO}/actions/runs/{run['id']}/artifacts", api=True
        )
        candidates = [a for a in artifacts.get("artifacts", []) if "samples-coverage" in a["name"]]
        if not candidates:
            return Fetched("samples", error=f"run {run['id']} has no samples-coverage artifact")
        artifact = candidates[0]
        if artifact.get("expired"):
            return Fetched("samples", error=f"artifact {artifact['name']} from run {run['id']} has expired")
        zip_bytes = http_get(artifact["archive_download_url"], api=True, accept="application/vnd.github+json")
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        json_names = [n for n in zf.namelist() if n.endswith(".json")]
        if not json_names:
            return Fetched("samples", error=f"artifact {artifact['name']} contained no JSON file")
        data = json.loads(zf.read(json_names[0]))
        source_version = f"{run['head_sha'][:12]}@{run['updated_at']}"
        return Fetched("samples", data=data, source_version=source_version)
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        return Fetched("samples", error=f"fetch failed: {exc}")


def _iter_envelope_files(dir_path: Path) -> list[Path]:
    if not dir_path.is_dir():
        return []
    return sorted(p for p in dir_path.glob("*.json") if p.is_file())


def _load_envelopes(name: str, dir_path: Path, required_fields: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    """Reads every *.json envelope committed under dir_path. A malformed or
    incomplete envelope is skipped and recorded as a warning; it never raises
    and never fails the build. This is the deliberately forgiving contract for
    pushed-envelope producers (honua-io/honua-evidence#8) -- unlike the
    network-pulled producers above, these files are hand-authored or written
    by an out-of-band automation job, so a single bad file must not take down
    the whole aggregation run."""
    envelopes: list[dict] = []
    warnings: list[str] = []
    for path in _iter_envelope_files(dir_path):
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            rel = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            warnings.append(f"{name}: {rel}: unreadable/invalid JSON, skipped ({exc})")
            continue
        if not isinstance(raw, dict):
            warnings.append(f"{name}: {rel}: envelope is not a JSON object, skipped")
            continue
        missing = [f for f in required_fields if f not in raw]
        if missing:
            warnings.append(f"{name}: {rel}: missing required field(s) {missing}, skipped")
            continue
        envelopes.append(raw)
    return envelopes, warnings


def fetch_dr_drills() -> Fetched:
    """Terraform DR drill evidence (honua-io/honua-evidence#8). honua-terraform
    owns DR by design (stateless server) and pushes one JSON envelope per
    drill run into data/producers/dr-drills/ (see docs/producer-contracts.md,
    which wraps honua-terraform's docs/devops/dr-evidence-template.json with a
    capabilityKeys join). No envelopes pushed yet -> honest "missing", not an
    error."""
    envelopes, warnings = _load_envelopes("dr-drills", DR_DRILLS_DIR, DR_DRILL_REQUIRED_FIELDS)
    valid: list[dict] = []
    for e in envelopes:
        keys = e.get("capabilityKeys")
        if isinstance(keys, list) and keys and all(isinstance(k, str) and k for k in keys):
            valid.append(e)
        else:
            warnings.append(
                f"dr-drills: envelope {e.get('id', '?')!r}: 'capabilityKeys' must be a non-empty list of strings, skipped"
            )

    fetched = Fetched("dr-drills", data=valid, warnings=warnings)
    if not valid:
        fetched.error = "no DR drill evidence envelopes found under data/producers/dr-drills/ (none pushed yet)"
        return fetched

    latest = max(valid, key=lambda e: e["capturedAt"])
    sha = latest.get("sourceRef")
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{6,40}", sha):
        fetched.source_version = f"{sha[:12]}@{latest['capturedAt']}"
    else:
        fetched.source_version = latest["capturedAt"]
    return fetched


def fetch_live_canary() -> Fetched:
    """Live/deployed-environment canary and cloud e2e results (honua-io/
    honua-evidence#8). honua-release's scheduled demo canary / cloud e2e
    workflow pushes one manifest envelope per run into
    data/producers/live-canary/ (see docs/producer-contracts.md)."""
    envelopes, warnings = _load_envelopes("live-canary", LIVE_CANARY_DIR, LIVE_CANARY_REQUIRED_FIELDS)
    valid: list[dict] = []
    for e in envelopes:
        probes = e.get("probes")
        if isinstance(probes, list) and all(isinstance(p, dict) for p in probes):
            valid.append(e)
        else:
            warnings.append(
                f"live-canary: manifest {e.get('manifestId', '?')!r}: 'probes' must be a list of objects, skipped"
            )

    fetched = Fetched("live-canary", data=valid, warnings=warnings)
    if not valid:
        fetched.error = "no live-canary evidence envelopes found under data/producers/live-canary/ (none pushed yet)"
        return fetched

    latest = max(valid, key=lambda e: e["runAt"])
    sha = latest.get("sourceRef")
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{6,40}", sha):
        fetched.source_version = f"{sha[:12]}@{latest['runAt']}"
    else:
        fetched.source_version = latest["runAt"]
    return fetched


def _dr_drill_items(envelope: dict) -> list[tuple[str, dict]]:
    summary = {
        "id": envelope["id"],
        "drill": envelope["drill"],
        "cloud": envelope.get("cloud"),
        "target": envelope.get("target"),
        "environment": envelope.get("environment"),
        "capturedAt": envelope["capturedAt"],
        "verdict": envelope["verdict"],
        "sourceRunUrl": envelope.get("sourceRunUrl"),
    }
    return [(key, summary) for key in envelope["capabilityKeys"]]


def _live_canary_items(envelope: dict) -> tuple[list[tuple[str, dict]], list[str]]:
    items: list[tuple[str, dict]] = []
    warnings: list[str] = []
    for probe in envelope.get("probes", []):
        keys = probe.get("capabilityKeys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k for k in keys):
            warnings.append(
                f"live-canary: manifest {envelope.get('manifestId', '?')!r}: probe "
                f"{probe.get('probeName', '?')!r} has no valid 'capabilityKeys' (must be a "
                "non-empty list of strings), skipped"
            )
            continue
        summary = {
            "manifestId": envelope["manifestId"],
            "probeName": probe.get("probeName"),
            "targetEnvironment": envelope["targetEnvironment"],
            "status": probe.get("status"),
            "lastGreenAt": probe.get("lastGreenAt"),
            "sourceRunUrl": envelope.get("sourceRunUrl"),
        }
        items.extend((key, summary) for key in keys)
    return items, warnings


def join_local_producer(
    name: str, envelopes: list[dict], canonical_keys: set[str],
    extract_items,
) -> tuple[dict[str, list[dict]], list[str]]:
    """Joins a list of pushed-envelope producer records onto canonical
    capability keys. An envelope referencing a capability key absent from the
    canonical vocabulary is a WARNING, never a build failure -- this is the
    forgiving contract issue #8 defines for pushed-envelope producers,
    distinct from the hard drift gate applied to server-matrix/SDK/samples
    below."""
    by_key: dict[str, list[dict]] = {}
    warnings: list[str] = []
    for envelope in envelopes:
        result = extract_items(envelope)
        items, extra_warnings = result if isinstance(result, tuple) else (result, [])
        warnings.extend(extra_warnings)
        for key, summary in items:
            if key not in canonical_keys:
                warnings.append(
                    f"{name}: envelope references unknown capability key {key!r} (not in "
                    "honua-server's canonical capability-keys.v1.json) -- skipped"
                )
                continue
            by_key.setdefault(key, []).append(summary)
    return by_key, warnings


def category_to_label_slug(category: str) -> str:
    """PascalCase capability category -> 'cap/<kebab-case>' label slug.
    'ControlPlane' -> 'control-plane', 'AI' -> 'ai', 'FieldOps' -> 'field-ops'."""
    kebab = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", category).lower()
    return kebab


def fetch_open_issues(categories: list[str]) -> Fetched:
    token = gh_token()
    if not token:
        return Fetched("open-issues", error="no GitHub token available for issue lookup (set HONUA_EVIDENCE_TOKEN)")
    by_category: dict[str, list[dict[str, Any]]] = {}
    errors = []
    for category in categories:
        slug = category_to_label_slug(category)
        label = f"cap/{slug}"
        try:
            # The plain issues-list endpoint (core rate limit: 5000/hr) is
            # used deliberately instead of /search/issues (30/min secondary
            # limit) -- one query per capability category quickly exhausts
            # the search quota and produced flaky, silently-truncated
            # results when this used /search/issues. The list response
            # already includes each issue's full body (no extra per-issue
            # call needed) -- that body is where issue #5's per-key join
            # parses the advisory "Capability Key(s)" field from.
            issues = http_get_json(
                f"https://api.github.com/repos/{GAPS_REPO}/issues"
                f"?labels={label}&state=open&per_page=10&sort=created&direction=asc",
                api=True,
            )
            by_category[category] = [
                {
                    "number": it["number"],
                    "title": it["title"],
                    "url": it["html_url"],
                    "body": it.get("body") or "",
                }
                for it in issues
                if "pull_request" not in it
            ]
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError) as exc:
            errors.append(f"{label}: {exc}")
            by_category[category] = []
    if errors and len(errors) == len(categories):
        return Fetched("open-issues", error="; ".join(errors))
    # No natural "version" for a live query; freshness for this producer is
    # judged purely by fetchedAt recency (always "fresh" at build time),
    # not by a commit/artifact age.
    return Fetched("open-issues", data=by_category, source_version="live-query")


# --- per-capability-key gap join (honua-io/honua-evidence#5) ---------------
#
# honua-server's bug.yml/feature.yml/tech-debt.yml issue forms all carry an
# `id: capability-keys` field (label text varies slightly -- "Capability
# Key(s)" on bug/feature, "Capability Key(s) (optional)" on tech-debt) that
# authors may fill with a comma-separated list of capability keys. Two shapes
# are observed in real honua-server issues:
#
#  1. The GitHub issue-form rendering of that field, e.g.:
#         ### Capability Key(s)
#
#         editing.feature-edits, geocoding.single-line
#     (or the literal text "_No response_" if the optional field was left
#     blank -- GitHub's own placeholder for an unanswered optional field).
#  2. A free-text inline mention embedded in a hand-written or scripted issue
#     body (this is what every real cap/*-labeled honua-server issue found
#     while building this join actually used, form rendering included, e.g.):
#         Capability key(s): `serve.wms`, `serve.wmts` (aggregate gap).
#
# Both are handled by locating the "capability key(s)" mention and either
# reading the rest of its own line (shape 2, and shape 1 when the field ended
# up on one line) or, if that's blank, the next non-blank line (shape 1's
# separate value line).
CAPABILITY_KEYS_FIELD_RE = re.compile(
    r"^[ \t]{0,3}#{0,6}[ \t]*capability key\(s\)(?:[ \t]*\(optional\))?[ \t]*:?[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_CAPABILITY_KEY_BACKTICK_RE = re.compile(r"`([^`]+)`")
_NO_RESPONSE_VALUES = {"_no response_", "n/a", "none", "-"}


def _capability_keys_field_value(body: str) -> str | None:
    """Returns the raw (unvalidated) text of the 'Capability Key(s)' field in
    an issue body, or None if the field isn't present at all."""
    match = CAPABILITY_KEYS_FIELD_RE.search(body)
    if match is None:
        return None
    same_line = match.group(1).strip()
    if same_line:
        return same_line
    # Header-only line (issue-form rendering): the value is the next
    # non-blank line, unless we run into the next field's header first.
    for line in body[match.end():].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return None
        return stripped
    return None


def _clean_capability_key_token(token: str) -> str:
    token = token.strip().strip("`").strip(" .,;:()")
    # Defends against inline prose that didn't backtick-quote its keys (a
    # capability key is never itself whitespace-bearing): keep only the
    # first whitespace-delimited word.
    token = re.split(r"\s", token, maxsplit=1)[0]
    return token.strip(" .,;:()")


def _capability_key_tokens(raw_value: str) -> list[str]:
    if not raw_value or raw_value.strip().lower() in _NO_RESPONSE_VALUES:
        return []
    # Trailing explanatory prose in real issues is always parenthetical and
    # comes after every actual key token (see module docstring's real-world
    # example) -- truncate there so a backtick-quoted filename/reference
    # mentioned only in that prose (e.g. "`capability-keys.v1.json`") isn't
    # mistaken for a capability key.
    value = raw_value.split("(", 1)[0]
    spans = _CAPABILITY_KEY_BACKTICK_RE.findall(value)
    # Backtick-quoted keys are treated as the ground truth when present --
    # real issues embed explanatory prose outside the backticks (see module
    # docstring). Otherwise the whole value is a plain comma-separated list
    # (the clean issue-form rendering).
    source = spans if spans else [value]
    tokens: list[str] = []
    for span in source:
        for token in span.split(","):
            cleaned = _clean_capability_key_token(token)
            if cleaned:
                tokens.append(cleaned)
    return tokens


def parse_issue_capability_keys(body: str, canonical_keys: set[str]) -> tuple[list[str], list[str]]:
    """Parses honua-server's advisory 'Capability Key(s)' issue-form field out
    of an issue body and validates each comma-separated token against the
    canonical capability vocabulary. Returns (valid_keys, invalid_tokens):
    valid_keys is de-duplicated and order-preserving; invalid_tokens is every
    parsed token absent from canonical_keys, for warning/logging -- an
    unrecognized token here is never fatal, this field is advisory human-
    authored issue-tracker text, not a producer contract."""
    raw_value = _capability_keys_field_value(body or "")
    if raw_value is None:
        return [], []
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for token in _capability_key_tokens(raw_value):
        if token in canonical_keys:
            if token not in seen:
                valid.append(token)
                seen.add(token)
        else:
            invalid.append(token)
    return valid, invalid


def join_gaps_by_key(
    by_category: dict[str, list[dict[str, Any]]], canonical_keys: set[str]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], list[str]]:
    """Splits each category's open honua-server issues into a per-capability-
    KEY join (issues whose advisory 'Capability Key(s)' field parses to at
    least one canonical key) and a category-level fallback (every other
    issue in that category: field absent, empty/"_No response_", or every
    parsed token unrecognized). An issue with a mix of valid and invalid
    tokens joins only on its valid keys -- it is NOT also added to the
    category fallback, since it does carry usable per-capability signal.
    honua-io/honua-evidence#5."""
    gaps_by_key: dict[str, list[dict[str, Any]]] = {}
    gaps_by_category: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for category, issues in by_category.items():
        for issue in issues:
            ref = {"number": issue["number"], "title": issue["title"], "url": issue["url"]}
            valid_keys, invalid_tokens = parse_issue_capability_keys(issue.get("body") or "", canonical_keys)
            for token in invalid_tokens:
                warnings.append(
                    f"open-issues: {GAPS_REPO}#{issue['number']}: 'Capability Key(s)' field references "
                    f"unknown capability key {token!r} (not in honua-server's canonical "
                    "capability-keys.v1.json) -- ignored"
                )
            if valid_keys:
                for key in valid_keys:
                    gaps_by_key.setdefault(key, []).append(ref)
            else:
                gaps_by_category.setdefault(category, []).append(ref)
    return gaps_by_key, gaps_by_category, warnings


def normalize_sdk_capabilities(sdk_name: str, raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """honua-sdk-js/-dotnet/-python each name their per-capability list
    slightly differently ('capabilities' vs 'coverage') and carry slightly
    different optional fields. Normalize to {key: {status, sinceVersion,
    entrypoints, evidence?, note?}}."""
    entries = raw.get("capabilities") or raw.get("coverage") or []
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = entry["key"]
        normalized = {
            "status": entry.get("status"),
            "sinceVersion": entry.get("sinceVersion"),
        }
        if entry.get("entrypoints"):
            normalized["entrypoints"] = entry["entrypoints"]
        if entry.get("evidence"):
            normalized["evidence"] = entry["evidence"]
        if entry.get("note"):
            normalized["note"] = entry["note"]
        out[key] = normalized
    return out


def awaiting_first_envelope(fetches: dict[str, "Fetched"]) -> list[str]:
    """Producers in AWAITING_FIRST_ENVELOPE that still have no envelope at all.
    Reported as a ::notice:: by main() so dropping their ledger row is visible
    rather than silent."""
    return sorted(
        name for name, fetched in fetches.items()
        if name in AWAITING_FIRST_ENVELOPE and not fetched.data
    )


def build_freshness_ledger(fetches: dict[str, "Fetched"], thresholds: dict[str, int]) -> dict[str, Any]:
    """The matrix's `freshness` block: exactly one row per producer that has
    ever produced anything.

    A producer listed in AWAITING_FIRST_ENVELOPE that has never had an envelope
    pushed gets no row -- see that constant for why. Every other producer,
    including one that has gone genuinely dark, always gets a row
    (`missing`/`stale`); nothing else is ever omitted."""
    omit = set(awaiting_first_envelope(fetches))
    return {name: fetched.ledger_entry(thresholds) for name, fetched in fetches.items() if name not in omit}


def build_matrix(*, staleness: dict[str, int]) -> tuple[dict[str, Any], list[str]]:
    """Returns (matrix, unknown_keys). unknown_keys is empty on a clean run;
    a non-empty list means the drift gate should fail the build."""

    server_keys = fetch_server_keys()
    server_matrix = fetch_server_matrix()
    sdk_fetches = {sdk: fetch_sdk(sdk) for sdk in SDK_URLS}
    samples = fetch_samples()
    cite = fetch_cite_status()
    dr_drills = fetch_dr_drills()
    live_canary = fetch_live_canary()

    if not server_keys.ok:
        raise SystemExit(f"::error::cannot proceed without the canonical capability key list: {server_keys.error}")

    canonical = server_keys.data["capabilities"]
    canonical_by_key = {c["key"]: c for c in canonical}
    canonical_keys = set(canonical_by_key)
    categories = sorted({c["category"] for c in canonical})

    open_issues = fetch_open_issues(categories)

    server_matrix_by_key: dict[str, Any] = {}
    unjoined_cite_suites: list[str] = []
    if server_matrix.ok:
        server_matrix_by_key = {c["key"]: c for c in server_matrix.data.get("capabilities", [])}
        unjoined_cite_suites = server_matrix.data.get("unjoinedCiteSuites", [])

    normalized_sdks: dict[str, dict[str, dict[str, Any]]] = {}
    for sdk, fetched in sdk_fetches.items():
        if fetched.ok:
            normalized_sdks[sdk] = normalize_sdk_capabilities(sdk, fetched.data)
        else:
            normalized_sdks[sdk] = {}

    samples_by_key: dict[str, list[dict[str, Any]]] = {}
    if samples.ok:
        samples_by_key = samples.data.get("capabilities", {})

    # --- drift gate: collect every capability key referenced by any producer
    # and diff against the canonical vocabulary. ---
    unknown_keys: set[str] = set()
    if server_matrix.ok:
        unknown_keys |= {k for k in server_matrix_by_key if k not in canonical_keys}
    for sdk_data in normalized_sdks.values():
        unknown_keys |= {k for k in sdk_data if k not in canonical_keys}
    if samples.ok:
        unknown_keys |= {k for k in samples_by_key if k not in canonical_keys}

    # --- pushed-envelope producers (issue #8): joined onto canonical keys with
    # a forgiving warn-not-crash contract for unknown keys, deliberately NOT
    # folded into the hard drift gate above. ---
    dr_by_key, dr_warnings = join_local_producer("dr-drills", dr_drills.data or [], canonical_keys, _dr_drill_items)
    live_by_key, live_warnings = join_local_producer(
        "live-canary", live_canary.data or [], canonical_keys, _live_canary_items
    )

    # --- known-gaps join (issue #5): per-capability-KEY where an open issue's
    # advisory 'Capability Key(s)' field parses to a canonical key, with a
    # category-level fallback for issues that don't carry that signal. An
    # unrecognized token in the field is a warning, not a drift-gate failure
    # -- see module docstring. ---
    gaps_by_key: dict[str, list[dict[str, Any]]] = {}
    gaps_by_category: dict[str, list[dict[str, Any]]] = {}
    gap_warnings: list[str] = []
    if open_issues.ok:
        gaps_by_key, gaps_by_category, gap_warnings = join_gaps_by_key(open_issues.data, canonical_keys)

    ingestion_warnings = sorted(
        set(dr_drills.warnings) | set(live_canary.warnings) | set(dr_warnings) | set(live_warnings) | set(gap_warnings)
    )

    capabilities_out = []
    for key in sorted(canonical_keys):
        canon = canonical_by_key[key]
        base = server_matrix_by_key.get(key, {})
        sdks_entry = {}
        for sdk in SDK_URLS:
            if not sdk_fetches[sdk].ok:
                # The snapshot itself could not be fetched (repo/file absent,
                # network failure): degrade to an explicit producer-missing
                # marker. "not-covered" is a positive coverage claim reserved
                # for a snapshot that loaded and genuinely omits this key --
                # emitting it here would fabricate evidence (e.g. for
                # honua-sdk-dotnet before contracts/sdk-coverage.v1.json first
                # lands on its trunk).
                sdks_entry[sdk] = {"status": "producer-missing"}
                continue
            sdk_cov = normalized_sdks.get(sdk, {}).get(key)
            sdks_entry[sdk] = sdk_cov if sdk_cov is not None else {"status": "not-covered"}

        key_gaps = gaps_by_key.get(key, []) if open_issues.ok else []
        category_gaps = gaps_by_category.get(canon["category"], []) if open_issues.ok else []
        effective_gaps = key_gaps if key_gaps else category_gaps

        capabilities_out.append(
            {
                "key": key,
                "displayName": canon["displayName"],
                "category": canon["category"],
                "edition": canon["edition"],
                "entryCount": base.get("entryCount", 0),
                "provingTestCount": base.get("provingTestCount", 0),
                "maturity": base.get("maturity", {}),
                "noSurface": base.get("noSurface"),
                "cite": base.get("cite", []),
                "parity": base.get("parity", []),
                "esriAssess": base.get("esriAssess", []),
                "interop": base.get("interop", []),
                "geobench": base.get("geobench", []),
                "dr": dr_by_key.get(key, []),
                "liveCanary": live_by_key.get(key, []),
                "sdks": sdks_entry,
                # null = samples producer snapshot unavailable this run
                # (coverage unknown); [] = artifact fetched and genuinely
                # lists no sample for this key. Never conflate the two.
                "samples": samples_by_key.get(key, []) if samples.ok else None,
                "openIssues": {
                    "count": len(effective_gaps),
                    "refs": effective_gaps,
                    # True when 'refs' above fell back to the category-wide
                    # attachment (no open issue parsed a key-level match for
                    # this specific capability); False when 'refs' is the
                    # finer-grained per-key join. 'keyRefs'/'categoryRefs'
                    # below always carry both, independent of the fallback.
                    "categoryLevel": not bool(key_gaps),
                    "keyRefs": key_gaps,
                    "categoryRefs": category_gaps,
                    "label": f"cap/{category_to_label_slug(canon['category'])}",
                },
            }
        )

    fetches = {
            "server-keys": server_keys,
            "server-matrix": server_matrix,
            "sdk-js": sdk_fetches["js"],
            "sdk-dotnet": sdk_fetches["dotnet"],
            "sdk-python": sdk_fetches["python"],
            "samples": samples,
            "open-issues": open_issues,
            "cite": cite,
            "dr-drills": dr_drills,
            "live-canary": live_canary,
    }
    freshness = build_freshness_ledger(fetches, staleness)
    awaiting = awaiting_first_envelope(fetches)

    matrix = {
        "schemaVersion": SCHEMA_VERSION,
        "generator": "scripts/aggregate.py",
        "trackingIssue": "honua-io/honua-evidence#1",
        "generatedAt": now_iso(),
        "description": (
            "Phase-B enriched capability evidence matrix. Ingests honua-server's "
            "Phase-A capability-matrix.v1.json as a producer snapshot (does not "
            "re-derive it) and joins SDK coverage, executable-sample coverage, "
            "a known-gaps preview, CITE freshness, and pushed-envelope cross-repo "
            "evidence (terraform DR drills, live/canary probes -- issue #8), "
            "keyed to honua-server's canonical capability vocabulary. See "
            "freshness for per-producer pull status and docs/producer-contracts.md "
            "for the pushed-envelope schemas."
        ),
        "sourceArtifacts": {
            "server-keys": SERVER_KEYS_URL,
            "server-matrix": SERVER_MATRIX_URL,
            "sdk-js": SDK_URLS["js"],
            "sdk-dotnet": SDK_URLS["dotnet"],
            "sdk-python": SDK_URLS["python"],
            "samples": f"https://github.com/{SAMPLES_REPO}/actions/workflows/{SAMPLES_WORKFLOW}",
            "open-issues": f"https://github.com/{GAPS_REPO}/issues?q=is%3Aopen+label%3Acap%2F%2A",
            "cite": f"https://github.com/honua-io/honua-server/blob/trunk/{CITE_STATUS_PATH}",
            "dr-drills": "data/producers/dr-drills/ (pushed envelopes; see docs/producer-contracts.md)",
            "live-canary": "data/producers/live-canary/ (pushed envelopes; see docs/producer-contracts.md)",
        },
        "unjoinedCiteSuites": unjoined_cite_suites,
        # Pushed-envelope producers that are defined but have never produced an
        # envelope, and therefore carry no `freshness` row (see
        # AWAITING_FIRST_ENVELOPE and docs/producer-contracts.md). Declared
        # here so "not built yet" stays visible and distinguishable from
        # "reported once and then stopped", which is what `missing` means.
        "awaitingFirstEnvelope": awaiting,
        "freshness": freshness,
        "ingestionWarnings": ingestion_warnings,
        "capabilities": capabilities_out,
    }
    return matrix, sorted(unknown_keys)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output path for capability-matrix.v1.json")
    parser.add_argument(
        "--check", action="store_true",
        help="Exit non-zero if the generated output differs from --output's current contents (drift gate).",
    )
    parser.add_argument(
        "--inject-unknown-key", metavar="KEY", default=None,
        help="Test hook: pretend a producer referenced this extra capability key, to exercise the drift gate.",
    )
    args = parser.parse_args()

    staleness = staleness_thresholds()
    matrix, unknown_keys = build_matrix(staleness=staleness)

    if args.inject_unknown_key:
        unknown_keys = sorted(set(unknown_keys) | {args.inject_unknown_key})
        matrix.setdefault("_driftGateTestInjection", []).append(args.inject_unknown_key)

    if unknown_keys:
        print("::error::Unknown capability key(s) referenced by a producer but absent from the canonical vocabulary:",
              file=sys.stderr)
        for key in unknown_keys:
            print(f"::error::  - {key}", file=sys.stderr)
        print(
            "::error::Canonical vocabulary is https://github.com/honua-io/honua-server "
            "docs/gis/data/capability-keys.v1.json. Fix the producer or, if the key is "
            "genuinely new, land it there first.",
            file=sys.stderr,
        )
        return 1

    for warning in matrix.get("ingestionWarnings", []):
        print(f"::warning::{warning}", file=sys.stderr)

    rendered = json.dumps(matrix, indent=2, sort_keys=False) + "\n"

    if args.check:
        if not args.output.exists():
            print(f"::error::{args.output} does not exist; run without --check to generate it.", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        # generatedAt/freshness necessarily churn between runs; compare
        # everything else so --check catches structural/content drift
        # without permanently failing simply because time passed.
        committed = json.loads(current)
        # Structural gate (fails PRs): every capability key in the committed
        # aggregate must exist in the canonical vocabulary. Unknown keys from
        # live producers already failed during build_matrix above.
        canonical = {c["key"] for c in matrix["capabilities"]}
        committed_keys = {c["key"] for c in committed.get("capabilities", [])}
        unknown_committed = sorted(committed_keys - canonical)
        if unknown_committed:
            print(
                f"::error::{args.output} contains keys absent from the canonical vocabulary: "
                + ", ".join(unknown_committed),
                file=sys.stderr,
            )
            return 1
        # Content drift (does NOT fail PRs): producers move constantly; content
        # refresh is the scheduled aggregate job's responsibility, which
        # regenerates and commits. Failing PRs on upstream motion would make
        # every producer merge break this repo's checks.
        current_stable = strip_volatile(committed)
        rendered_stable = strip_volatile(matrix)
        if current_stable != rendered_stable:
            print(
                f"::notice::{args.output} differs from current producer snapshots; the "
                "scheduled aggregate run will refresh it. (Not a PR failure.)"
            )
        else:
            print(f"{args.output} is up to date ({len(matrix['capabilities'])} capabilities).")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"Wrote {args.output} ({len(matrix['capabilities'])} capabilities).")
    for name in matrix.get("awaitingFirstEnvelope", []):
        print(
            f"::notice::producer {name} has never had an evidence envelope pushed; it carries no "
            "freshness ledger row until it does (see docs/producer-contracts.md).",
            file=sys.stderr,
        )
    for name, entry in matrix["freshness"].items():
        detail = f" ({entry['detail']})" if entry.get("detail") else ""
        source_age = entry.get("sourceAgeDays")
        source_note = "" if source_age is None else f", source {source_age}d old"
        print(f"  producer {name}: {entry['status']}{detail}"
              f" [basis={entry.get('freshnessBasis', '-')}{source_note}]")
    return 0


def strip_volatile(matrix: dict[str, Any]) -> dict[str, Any]:
    clone = json.loads(json.dumps(matrix))
    clone.pop("generatedAt", None)
    for entry in clone.get("freshness", {}).values():
        # Every now-derived field: these necessarily churn between runs, so
        # comparing them would make --check permanently "drifted".
        entry.pop("fetchedAt", None)
        entry.pop("observedAt", None)
        entry.pop("ageDays", None)
        entry.pop("sourceAgeDays", None)
    return clone


if __name__ == "__main__":
    raise SystemExit(main())
