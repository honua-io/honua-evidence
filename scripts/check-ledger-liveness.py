#!/usr/bin/env python3
"""Ledger-liveness watchdog (honua-io/honua-evidence#17).

WHY THIS EXISTS
---------------
data/capability-matrix.v1.json is the freshness source of truth for
honua-release's `gate-evidence`. On 2026-08-16 the `aggregate` workflow
deadlocked itself: a scheduled run parked in `waiting` on the `github-pages`
environment with zero eligible reviewers and held the `aggregate-pages`
concurrency group (cancel-in-progress: false) for 42 hours. Ten later runs
queued behind it and were cancelled; the ledger froze.

The whole outage was INVISIBLE from inside this repo. Every symptom pointed
somewhere else: `live-canary` read `stale (ageDays=11)` although the canary
was ingesting fine, and `gate-evidence` stayed green off the frozen
timestamps -- and would eventually have gone red blaming `server-matrix`, the
wrong producer in the wrong repo.

honua-release#84 added a `ledger.generatedAt` age check on the CONSUMER side.
That is the right place for the release verdict, but it is three repos away
from the thing that is broken and it only trips at freeze time. This watchdog
is the PRODUCER-side half: it runs on its own schedule, in its own concurrency
group (so the very deadlock it detects can never silence it), and it answers
two questions no consumer can:

  1. Is the committed ledger still being regenerated?  (`generatedAt` age)
  2. Is an `aggregate` run parked right now?           (run status + age)

Question 2 is the leading indicator: a parked run is visible within an hour,
long before the ledger ages far enough for anyone downstream to notice.

This module is pure and offline by design -- it reads a matrix file and a
`gh run list --json ...` dump and returns a verdict. All GitHub mutations
(cancelling parked runs, filing the alert issue) live in
.github/workflows/ledger-liveness.yml, so the decision logic stays unit
testable with no network and no token. Python 3 standard library only, same
dependency-light rule as scripts/aggregate.py.

Run:
  python3 scripts/check-ledger-liveness.py --runs runs.json

Exit status:
  0  healthy
  1  stalled (ledger too old, and/or an aggregate run parked too long)
  2  usage / unreadable inputs
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = REPO_ROOT / "data" / "capability-matrix.v1.json"

# The aggregator refreshes daily (cron "23 6 * * *") plus on every
# producer-updated dispatch, so a healthy ledger is regenerated many times a
# day. 30h tolerates one entirely missed daily run without flapping, and
# deliberately trips BEFORE honua-release's own 36h `ledger.maxAgeHours` --
# this repo should be the first to say the aggregator is dead, not the last.
DEFAULT_MAX_AGE_HOURS = 30

# A healthy aggregate run finishes in ~30s. Anything still un-started after
# this long is parked, not busy.
DEFAULT_STUCK_AFTER_MINUTES = 45

# Statuses that mean "this run is waiting for an approval that may never
# come". These are the 2026-08-16 failure mode, and they are safe to cancel:
# the next scheduled/dispatched run re-aggregates from scratch, so nothing is
# lost, while leaving one parked costs the ledger.
PARKED_STATUSES = frozenset({"waiting", "action_required", "requested"})

# Statuses that mean "this run is queued behind something". Reported, never
# cancelled -- ordinary backpressure resolves on its own, and cancelling here
# would fight the concurrency group rather than help it.
BLOCKED_STATUSES = frozenset({"queued", "pending"})

WATCHED_STATUSES = PARKED_STATUSES | BLOCKED_STATUSES


def parse_iso8601(value: str) -> datetime:
    """Parse the Zulu timestamps GitHub and aggregate.py both emit."""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_generated_at(matrix_path: Path) -> str | None:
    """The ledger's own `generatedAt`, or None if it cannot be read.

    Unreadable is deliberately NOT the same as absent-and-fine: an
    unparseable or missing matrix is itself a stall-grade condition, and the
    caller reports it as such rather than passing quietly.
    """
    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    generated_at = matrix.get("generatedAt")
    return generated_at if isinstance(generated_at, str) else None


def evaluate_ledger(generated_at: str | None, now: datetime,
                    max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> dict:
    """Verdict on the committed ledger's own age.

    Returns {"status": "fresh"|"stale"|"unreadable", "ageHours": float|None,
             "generatedAt": str|None, "maxAgeHours": float, "detail": str}.
    """
    if not generated_at:
        return {"status": "unreadable", "ageHours": None, "generatedAt": None,
                "maxAgeHours": max_age_hours,
                "detail": "data/capability-matrix.v1.json is missing, unparseable, "
                          "or carries no generatedAt"}
    try:
        stamped = parse_iso8601(generated_at)
    except ValueError:
        return {"status": "unreadable", "ageHours": None, "generatedAt": generated_at,
                "maxAgeHours": max_age_hours,
                "detail": f"generatedAt {generated_at!r} is not an ISO-8601 timestamp"}

    age_hours = (now - stamped).total_seconds() / 3600.0
    status = "stale" if age_hours > max_age_hours else "fresh"
    detail = (f"ledger generatedAt {generated_at} is {age_hours:.1f}h old "
              f"(threshold {max_age_hours:g}h)")
    return {"status": status, "ageHours": round(age_hours, 2),
            "generatedAt": generated_at, "maxAgeHours": max_age_hours,
            "detail": detail}


def select_stuck_runs(runs: list, now: datetime,
                      stuck_after_minutes: float = DEFAULT_STUCK_AFTER_MINUTES) -> list:
    """Aggregate runs that have sat un-started for longer than the threshold.

    `runs` is `gh run list --json databaseId,status,createdAt,event,url` output.
    Each returned entry adds `ageMinutes` and `cancellable` (True only for the
    approval-parked statuses -- see PARKED_STATUSES).
    """
    cutoff = now - timedelta(minutes=stuck_after_minutes)
    stuck = []
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        status = str(run.get("status") or "").lower()
        if status not in WATCHED_STATUSES:
            continue
        created_raw = run.get("createdAt") or run.get("created_at")
        if not created_raw:
            continue
        try:
            created = parse_iso8601(created_raw)
        except ValueError:
            continue
        if created > cutoff:
            continue
        stuck.append({
            "id": run.get("databaseId") or run.get("id"),
            "status": status,
            "event": run.get("event"),
            "url": run.get("url"),
            "createdAt": created_raw,
            "ageMinutes": round((now - created).total_seconds() / 60.0, 1),
            "cancellable": status in PARKED_STATUSES,
        })
    return stuck


def build_report(ledger: dict, stuck: list, stuck_after_minutes: float) -> tuple[bool, str]:
    """(stalled?, markdown report). The report is both the job summary and,
    when stalled, the body of the alert issue -- one text, so what CI shows
    and what the issue says can never drift apart."""
    ledger_bad = ledger["status"] != "fresh"
    stalled = ledger_bad or bool(stuck)

    lines = ["## honua-evidence ledger liveness", ""]
    verdict = "STALLED" if stalled else "healthy"
    lines.append(f"**Verdict: {verdict}**")
    lines.append("")
    lines.append(f"- ledger: `{ledger['status']}` -- {ledger['detail']}")
    if stuck:
        lines.append(f"- parked `aggregate` runs (un-started for >{stuck_after_minutes:g}m): "
                     f"{len(stuck)}")
        for run in stuck:
            action = "cancelled by the watchdog" if run["cancellable"] else "reported only"
            url = run.get("url") or f"run {run['id']}"
            lines.append(f"  - [{run['id']}]({url}) `{run['status']}` "
                         f"({run['event']}), {run['ageMinutes']:g}m old -- {action}")
    else:
        lines.append(f"- parked `aggregate` runs (un-started for >{stuck_after_minutes:g}m): none")

    if stalled:
        lines += [
            "",
            "### What this means",
            "",
            "`data/capability-matrix.v1.json` is the freshness source of truth for",
            "honua-release's `gate-evidence`. While it is frozen, that gate reads stale",
            "timestamps as live ones and every producer in the ledger looks progressively",
            "and wrongly stale -- see honua-io/honua-evidence#17 for the 2026-08-16 outage",
            "this watchdog exists to prevent repeating silently.",
            "",
            "### Next steps",
            "",
            "1. A `waiting` run means the `github-pages` environment parked a deployment.",
            "   Check `gh api repos/honua-io/honua-evidence/actions/runs/<id>/pending_deployments`:",
            "   `reviewers: []` means there is no eligible approver at all, which is an",
            "   org/environment settings problem, not a workflow problem.",
            "2. The ledger no longer depends on that environment (the `aggregate` job is",
            "   environment-free since #17), so a parked `deploy` should only lag the",
            "   published site. A stale ledger alongside a parked run means that split",
            "   regressed -- re-check `.github/workflows/aggregate.yml`.",
            "3. Re-run: `gh workflow run aggregate.yml -R honua-io/honua-evidence`.",
        ]
    return stalled, "\n".join(lines) + "\n"


def write_outputs(path: str | None, **values) -> None:
    """Append key=value pairs to $GITHUB_OUTPUT (heredoc form for multiline)."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            text = str(value)
            if "\n" in text:
                handle.write(f"{key}<<__HONUA_EOF__\n{text}\n__HONUA_EOF__\n")
            else:
                handle.write(f"{key}={text}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX,
                        help="path to capability-matrix.v1.json (default: the committed one)")
    parser.add_argument("--runs", type=Path, default=None,
                        help="path to `gh run list --json databaseId,status,createdAt,event,url` "
                             "output, or - for stdin. Omit to check the ledger age only.")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--stuck-after-minutes", type=float, default=DEFAULT_STUCK_AFTER_MINUTES)
    parser.add_argument("--now", default=None,
                        help="override 'now' (ISO-8601) -- tests and replay only")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"),
                        help="file to append step outputs to (default: $GITHUB_OUTPUT)")
    args = parser.parse_args(argv)

    now = parse_iso8601(args.now) if args.now else datetime.now(timezone.utc)

    runs: list = []
    if args.runs is not None:
        try:
            raw = sys.stdin.read() if str(args.runs) == "-" else args.runs.read_text(encoding="utf-8")
            runs = json.loads(raw) if raw.strip() else []
        except (OSError, ValueError) as exc:
            print(f"error: could not read --runs {args.runs}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(runs, list):
            print(f"error: --runs {args.runs} is not a JSON array", file=sys.stderr)
            return 2

    ledger = evaluate_ledger(read_generated_at(args.matrix), now, args.max_age_hours)
    stuck = select_stuck_runs(runs, now, args.stuck_after_minutes)
    stalled, report = build_report(ledger, stuck, args.stuck_after_minutes)

    print(report)
    write_outputs(
        args.github_output,
        stalled="true" if stalled else "false",
        ledger_status=ledger["status"],
        ledger_age_hours="" if ledger["ageHours"] is None else ledger["ageHours"],
        cancel_run_ids=" ".join(str(r["id"]) for r in stuck if r["cancellable"]),
        report=report,
    )
    return 1 if stalled else 0


if __name__ == "__main__":
    sys.exit(main())
