#!/usr/bin/env python3
"""Join protocol certification observations against an authoritative denominator.

Requirements come from honua-release. Producers push immutable fragments under
data/producers/protocol-certification/. Every requirement is emitted: missing observations become
explicit skips, never absent rows or fabricated passes.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REQUIREMENTS_SCHEMA = "honua.protocol-certification-requirements/v1"
FRAGMENT_SCHEMA = "honua.protocol-certification-fragment/v1"
LEDGER_SCHEMA = "honua.protocol-certification/v1"
IDENTITY_FIELDS = ("surface", "operation", "canonical_client", "client_version", "deployment_target")
POLICY_FIELDS = (
    "capability_key", "surface", "operation", "maturity", "canonical_client", "client_lane",
    "client_version", "deployment_target", "required_tier", "licensed", "addressable_by_client",
    "addressability_reason", "scenario_facets", "contract_revision", "auth_policy_revision",
)
OBSERVATION_FIELDS = (
    "result", "skip_reason", "source_sha", "image_digest", "fixture_revision", "evidence_uri",
    "started_at", "completed_at",
)
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


def _identity(value: dict) -> tuple[object, ...]:
    return tuple(value.get(field) for field in IDENTITY_FIELDS)


def load_requirements(path: Path) -> tuple[str, bool, list[dict]]:
    document = _read_json(path)
    if document.get("schema") != REQUIREMENTS_SCHEMA:
        raise ValueError(f"{path}: schema must be {REQUIREMENTS_SCHEMA}")
    revision = document.get("revision")
    complete = document.get("complete")
    requirements = document.get("requirements")
    if not isinstance(revision, str) or not revision:
        raise ValueError(f"{path}: revision must be a non-empty string")
    if not isinstance(complete, bool):
        raise ValueError(f"{path}: complete must be a boolean")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError(f"{path}: requirements must be a non-empty array")
    seen: set[tuple[object, ...]] = set()
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            raise ValueError(f"{path}: requirements[{index}] must be an object")
        missing = [field for field in POLICY_FIELDS if field not in requirement]
        if missing:
            raise ValueError(f"{path}: requirements[{index}] missing {', '.join(missing)}")
        key = _identity(requirement)
        if key in seen:
            raise ValueError(f"{path}: duplicate requirement identity {key}")
        seen.add(key)
    return revision, complete, requirements


def load_fragments(directory: Path) -> list[tuple[Path, dict]]:
    fragments: list[tuple[Path, dict]] = []
    for path in sorted(directory.rglob("*.json")) if directory.is_dir() else []:
        document = _read_json(path)
        if document.get("schema") != FRAGMENT_SCHEMA:
            raise ValueError(f"{path}: schema must be {FRAGMENT_SCHEMA}")
        if not isinstance(document.get("producer"), str) or not document["producer"]:
            raise ValueError(f"{path}: producer must be a non-empty string")
        if _timestamp(document.get("generated_at")) is None:
            raise ValueError(f"{path}: generated_at must be a timezone-aware ISO-8601 timestamp")
        candidate = document.get("candidate")
        if not isinstance(candidate, dict) or not all(candidate.get(field) for field in ("source_sha", "image_digest", "cut_at")):
            raise ValueError(f"{path}: candidate must contain source_sha, image_digest, and cut_at")
        if _timestamp(candidate.get("cut_at")) is None:
            raise ValueError(f"{path}: candidate.cut_at must be a timezone-aware ISO-8601 timestamp")
        observations = document.get("observations")
        if not isinstance(observations, list):
            raise ValueError(f"{path}: observations must be an array")
        fragments.append((path, document))
    return fragments


def _candidate_identity(candidate: dict) -> tuple[object, ...]:
    return candidate.get("source_sha"), candidate.get("image_digest"), candidate.get("cut_at")


def choose_candidate(fragments: list[tuple[Path, dict]], expected: tuple[str | None, str | None, str | None]) -> dict:
    expected_sha, expected_digest, expected_cut = expected
    if any(expected) and not all(expected):
        raise ValueError("expected source SHA, image digest, and cut time must be supplied together")
    if all(expected):
        return {"source_sha": expected_sha, "image_digest": expected_digest, "cut_at": expected_cut}
    if not fragments:
        raise ValueError("no producer fragments exist and no exact candidate was supplied")
    # Producer arrival/generation time is not release authority: a delayed
    # fragment for an older candidate must never roll the ledger backward.
    # Release cut time orders candidates; an exact CLI triple remains the
    # authoritative option for release publication.
    newest_cut = max(
        _timestamp(document["candidate"]["cut_at"]) or datetime.min.replace(tzinfo=timezone.utc)
        for _, document in fragments
    )
    newest = [
        document["candidate"]
        for _, document in fragments
        if _timestamp(document["candidate"]["cut_at"]) == newest_cut
    ]
    identities = {_candidate_identity(candidate) for candidate in newest}
    if len(identities) != 1:
        raise ValueError(f"ambiguous candidates share newest cut_at {newest_cut.isoformat()}: {sorted(identities)}")
    return dict(newest[0])


def build_ledger(requirements_revision: str, requirements_complete: bool, requirements: list[dict], fragments: list[tuple[Path, dict]],
                 candidate: dict, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidate_id = _candidate_identity(candidate)
    by_producer_key: dict[tuple[str, tuple[object, ...]], tuple[datetime, Path, dict]] = {}

    for path, fragment in fragments:
        if _candidate_identity(fragment["candidate"]) != candidate_id:
            continue
        generated = _timestamp(fragment.get("generated_at"))
        if generated is None:
            raise ValueError(f"{path}: generated_at is invalid")
        if generated > now + CLOCK_SKEW_TOLERANCE:
            raise ValueError(f"{path}: generated_at is in the future")
        producer = fragment["producer"]
        for index, observation in enumerate(fragment["observations"]):
            if not isinstance(observation, dict):
                raise ValueError(f"{path}: observations[{index}] must be an object")
            missing = [field for field in (*IDENTITY_FIELDS, *OBSERVATION_FIELDS) if field not in observation]
            if missing:
                raise ValueError(f"{path}: observations[{index}] missing {', '.join(missing)}")
            started = _timestamp(observation.get("started_at"))
            completed = _timestamp(observation.get("completed_at"))
            if started is None:
                raise ValueError(f"{path}: observations[{index}].started_at is invalid")
            if completed is None:
                raise ValueError(f"{path}: observations[{index}].completed_at is invalid")
            if completed < started:
                raise ValueError(f"{path}: observations[{index}] completed before it started")
            if completed > generated + CLOCK_SKEW_TOLERANCE:
                raise ValueError(f"{path}: observations[{index}].completed_at is after fragment generation")
            if completed > now + CLOCK_SKEW_TOLERANCE:
                raise ValueError(f"{path}: observations[{index}].completed_at is in the future")
            composite = (producer, _identity(observation))
            previous = by_producer_key.get(composite)
            if previous is None or completed > previous[0]:
                by_producer_key[composite] = (completed, path, observation)

    observations_by_key: dict[tuple[object, ...], list[tuple[str, Path, dict]]] = {}
    for (producer, key), (_, path, observation) in by_producer_key.items():
        observations_by_key.setdefault(key, []).append((producer, path, observation))

    requirement_keys = {_identity(requirement) for requirement in requirements}
    unknown = sorted((key, producer, str(path)) for key, rows in observations_by_key.items() if key not in requirement_keys
                     for producer, path, _ in rows)
    if unknown:
        raise ValueError("observations do not resolve to requirements: " + "; ".join(map(str, unknown)))

    cells: list[dict] = []
    for requirement in requirements:
        key = _identity(requirement)
        matches = observations_by_key.get(key, [])
        producers = {producer for producer, _, _ in matches}
        if len(producers) > 1:
            detail = ", ".join(f"{producer} ({path})" for producer, path, _ in matches)
            raise ValueError(f"ambiguous cross-producer evidence for {key}: {detail}")

        cell = {field: requirement[field] for field in POLICY_FIELDS}
        # Addressability is owned by the requirement catalogue. Producer
        # observations cannot turn a non-addressable client operation into a
        # pass (or any other executable result).
        if not requirement["addressable_by_client"]:
            cell.update({
                "result": "not-addressable",
                "skip_reason": None,
                "source_sha": None,
                "image_digest": None,
                "fixture_revision": None,
                "evidence_uri": None,
                "started_at": None,
                "completed_at": None,
            })
        elif matches:
            observation = matches[0][2]
            cell.update({field: observation[field] for field in OBSERVATION_FIELDS})
        else:
            cell.update({
                "result": "skip",
                "skip_reason": "no producer evidence for required certification cell",
                "source_sha": None,
                "image_digest": None,
                "fixture_revision": None,
                "evidence_uri": None,
                "started_at": None,
                "completed_at": None,
            })
        cells.append(cell)

    return {
        "schema": LEDGER_SCHEMA,
        "requirements_revision": requirements_revision,
        "requirements_complete": requirements_complete,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "candidate": candidate,
        "cells": cells,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--producers", default="data/producers/protocol-certification", type=Path)
    parser.add_argument("--output", default="data/protocol-certification.v1.json", type=Path)
    parser.add_argument("--candidate-source-sha")
    parser.add_argument("--candidate-image-digest")
    parser.add_argument("--candidate-cut-at")
    args = parser.parse_args(argv)

    revision, complete, requirements = load_requirements(args.requirements)
    fragments = load_fragments(args.producers)
    candidate = choose_candidate(fragments, (
        args.candidate_source_sha, args.candidate_image_digest, args.candidate_cut_at,
    ))
    ledger = build_ledger(revision, complete, requirements, fragments, candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output}: {len(ledger['cells'])} required cell(s), candidate {candidate['source_sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
