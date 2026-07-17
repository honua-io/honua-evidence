#!/usr/bin/env python3
"""Phase-B capability-matrix aggregation (honua-io/honua-evidence#1, #3).

This is the honua-evidence take-over of the aggregation job that ran inside
honua-server during Phase A (honua-server#2893, scripts/ci/generate-capability-matrix.py).
Phase B does NOT re-derive the server's per-capability counts: it PULLS the
already-published capability-matrix.v1.json from honua-server as one producer
snapshot among several, validates every capability key it sees against the
canonical vocabulary, and ENRICHES the result with:

  * per-capability SDK coverage (honua-sdk-js / -dotnet / -python)
  * per-capability executable-sample coverage (honua-samples CI artifact)
  * per-capability known-gaps preview (open honua-server issues, cap/* labels)
  * a per-producer freshness ledger (fetchedAt, sourceVersion, status)

Dependency-light by design: Python 3 standard library only (json, re, ssl,
urllib, zipfile, argparse, dataclasses). No pip install, no dotnet/npm build.
Cross-repo GitHub API calls (commit metadata, Actions artifacts) use a token
from the HONUA_EVIDENCE_TOKEN or GITHUB_TOKEN environment variable when
present; anonymous requests are used otherwise (subject to GitHub's lower
unauthenticated rate limit) and any producer that can't be reached is
recorded as "missing" rather than silently omitted or failing the run.

Unknown capability keys -- present in any producer snapshot but absent from
honua-server's canonical capability-keys.v1.json -- FAIL the build. This is
the drift gate (issue #1's acceptance criteria).
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
SCHEMA_VERSION = "2.0.0"
USER_AGENT = "honua-evidence-aggregate/1 (+https://github.com/honua-io/honua-evidence)"

SERVER_KEYS_URL = "https://raw.githubusercontent.com/honua-io/honua-server/trunk/docs/gis/data/capability-keys.v1.json"
SERVER_MATRIX_URL = "https://raw.githubusercontent.com/honua-io/honua-server/trunk/docs/gis/data/capability-matrix.v1.json"
SDK_URLS = {
    "js": "https://raw.githubusercontent.com/honua-io/honua-sdk-js/trunk/config/sdk-coverage.v1.json",
    "dotnet": "https://raw.githubusercontent.com/honua-io/honua-sdk-dotnet/trunk/contracts/sdk-coverage.v1.json",
    "python": "https://raw.githubusercontent.com/honua-io/honua-sdk-python/trunk/compatibility/sdk-coverage.v1.json",
}
SAMPLES_REPO = "honua-io/honua-samples"
SAMPLES_WORKFLOW = "run-samples.yml"
GAPS_REPO = "honua-io/honua-server"

# Staleness thresholds (days) per producer. Configurable: override any entry
# via a JSON object in the HONUA_EVIDENCE_STALENESS_JSON env var, e.g.
# '{"samples": 5}'. A producer whose fetch fails outright is always "missing"
# regardless of these thresholds.
DEFAULT_STALENESS_DAYS = {
    "server-keys": 14,
    "server-matrix": 14,
    "sdk-js": 30,
    "sdk-dotnet": 30,
    "sdk-python": 30,
    "samples": 3,
    "open-issues": 7,
}


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
                 fetched_at: str | None = None, error: str | None = None):
        self.name = name
        self.data = data
        self.source_version = source_version
        self.fetched_at = fetched_at or now_iso()
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None

    def ledger_entry(self, thresholds: dict[str, int]) -> dict[str, Any]:
        if not self.ok:
            return {
                "fetchedAt": self.fetched_at,
                "sourceVersion": None,
                "status": "missing",
                "detail": self.error,
            }
        status = "fresh"
        age_days = None
        if self.source_version:
            age_days = commit_age_days(self.source_version)
        threshold = thresholds.get(self.name, 14)
        if age_days is not None and age_days > threshold:
            status = "stale"
        return {
            "fetchedAt": self.fetched_at,
            "sourceVersion": self.source_version,
            "ageDays": age_days,
            "status": status,
        }


_COMMIT_DATE_CACHE: dict[str, str] = {}


def commit_age_days(source_version: str) -> int | None:
    """source_version is either 'sha@ISO8601DATE' (our own encoding, cheap
    path) or a bare timestamp. Returns whole days since that date, or None
    if it can't be parsed."""
    date_str = source_version.split("@", 1)[-1]
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


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
            # results when this used /search/issues.
            issues = http_get_json(
                f"https://api.github.com/repos/{GAPS_REPO}/issues"
                f"?labels={label}&state=open&per_page=10&sort=created&direction=asc",
                api=True,
            )
            by_category[category] = [
                {"number": it["number"], "title": it["title"], "url": it["html_url"]}
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


def build_matrix(*, staleness: dict[str, int]) -> tuple[dict[str, Any], list[str]]:
    """Returns (matrix, unknown_keys). unknown_keys is empty on a clean run;
    a non-empty list means the drift gate should fail the build."""

    server_keys = fetch_server_keys()
    server_matrix = fetch_server_matrix()
    sdk_fetches = {sdk: fetch_sdk(sdk) for sdk in SDK_URLS}
    samples = fetch_samples()

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

    capabilities_out = []
    for key in sorted(canonical_keys):
        canon = canonical_by_key[key]
        base = server_matrix_by_key.get(key, {})
        sdks_entry = {}
        for sdk in SDK_URLS:
            sdk_cov = normalized_sdks.get(sdk, {}).get(key)
            sdks_entry[sdk] = sdk_cov if sdk_cov is not None else {"status": "not-covered"}

        gaps = open_issues.data.get(canon["category"], []) if open_issues.ok else []

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
                "sdks": sdks_entry,
                "samples": samples_by_key.get(key, []),
                "openIssues": {
                    "count": len(gaps),
                    "refs": gaps,
                    "categoryLevel": True,
                    "label": f"cap/{category_to_label_slug(canon['category'])}",
                },
            }
        )

    freshness = {
        "server-keys": server_keys.ledger_entry(staleness),
        "server-matrix": server_matrix.ledger_entry(staleness),
        "sdk-js": sdk_fetches["js"].ledger_entry(staleness),
        "sdk-dotnet": sdk_fetches["dotnet"].ledger_entry(staleness),
        "sdk-python": sdk_fetches["python"].ledger_entry(staleness),
        "samples": samples.ledger_entry(staleness),
        "open-issues": open_issues.ledger_entry(staleness),
    }

    matrix = {
        "schemaVersion": SCHEMA_VERSION,
        "generator": "scripts/aggregate.py",
        "trackingIssue": "honua-io/honua-evidence#1",
        "generatedAt": now_iso(),
        "description": (
            "Phase-B enriched capability evidence matrix. Ingests honua-server's "
            "Phase-A capability-matrix.v1.json as a producer snapshot (does not "
            "re-derive it) and joins SDK coverage, executable-sample coverage, and "
            "a known-gaps preview, keyed to honua-server's canonical capability "
            "vocabulary. See freshness for per-producer pull status."
        ),
        "sourceArtifacts": {
            "server-keys": SERVER_KEYS_URL,
            "server-matrix": SERVER_MATRIX_URL,
            "sdk-js": SDK_URLS["js"],
            "sdk-dotnet": SDK_URLS["dotnet"],
            "sdk-python": SDK_URLS["python"],
            "samples": f"https://github.com/{SAMPLES_REPO}/actions/workflows/{SAMPLES_WORKFLOW}",
            "open-issues": f"https://github.com/{GAPS_REPO}/issues?q=is%3Aopen+label%3Acap%2F%2A",
        },
        "unjoinedCiteSuites": unjoined_cite_suites,
        "freshness": freshness,
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

    rendered = json.dumps(matrix, indent=2, sort_keys=False) + "\n"

    if args.check:
        if not args.output.exists():
            print(f"::error::{args.output} does not exist; run without --check to generate it.", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        # generatedAt/freshness necessarily churn between runs; compare
        # everything else so --check catches structural/content drift
        # without permanently failing simply because time passed.
        current_stable = strip_volatile(json.loads(current))
        rendered_stable = strip_volatile(matrix)
        if current_stable != rendered_stable:
            print(
                f"::error::{args.output} is stale relative to current producer snapshots. Run "
                "`python3 scripts/aggregate.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} is up to date ({len(matrix['capabilities'])} capabilities).")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"Wrote {args.output} ({len(matrix['capabilities'])} capabilities).")
    for name, entry in matrix["freshness"].items():
        print(f"  producer {name}: {entry['status']}" + (f" ({entry['detail']})" if entry.get("detail") else ""))
    return 0


def strip_volatile(matrix: dict[str, Any]) -> dict[str, Any]:
    clone = json.loads(json.dumps(matrix))
    clone.pop("generatedAt", None)
    for entry in clone.get("freshness", {}).values():
        entry.pop("fetchedAt", None)
        entry.pop("ageDays", None)
    return clone


if __name__ == "__main__":
    raise SystemExit(main())
