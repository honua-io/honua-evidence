#!/usr/bin/env python3
"""Per-prospect evidence-brief generator (honua-io/honua-evidence#4).

Turns a capability key list -- from a ``?caps=`` URL, a plain list (intake
record), or a ``honua-caps.v1`` JSON file emitted by honua-esri-assess's
``--emit honua-caps`` crosswalk -- into a BUYER-SHAREABLE Markdown evidence
brief rendered from the committed ``data/capability-matrix.v1.json``:

* front matter carrying the proof-asset classification (BUYER-SHAREABLE),
  source-of-truth pointers, generation date, and matrix version;
* one L1 card per requested capability (edition, evidence status, evidence
  table, SDK coverage, samples) linking to its L2 page on
  https://evidence.honua.io/capabilities/;
* gap disclosures on every card and a global disclosures section. Gaps are
  included unconditionally -- there is deliberately NO flag to omit them; a
  brief with the gaps removed must be impossible to generate (the honesty
  mechanic from issue #4). Missing/stale producers from the freshness ledger
  are restated in the brief, never papered over as coverage;
* an output guard: this repo is public and briefs are buyer-shareable, so
  the rendered text is scanned for internal-only strings (private repo
  names, personal email addresses) before anything is written; a hit aborts
  with exit code 3 and writes nothing.

A second mode, ``proof-counts``, renders the marker-delimited protocol-
conformance counts block that refreshes the Benchmark/Conformance summary
counts quoted in the (private) sales proof-asset package per release --
the generator emits the block; a human carries it over in a reviewed PR
(human-in-the-loop only, same as brief delivery: this tool produces files,
people send them).

Zero-dependency: Python standard library only, offline -- it reads the
already-aggregated matrix JSON and never issues a network request.

Usage:
  python3 scripts/generate-brief.py brief --prospect "Acme County" \
      --caps serve.ogc-api-features,editing.featureserver-edits
  python3 scripts/generate-brief.py brief --prospect "Acme County" \
      --caps-url "https://honua.io/capabilities.html?caps=serve.wms,serve.wmts&units=4"
  python3 scripts/generate-brief.py brief --prospect "Acme County" \
      --caps-file honua-caps.json
  python3 scripts/generate-brief.py proof-counts --output -

Exit codes: 0 = written, 2 = bad input (e.g. unknown capability key -- keys
are validated against the matrix, which is drift-gated to honua-server's
canonical vocabulary; nothing is fabricated for an unknown key), 3 = the
buyer-shareable output guard found an internal-only string.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = REPO_ROOT / "data" / "capability-matrix.v1.json"
DEFAULT_OUT_DIR = REPO_ROOT / "dist" / "briefs"

SITE_BASE_URL = "https://evidence.honua.io/"
MATRIX_PUBLIC_URL = SITE_BASE_URL + "data/capability-matrix.v1.json"

# Public contact addresses. Personal addresses never appear in generated
# output (enforced by FORBIDDEN_OUTPUT_TERMS below).
CONTACT_EMAIL = "info@honua.io"
SECURITY_EMAIL = "security@honua.io"

# honua-evidence is a public repo and briefs are BUYER-SHAREABLE, so the
# rendered output must never leak internal-only material: private repo
# names/URLs, internal issue refs into those repos, or personal email
# addresses. The scan is case-insensitive and fail-closed -- if upstream
# matrix data (issue titles, sample metadata, ...) ever carries one of
# these, generation aborts instead of shipping the leak.
FORBIDDEN_OUTPUT_TERMS = (
    "honua-sales",
    "honua-demo",
    "honua-marketplace",
    "honua-support",
    "honua-devops",
    "honua-agentflow",
    "mike@honua.io",
)

EDITION_ORDER = ("Community", "Pro", "Enterprise")

# Maps a capability card's evidence rows to the freshness-ledger producer
# whose staleness/absence the row must disclose.
_PRODUCER_FOR_SDK = {"js": "sdk-js", "dotnet": "sdk-dotnet", "python": "sdk-python"}

_STATUS_LABEL = {
    "source-backed": "Source-backed",
    "source-evaluation": "Source evaluation",
    "partial": "Partial coverage",
    "proof-pending": "Proof pending",
}


class BriefInputError(Exception):
    """Bad caller input (unknown capability key, unreadable caps file, ...)."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(key: str) -> str:
    # Mirrors scripts/build-site.py's slug(): L2 pages live at
    # capabilities/<key with '.' -> '-'>.html.
    return key.replace(".", "-")


def prospect_slug(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return cleaned or "prospect"


def md_cell(value: Any) -> str:
    """Make an arbitrary string safe inside a Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Input: capability key lists
# ---------------------------------------------------------------------------

def parse_caps_url(url: str) -> tuple[list[str], int | None]:
    """Extract capability keys (and the optional serving-unit estimate) from
    a shareable catalog URL of the honua-esri-assess ``--emit honua-caps``
    shape: ``...?caps=<comma-keys>&units=<estimate>``."""
    query = parse_qs(urlsplit(url).query)
    raw = ",".join(query.get("caps", []))
    keys = [token.strip() for token in raw.split(",") if token.strip()]
    if not keys:
        raise BriefInputError(f"--caps-url has no caps= query parameter with capability keys: {url!r}")
    units: int | None = None
    for value in query.get("units", []):
        try:
            units = int(value)
        except ValueError as err:
            raise BriefInputError(f"--caps-url units= is not an integer: {value!r}") from err
    return keys, units


def load_caps_file(path: Path) -> tuple[list[str], int | None, list[dict[str, Any]]]:
    """Read a ``honua-caps.v1`` payload (honua-esri-assess ``--emit
    honua-caps``, honua-io/honua-esri-assess#84). Returns (keys,
    units_estimate, unmapped_entries) -- the unmapped inventory entries are
    carried into the brief's disclosures, not dropped (the honesty mechanic
    applies to the input side too)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise BriefInputError(f"could not read caps file {path}: {err}") from err
    if not isinstance(payload, dict) or not isinstance(payload.get("capabilities"), list):
        raise BriefInputError(
            f"caps file {path} is not a honua-caps payload (expected an object with a 'capabilities' list)"
        )
    keys = [entry["key"] for entry in payload["capabilities"] if isinstance(entry, dict) and entry.get("key")]
    if not keys:
        raise BriefInputError(f"caps file {path} contains no capability keys")
    units = payload.get("unitsEstimate")
    if units is not None and not isinstance(units, int):
        units = None
    unmapped = [entry for entry in payload.get("unmapped", []) if isinstance(entry, dict)]
    return keys, units, unmapped


def dedupe(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def resolve_capabilities(matrix: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    """Resolve requested keys against the matrix, preserving request order.
    Unknown keys fail loudly (the matrix is drift-gated to honua-server's
    canonical vocabulary; a brief never fabricates a capability)."""
    by_key = {cap["key"]: cap for cap in matrix.get("capabilities", [])}
    unknown = [key for key in keys if key not in by_key]
    if unknown:
        raise BriefInputError(
            "unknown capability key(s): "
            + ", ".join(sorted(unknown))
            + " -- keys are validated against data/capability-matrix.v1.json "
            "(canonical vocabulary: honua-server capability-keys.v1.json); nothing is fabricated"
        )
    return [by_key[key] for key in keys]


# ---------------------------------------------------------------------------
# Derived facts
# ---------------------------------------------------------------------------

def capability_status(cap: dict[str, Any]) -> str:
    # Mirrors scripts/build-site.py's capability_status() so the brief and
    # the public site never disagree about a capability's evidence status.
    if cap.get("entryCount", 0) > 0 and cap.get("provingTestCount", 0) > 0:
        return "source-backed"
    if cap.get("entryCount", 0) > 0:
        return "source-evaluation"
    if cap.get("noSurface") is not None:
        return "partial"
    return "proof-pending"


def edition_estimate(caps: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Highest edition across the requested set, plus the capability keys
    driving it. This is an estimate for conversation-starting only --
    pricing/packaging are quoted by the Honua team, never by this brief."""
    rank = {name: index for index, name in enumerate(EDITION_ORDER)}
    best = "Community"
    for cap in caps:
        if rank.get(cap.get("edition"), 0) > rank[best]:
            best = cap["edition"]
    drivers = sorted(cap["key"] for cap in caps if cap.get("edition") == best)
    return best, drivers


def producer_note(freshness: dict[str, Any], producer: str,
                  awaiting: Iterable[str] = ()) -> str | None:
    """A short parenthetical disclosure when the producer behind an evidence
    row is stale or missing; None when it is fresh. An absent ledger entry
    is treated as missing, EXCEPT for a producer the matrix declares in
    `awaitingFirstEnvelope` -- one whose ingestion is wired up but which has
    never produced a single envelope. That is disclosed as what it is, "not
    built yet", rather than as a snapshot that went missing (see
    docs/producer-contracts.md and honua-io/honua-release#89). Either way it is
    disclosed: nothing here ever reads as coverage."""
    if producer in set(awaiting):
        return f"{producer} has never produced evidence; this lane is not built yet"
    entry = freshness.get(producer)
    if entry is None:
        return f"{producer} snapshot absent from the freshness ledger at generation time"
    status = entry.get("status")
    if status == "stale":
        age = entry.get("ageDays")
        age_text = f"{age} days old" if age is not None else "past its freshness threshold"
        return f"{producer} snapshot stale at generation time ({age_text})"
    if status == "missing":
        return f"{producer} produced no evidence at generation time"
    return None


def scan_forbidden(text: str) -> list[str]:
    lowered = text.lower()
    return [term for term in FORBIDDEN_OUTPUT_TERMS if term in lowered]


# ---------------------------------------------------------------------------
# Rendering: the brief
# ---------------------------------------------------------------------------

def render_front_matter(matrix: dict[str, Any], prospect: str, generated_at: str) -> str:
    lines = [
        "---",
        "classification: BUYER-SHAREABLE",
        f"prospect: {prospect}",
        f"generatedAt: {generated_at}",
        "generator: honua-io/honua-evidence scripts/generate-brief.py (honua-io/honua-evidence#4)",
        f"matrixSchemaVersion: {matrix.get('schemaVersion', 'unknown')}",
        f"matrixGeneratedAt: {matrix.get('generatedAt', 'unknown')}",
        "sourceOfTruth:",
        f"  - {SITE_BASE_URL}",
        f"  - {MATRIX_PUBLIC_URL}",
        "delivery: human-in-the-loop -- this generator produces files; a person reviews and sends them",
        f"contact: {CONTACT_EMAIL}",
        "---",
    ]
    return "\n".join(lines)


def render_freshness_section(matrix: dict[str, Any]) -> list[str]:
    freshness: dict[str, Any] = matrix.get("freshness", {})
    lines = [
        "## Evidence freshness at generation time",
        "",
        "Every producer feeding this brief is listed with its status from the",
        f"freshness ledger in [capability-matrix.v1.json]({MATRIX_PUBLIC_URL}).",
        "A producer that produced nothing is recorded as **missing** and the",
        "affected sections below say so -- absence of evidence is disclosed,",
        "never restated as coverage.",
        "",
        "| Producer | Status | Source version | Age (days) |",
        "| --- | --- | --- | --- |",
    ]
    degraded: list[str] = []
    for producer, entry in freshness.items():
        status = entry.get("status", "missing")
        source_version = entry.get("sourceVersion") or "-"
        age = entry.get("ageDays")
        lines.append(
            f"| {md_cell(producer)} | {md_cell(status)} | {md_cell(source_version)} | "
            f"{md_cell(age if age is not None else '-')} |"
        )
        if status != "fresh":
            detail = entry.get("detail")
            degraded.append(f"- `{producer}`: **{status}**" + (f" -- {detail}" if detail else ""))
    if degraded:
        lines += ["", "Degraded producers at generation time:", ""] + degraded
    awaiting = matrix.get("awaitingFirstEnvelope") or []
    if awaiting:
        lines += [
            "",
            "Producers not built yet (no ledger row, because they have never produced",
            "anything at all -- disclosed here rather than shown as a snapshot that went",
            "missing):",
            "",
        ] + [f"- `{producer}`: **not built yet**" for producer in awaiting]
    return lines


def render_capability_card(cap: dict[str, Any], freshness: dict[str, Any],
                           awaiting: Iterable[str] = ()) -> list[str]:
    key = cap["key"]
    status = capability_status(cap)
    l2_url = f"{SITE_BASE_URL}capabilities/{slug(key)}.html"
    lines = [
        f"### {cap.get('displayName', key)} (`{key}`)",
        "",
        f"- Edition: {cap.get('edition', 'unknown')} | Status: {_STATUS_LABEL[status]}",
        f"- Evidence page (every claim links down to a raw receipt): <{l2_url}>",
        "",
        "| Evidence | Result at generation time |",
        "| --- | --- |",
    ]

    entry_count = cap.get("entryCount", 0) or 0
    test_count = cap.get("provingTestCount", 0) or 0
    counts_note = producer_note(freshness, "server-matrix")
    counts_text = (
        f"{test_count} proving tests across {entry_count} API entries"
        if entry_count or test_count
        else "no per-entry counts recorded"
    )
    if counts_note:
        counts_text += f" ({counts_note})"
    lines.append(f"| Proving tests | {md_cell(counts_text)} |")

    cite_rows = cap.get("cite") or []
    if cite_rows:
        parts = [
            f"{row.get('suite')} ({row.get('profile')}): {row.get('passed')}/{row.get('total')}"
            f" ({row.get('passRate')}%)"
            for row in cite_rows
        ]
        cite_text = "; ".join(parts)
        note = producer_note(freshness, "cite")
        if note:
            cite_text += f" ({note})"
    else:
        cite_text = "no OGC CITE suite joined to this capability"
    lines.append(f"| OGC CITE conformance | {md_cell(cite_text)} |")

    parity_rows = cap.get("parity") or []
    parity_text = (
        "; ".join(f"{row.get('displayName', row.get('serviceId'))}: {row.get('parity')}" for row in parity_rows)
        or "no operation-level parity cases recorded"
    )
    lines.append(f"| Esri parity cases | {md_cell(parity_text)} |")

    interop = cap.get("interop") or []
    interop_text = (
        "; ".join(f"{row.get('clientLane')} ({row.get('protocol')})" for row in interop)
        or "no real-client interop envelopes recorded"
    )
    lines.append(f"| Real-client interop | {md_cell(interop_text)} |")

    geobench = cap.get("geobench") or []
    lines.append(
        f"| Performance (geobench) | {md_cell('; '.join(geobench) if geobench else 'no geobench workloads recorded')} |"
    )

    sdk_parts: list[str] = []
    for lane in ("js", "dotnet", "python"):
        entry = (cap.get("sdks") or {}).get(lane) or {}
        lane_status = entry.get("status", "not-covered")
        if lane_status == "producer-missing":
            text = f"{lane}: coverage snapshot unavailable at generation time (not a coverage claim)"
        else:
            text = f"{lane}: {lane_status}"
            since = entry.get("sinceVersion")
            if lane_status in {"covered", "partial"} and since:
                text += f" (since {since})"
            note = producer_note(freshness, _PRODUCER_FOR_SDK[lane])
            if note and lane_status != "not-covered":
                text += f" ({note})"
        sdk_parts.append(text)
    lines.append(f"| SDK coverage | {md_cell('; '.join(sdk_parts))} |")

    samples = cap.get("samples") or []
    if samples:
        parts = []
        for sample in samples:
            last_run = sample.get("lastRun") or {}
            run_text = (
                f"last run {last_run.get('outcome')} at {last_run.get('at')}" if last_run else "no run recorded"
            )
            parts.append(f"{sample.get('title', sample.get('id'))} -- {run_text}")
        samples_text = "; ".join(parts)
        note = producer_note(freshness, "samples")
        if note:
            samples_text += f" ({note})"
    else:
        note = producer_note(freshness, "samples")
        samples_text = "no executable samples recorded" + (f" ({note})" if note else "")
    lines.append(f"| Executable samples | {md_cell(samples_text)} |")

    for field, label, producer in (("dr", "DR drills", "dr-drills"), ("liveCanary", "Live canary", "live-canary")):
        rows = cap.get(field) or []
        if rows:
            text = f"{len(rows)} evidence envelope(s) recorded"
            note = producer_note(freshness, producer, awaiting)
            if note:
                text += f" ({note})"
        else:
            note = producer_note(freshness, producer, awaiting)
            text = "none recorded" + (f" ({note})" if note else "")
        lines.append(f"| {label} | {md_cell(text)} |")

    # Gap disclosure block: ALWAYS rendered, on every card, with no way to
    # switch it off (issue #4's honesty mechanic).
    lines += ["", "Known gaps (disclosed by default; this section cannot be removed):", ""]
    gap_lines: list[str] = []
    no_surface = cap.get("noSurface")
    if no_surface:
        gap_lines.append(f"- No dedicated route surface: {no_surface.get('reason', no_surface.get('reasonCode'))}")
    open_issues = cap.get("openIssues") or {}
    for ref in open_issues.get("refs", []):
        scope = "category-level" if ref in (open_issues.get("categoryRefs") or []) else "capability-level"
        gap_lines.append(f"- Open issue [{ref.get('title')}]({ref.get('url')}) ({scope})")
    not_covered = [
        lane for lane in ("js", "dotnet", "python")
        if ((cap.get("sdks") or {}).get(lane) or {}).get("status") in {None, "not-covered"}
    ]
    if not_covered:
        gap_lines.append(f"- SDK coverage genuinely absent (snapshot loaded, key omitted): {', '.join(not_covered)}")
    producer_missing = [
        lane for lane in ("js", "dotnet", "python")
        if ((cap.get("sdks") or {}).get(lane) or {}).get("status") == "producer-missing"
    ]
    if producer_missing:
        gap_lines.append(
            "- SDK coverage unknown (producer snapshot unavailable at generation time): "
            + ", ".join(producer_missing)
        )
    if not gap_lines:
        gap_lines.append(
            "- No open gap issues or coverage holes recorded against this capability at generation time."
        )
    lines += gap_lines
    lines.append("")
    return lines


def render_brief(
    matrix: dict[str, Any],
    caps: list[dict[str, Any]],
    *,
    prospect: str,
    units: int | None,
    unmapped: list[dict[str, Any]],
    generated_at: str,
) -> str:
    freshness: dict[str, Any] = matrix.get("freshness", {})
    edition, drivers = edition_estimate(caps)

    lines: list[str] = [render_front_matter(matrix, prospect, generated_at), ""]
    lines += [
        f"# Honua Evidence Brief -- {prospect}",
        "",
        f"- Purpose: per-prospect evidence summary for the {len(caps)} capabilities below;"
        " every claim links one level down to a receipt a third party hosts or can re-run.",
        "- Classification: BUYER-SHAREABLE (shareable with the prospect's evaluation team;"
        " not posted on a public site).",
        f"- Generated: {generated_at} from capability-matrix.v1.json"
        f" schemaVersion {matrix.get('schemaVersion', 'unknown')}"
        f" (matrix generated {matrix.get('generatedAt', 'unknown')}).",
        f"- Contact: {CONTACT_EMAIL} (security questions: {SECURITY_EMAIL})",
        "",
        f"> Source of truth: the public evidence index at <{SITE_BASE_URL}>.",
        "> Gaps and stale or missing evidence are disclosed inline, next to the",
        "> strengths -- this generator cannot produce a brief with the gaps",
        "> removed, and delivery is human-in-the-loop only (a person reviews",
        "> and sends this file; nothing is auto-sent).",
        "",
        "## Scope and edition estimate",
        "",
        f"- Capabilities in scope: {len(caps)} -- " + ", ".join(f"`{cap['key']}`" for cap in caps),
        f"- Edition estimate: **{edition}**, driven by: " + ", ".join(f"`{key}`" for key in drivers) + ".",
        f"  Pricing and packaging are quoted by the Honua team ({CONTACT_EMAIL});"
        " this brief never states prices.",
    ]
    if units is not None:
        lines.append(
            f"- Serving-unit estimate: {units} (derived from the prospect's estate scan;"
            " an input to sizing conversations, not a quote)."
        )
    lines.append("")
    lines += render_freshness_section(matrix)
    lines += ["", "## Capability evidence", ""]
    for cap in caps:
        lines += render_capability_card(cap, freshness, matrix.get("awaitingFirstEnvelope") or [])

    lines += ["## Disclosures", ""]
    unjoined = matrix.get("unjoinedCiteSuites") or []
    if unjoined:
        lines.append(
            "- OGC CITE suites recorded upstream but not yet joined to a capability key"
            " (their counts are deliberately not attributed here): " + ", ".join(unjoined) + "."
        )
    for entry in unmapped:
        lines.append(
            f"- Estate-scan capability `{entry.get('assessKey', '?')}` has no Honua capability-key"
            f" mapping yet ({entry.get('reason', 'unmapped')};"
            f" {entry.get('matchedInventoryCount', '?')} matched inventory item(s))."
            " It is listed here rather than dropped."
        )
    lines.append(
        "- This brief was generated by the open-source generator in"
        " [honua-io/honua-evidence](https://github.com/honua-io/honua-evidence);"
        " re-run it against the published matrix to reproduce every number above."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rendering: the proof-counts refresh block
# ---------------------------------------------------------------------------

def render_proof_counts(matrix: dict[str, Any], generated_at: str) -> str:
    """The marker-delimited protocol-conformance counts block used to refresh
    the sales proof-asset package's conformance summary per release. Only
    counts actually joined in the matrix are emitted; unjoined suites are
    named without counts, and a stale CITE snapshot is disclosed."""
    freshness: dict[str, Any] = matrix.get("freshness", {})
    lines = [
        "<!-- BEGIN GENERATED: honua-evidence proof-counts (scripts/generate-brief.py, honua-io/honua-evidence#4) -->",
        f"Protocol conformance summary -- generated {generated_at} from"
        f" capability-matrix.v1.json schemaVersion {matrix.get('schemaVersion', 'unknown')}"
        f" (matrix generated {matrix.get('generatedAt', 'unknown')}).",
        "",
        "| Suite | Profile | Passed / Total | Pass rate | Capability |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    rows = 0
    for cap in matrix.get("capabilities", []):
        for row in cap.get("cite") or []:
            rows += 1
            lines.append(
                f"| {md_cell(row.get('suite'))} | {md_cell(row.get('profile'))} "
                f"| {row.get('passed')} / {row.get('total')} | {row.get('passRate')}% "
                f"| `{cap['key']}` |"
            )
    if rows == 0:
        lines.append("| (no CITE suites joined in this matrix) | - | - | - | - |")
    lines.append("")
    cite_entry = freshness.get("cite", {})
    note = producer_note(freshness, "cite")
    lines.append(
        f"- CITE source snapshot: {cite_entry.get('sourceVersion', 'unknown')} --"
        f" ledger status: {cite_entry.get('status', 'missing')}" + (f" ({note})" if note else "") + "."
    )
    unjoined = matrix.get("unjoinedCiteSuites") or []
    if unjoined:
        lines.append(
            "- Suites recorded upstream but not joined to a capability key"
            " (counts deliberately not quoted): " + ", ".join(unjoined) + "."
        )
    lines.append(f"- Source of truth: <{MATRIX_PUBLIC_URL}>.")
    lines.append("<!-- END GENERATED: honua-evidence proof-counts -->")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)

    brief = sub.add_parser("brief", help="generate a per-prospect BUYER-SHAREABLE evidence brief")
    brief.add_argument("--prospect", required=True, help="prospect name (title, front matter, output filename)")
    source = brief.add_mutually_exclusive_group(required=True)
    source.add_argument("--caps", help="comma-separated capability keys (intake record)")
    source.add_argument("--caps-url", help="shareable catalog URL carrying ?caps=<keys>[&units=<estimate>]")
    source.add_argument(
        "--caps-file",
        type=Path,
        help="honua-caps.v1 JSON emitted by honua-esri-assess --emit honua-caps (EsriFootprint crosswalk)",
    )
    brief.add_argument("--units", type=int, help="serving-unit estimate override (intake record)")
    brief.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    brief.add_argument(
        "--output",
        help="output path, or '-' for stdout (default: dist/briefs/<prospect>-evidence-brief-<date>.md)",
    )

    counts = sub.add_parser("proof-counts", help="emit the marker-delimited conformance-counts refresh block")
    counts.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    counts.add_argument("--output", default="-", help="output path, or '-' for stdout (default)")
    return parser


def _write_guarded(text: str, output: str | None, default_path: Path) -> int:
    """Run the buyer-shareable output guard, then write. Nothing is written
    on a guard hit -- fail closed."""
    hits = scan_forbidden(text)
    if hits:
        print(
            "::error::buyer-shareable output guard: generated content contains internal-only "
            f"term(s) {', '.join(sorted(hits))}; refusing to write anything. Fix the upstream "
            "data (or the generator) -- there is no override flag.",
            file=sys.stderr,
        )
        return 3
    if output == "-":
        sys.stdout.write(text)
        return 0
    path = Path(output) if output else default_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        print(f"::error::could not read matrix {args.matrix}: {err}", file=sys.stderr)
        return 2
    generated_at = utc_now_iso()

    if args.mode == "proof-counts":
        text = render_proof_counts(matrix, generated_at)
        return _write_guarded(text, args.output, DEFAULT_OUT_DIR / "proof-counts.md")

    try:
        unmapped: list[dict[str, Any]] = []
        units = args.units
        if args.caps:
            keys = [token.strip() for token in args.caps.split(",") if token.strip()]
            if not keys:
                raise BriefInputError("--caps is empty")
        elif args.caps_url:
            keys, url_units = parse_caps_url(args.caps_url)
            units = units if units is not None else url_units
        else:
            keys, file_units, unmapped = load_caps_file(args.caps_file)
            units = units if units is not None else file_units
        caps = resolve_capabilities(matrix, dedupe(keys))
    except BriefInputError as err:
        print(f"::error::{err}", file=sys.stderr)
        return 2

    text = render_brief(
        matrix, caps, prospect=args.prospect, units=units, unmapped=unmapped, generated_at=generated_at
    )
    date = generated_at.split("T", 1)[0]
    default_path = DEFAULT_OUT_DIR / f"{prospect_slug(args.prospect)}-evidence-brief-{date}.md"
    return _write_guarded(text, args.output, default_path)


if __name__ == "__main__":
    raise SystemExit(main())
