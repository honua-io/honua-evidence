"""Preserve diagnostic nightly evidence and age it against evaluation time."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

MAX_AGE_DAYS = 14


def instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def apply_nightly_evidence(matrix, source, evaluated_at):
    """Copy observations; a successful pull never renews a producer timestamp."""
    if "nightlyEvidence" not in source:
        return
    matrix["nightlyEvidence"] = copy.deepcopy(source["nightlyEvidence"])
    contract = source.get("evidenceFreshness", {})
    matrix["evidenceFreshness"] = copy.deepcopy(contract)
    matrix["evidenceFreshness"]["evaluatedAt"] = evaluated_at
    now = instant(evaluated_at)
    anchor = instant(contract.get("anchor"))
    collected = instant(source["nightlyEvidence"].get("collectedAt"))
    configured_limit = contract.get("maxAgeDays")
    valid_limit = type(configured_limit) in (int, float) and 0 < configured_limit <= MAX_AGE_DAYS
    limit = configured_limit if valid_limit else MAX_AGE_DAYS
    collection_age = (now - anchor).total_seconds() / 86400 if now and anchor else None
    valid_collection = valid_limit and now and anchor and collected == anchor and collection_age >= 0
    collection_stale = valid_collection and collection_age > limit
    for capability in matrix["capabilities"]:
        for kind in ("cite", "interop", "geobenchResults"):
            for row in capability.get(kind, []):
                stamp = row.get("freshness", {})
                at = instant(stamp.get("runDate"))
                age = (now - at).total_seconds() / 86400 if now and at else None
                if collection_stale:
                    state = "stale"
                elif not valid_collection or at is None or at > anchor:
                    state = "unknown"
                elif age > limit:
                    state = "stale"
                else:
                    state = stamp.get("state") if stamp.get("state") in ("fresh", "stale", "unknown") else "unknown"
                row["freshness"] = {**stamp, "state": state,
                                    "ageDays": round(age, 3) if age is not None else None}
                if kind == "cite" and state != "fresh":
                    row["passRate"] = 0.0
