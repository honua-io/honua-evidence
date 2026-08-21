#!/usr/bin/env python3
"""Join protocol certification observations against an authoritative denominator.

Requirements come from honua-release. Producers push immutable fragments under
data/producers/protocol-certification/. Every requirement is emitted: missing observations become
explicit skips, never absent rows or fabricated passes.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from urllib.parse import urlparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REQUIREMENTS_SCHEMA = "honua.protocol-certification-requirements/v1"
FRAGMENT_SCHEMA = "honua.protocol-certification-fragment/v1"
LEDGER_SCHEMA = "honua.protocol-certification/v1"
IDENTITY_FIELDS = ("surface", "operation", "canonical_client", "client_version", "deployment_target")
POLICY_FIELDS = (
    "capability_key", "surface", "operation", "maturity", "canonical_client", "client_lane",
    "client_version", "deployment_target", "required_tier", "licensed", "entitlement_policy_revision", "addressable_by_client",
    "addressability_reason", "scenario_facets", "contract_revision", "auth_policy_revision",
    "fixture_revision", "budget_expectations",
)
OPTIONAL_POLICY_FIELDS = ("test_ids",)
OBSERVATION_FIELDS = (
    "result", "skip_reason", "source_sha", "producer_source_sha", "image_digest", "fixture_revision",
    "contract_revision", "auth_policy_revision", "evidence_uri", "evidence_digest", "evidence_receipt", "facet_results",
    "started_at", "completed_at",
)
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)
OBSERVATION_RESULTS = frozenset({"pass", "fail", "skip"})
ENTITLEMENT_POLICIES = {
    "honua-pro-feature-subscriptions-v1": ("licensed-release", "api-key-protected-v1"),
    "esri-arcgis-pro-arcpy-v1": ("windows-licensed", "anonymous-and-protected-v1"),
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
def _valid_evidence_uri(value: object, evidence_digest: object) -> bool:
    if not isinstance(value, str) or not isinstance(evidence_digest, str):
        return False
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "evidence.honua.io"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    match = re.fullmatch(r"/data/sha256/([0-9a-f]{64})", parsed.path)
    return match is not None and evidence_digest == f"sha256:{match.group(1)}"


def _receipt_bytes(value: object) -> bytes | None:
    if not isinstance(value, dict):
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


RECEIPT_ID_FIELDS = (
    "capability_key", "surface", "operation", "canonical_client", "client_version",
    "deployment_target", "source_sha", "producer_source_sha", "image_digest",
    "fixture_revision", "contract_revision", "auth_policy_revision", "started_at", "completed_at",
)


def _valid_entitlement_assertion(observation: dict, requirement: dict, entitlement: object) -> bool:
    if not requirement.get("licensed"):
        return entitlement is None and requirement.get("entitlement_policy_revision") is None
    if not isinstance(entitlement, dict) or set(entitlement) != {
        "policy_revision", "capability_key", "deployment_target", "verification",
        "status", "checked_at", "license_fingerprint",
    }:
        return False
    checked_at = _timestamp(entitlement.get("checked_at"))
    started_at = _timestamp(observation.get("started_at"))
    completed_at = _timestamp(observation.get("completed_at"))
    return (
        isinstance(requirement.get("entitlement_policy_revision"), str)
        and entitlement.get("policy_revision") == requirement["entitlement_policy_revision"]
        and entitlement.get("capability_key") == requirement.get("capability_key")
        and entitlement.get("deployment_target") == requirement.get("deployment_target")
        and entitlement.get("verification") == "live-server-capability-probe-v1"
        and entitlement.get("status") == "active"
        and isinstance(entitlement.get("license_fingerprint"), str)
        and DIGEST_RE.fullmatch(entitlement["license_fingerprint"]) is not None
        and checked_at is not None
        and started_at is not None
        and completed_at is not None
        and started_at <= checked_at <= completed_at
    )


def _valid_receipt(observation: dict, requirement: dict) -> bool:
    receipt = observation.get("evidence_receipt")
    facet_results = observation.get("facet_results")
    receipt_fields = {"schema", "identity", "result", "facets", "payload_base64"}
    if requirement.get("licensed"):
        receipt_fields.add("entitlement")
    if (
        not isinstance(receipt, dict)
        or set(receipt) != receipt_fields
        or not isinstance(facet_results, dict)
    ):
        return False
    expected_identity = {
        field: (
            requirement["capability_key"] if field == "capability_key"
            else observation[field]
        )
        for field in RECEIPT_ID_FIELDS
    }
    if "test_ids" in requirement:
        expected_identity["test_ids"] = requirement["test_ids"]
    if requirement.get("licensed"):
        expected_identity["entitlement_policy_revision"] = requirement.get("entitlement_policy_revision")
    facets = receipt.get("facets")
    if (
        receipt.get("schema") != "honua.certification-evidence-receipt/v1"
        or receipt.get("identity") != expected_identity
        or receipt.get("result") != observation.get("result")
        or not isinstance(facets, dict)
        or set(facets) != set(requirement["scenario_facets"])
        or any(
            not isinstance(facet_results.get(facet), dict)
            or facets[facet] != facet_results[facet].get("result")
            for facet in facets
        )
        or not _valid_entitlement_assertion(observation, requirement, receipt.get("entitlement"))
        or not isinstance(receipt.get("payload_base64"), str)
    ):
        return False
    try:
        base64.b64decode(receipt["payload_base64"], validate=True)
    except (ValueError, TypeError):
        return False
    return True


def _result_counts(rows: list[dict]) -> dict[str, int]:
    return {
        "required": len(rows),
        "required_addressable": sum(bool(row.get("addressable_by_client")) for row in rows),
        "passed": sum(row.get("result") == "pass" for row in rows),
        "failed": sum(row.get("result") == "fail" for row in rows),
        "skipped": sum(row.get("result") == "skip" for row in rows),
        "not_addressable": sum(row.get("result") == "not-addressable" for row in rows),
    }


def _dimension_counts(cells: list[dict], field: str) -> dict[str, dict[str, int]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for cell in cells:
        grouped[str(cell[field])].append(cell)
    return {key: _result_counts(grouped[key]) for key in sorted(grouped)}


def build_summary(ledger: dict) -> dict:
    cells = ledger["cells"]
    facet_rows: dict[str, list[dict]] = defaultdict(list)
    client_operations: dict[str, set[tuple[str, str]]] = defaultdict(set)
    client_passed_operations: dict[str, set[tuple[str, str]]] = defaultdict(set)
    supported_groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)

    for cell in cells:
        for facet in cell["scenario_facets"]:
            facet_rows[facet].append(cell)
        operation = (cell["surface"], cell["operation"])
        client_operations[cell["canonical_client"]].add(operation)
        if cell["result"] == "pass":
            client_passed_operations[cell["canonical_client"]].add(operation)
        if cell["maturity"] in {"supported", "deprecated"}:
            supported_groups[(cell["surface"], cell["operation"], cell["deployment_target"])].append(cell)

    addressable_supported_groups = [
        rows for rows in supported_groups.values()
        if any(row["addressable_by_client"] for row in rows)
    ]
    supported_passed = sum(
        all(row["result"] == "pass" for row in rows if row["addressable_by_client"])
        for rows in addressable_supported_groups
    )
    supported_required = len(addressable_supported_groups)

    return {
        "schema": "honua.protocol-certification-summary/v1",
        "requirements_revision": ledger["requirements_revision"],
        "requirements_source_revision": ledger["requirements_source_revision"],
        "requirements_complete": ledger["requirements_complete"],
        "generated_at": ledger["generated_at"],
        "candidate": ledger["candidate"],
        "overall": _result_counts(cells),
        "by_surface": _dimension_counts(cells, "surface"),
        "by_client": _dimension_counts(cells, "canonical_client"),
        "by_target": _dimension_counts(cells, "deployment_target"),
        "by_required_tier": _dimension_counts(cells, "required_tier"),
        "scenario_facets": {
            facet: _result_counts(facet_rows[facet]) for facet in sorted(facet_rows)
        },
        "supported_operation_coverage": {
            "required": supported_required,
            "passed": supported_passed,
            "percent": round(100 * supported_passed / supported_required, 2) if supported_required else 0.0,
        },
        "canonical_client_operation_depth": {
            client: {
                "required_operations": len(client_operations[client]),
                "passed_operations": len(client_passed_operations[client]),
            }
            for client in sorted(client_operations)
        },
    }


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


def _governed_identity(value: dict) -> tuple[object, ...]:
    test_ids = value.get("test_ids")
    return (*_identity(value), tuple(test_ids) if isinstance(test_ids, list) else None)


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
        if not isinstance(requirement["addressable_by_client"], bool):
            raise ValueError(f"{path}: requirements[{index}].addressable_by_client must be a boolean")
        licensed = requirement.get("licensed")
        entitlement_policy = requirement.get("entitlement_policy_revision")
        if not isinstance(licensed, bool):
            raise ValueError(f"{path}: requirements[{index}].licensed must be a boolean")
        if licensed != isinstance(entitlement_policy, str):
            raise ValueError(
                f"{path}: requirements[{index}] licensed and entitlement_policy_revision must agree"
            )
        if licensed and entitlement_policy not in ENTITLEMENT_POLICIES:
            raise ValueError(f"{path}: requirements[{index}] entitlement policy is not governed")
        if licensed:
            expected_target, expected_auth = ENTITLEMENT_POLICIES[entitlement_policy]
            if (
                requirement.get("deployment_target") != expected_target
                or requirement.get("auth_policy_revision") != expected_auth
            ):
                raise ValueError(
                    f"{path}: requirements[{index}] entitlement target/auth does not match policy"
                )
        facets = requirement["scenario_facets"]
        if not (
            isinstance(facets, list)
            and all(isinstance(facet, str) and facet for facet in facets)
            and len(set(facets)) == len(facets)
        ):
            raise ValueError(
                f"{path}: requirements[{index}].scenario_facets must be an array of non-empty strings"
            )
        if "test_ids" in requirement:
            test_ids = requirement["test_ids"]
            if not (
                isinstance(test_ids, list)
                and test_ids
                and all(isinstance(test_id, str) and test_id for test_id in test_ids)
                and len(set(test_ids)) == len(test_ids)
            ):
                raise ValueError(
                    f"{path}: requirements[{index}].test_ids must be a non-empty unique string array"
                )
        key = _governed_identity(requirement)
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
        if not isinstance(candidate, dict):
            raise ValueError(f"{path}: candidate must contain source_sha, image_digest, and cut_at")
        if not isinstance(candidate.get("source_sha"), str) or not SHA_RE.fullmatch(candidate["source_sha"]):
            raise ValueError(f"{path}: candidate.source_sha must be a 40-character lowercase hex SHA")
        if not isinstance(candidate.get("image_digest"), str) or not DIGEST_RE.fullmatch(candidate["image_digest"]):
            raise ValueError(f"{path}: candidate.image_digest must be a sha256 digest")
        if _timestamp(candidate.get("cut_at")) is None:
            raise ValueError(f"{path}: candidate.cut_at must be a timezone-aware ISO-8601 timestamp")
        observations = document.get("observations")
        if not isinstance(observations, list):
            raise ValueError(f"{path}: observations must be an array")
        operation_scope = document.get("operation_scope")
        if not isinstance(operation_scope, dict) or operation_scope.get("complete") is not True:
            raise ValueError(f"{path}: operation_scope must be complete before observations can be aggregated")
        fragments.append((path, document))
    return fragments


def _candidate_identity(candidate: dict) -> tuple[object, ...]:
    return candidate.get("source_sha"), candidate.get("image_digest"), _timestamp(candidate.get("cut_at"))


def choose_candidate(fragments: list[tuple[Path, dict]], expected: tuple[str | None, str | None, str | None],
                     now: datetime | None = None) -> dict:
    expected_sha, expected_digest, expected_cut = expected
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if any(expected) and not all(expected):
        raise ValueError("expected source SHA, image digest, and cut time must be supplied together")
    if all(expected):
        if not isinstance(expected_sha, str) or not SHA_RE.fullmatch(expected_sha):
            raise ValueError("explicit candidate source SHA must be a full 40-character SHA")
        if not isinstance(expected_digest, str) or not DIGEST_RE.fullmatch(expected_digest):
            raise ValueError("explicit candidate image digest must be a sha256 digest")
        cut = _timestamp(expected_cut)
        if cut is None:
            raise ValueError("explicit candidate cut must be a timezone-aware ISO-8601 timestamp")
        if cut > now + CLOCK_SKEW_TOLERANCE:
            raise ValueError("explicit candidate cut is in the future")
        return {"source_sha": expected_sha, "image_digest": expected_digest, "cut_at": expected_cut}
    if not fragments:
        raise ValueError("no producer fragments exist and no exact candidate was supplied")
    for path, document in fragments:
        cut = _timestamp(document["candidate"].get("cut_at"))
        generated = _timestamp(document.get("generated_at"))
        if cut is None or generated is None:
            raise ValueError(f"{path}: candidate cut or fragment generation timestamp is invalid")
        if cut > generated + CLOCK_SKEW_TOLERANCE:
            raise ValueError(f"{path}: candidate.cut_at is after fragment generation")
        if cut > now + CLOCK_SKEW_TOLERANCE:
            raise ValueError(f"{path}: candidate.cut_at is in the future")
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


def build_ledger(requirements_revision: str, requirements_source_revision: str, requirements_complete: bool, requirements: list[dict], fragments: list[tuple[Path, dict]],
                 candidate: dict, now: datetime | None = None) -> dict:
    if not isinstance(requirements_source_revision, str) or not SHA_RE.fullmatch(requirements_source_revision):
        raise ValueError("requirements_source_revision must be a full 40-character SHA")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidate_id = _candidate_identity(candidate)
    requirement_by_key = {_governed_identity(requirement): requirement for requirement in requirements}
    requirement_keys = set(requirement_by_key)
    by_producer_key: dict[
        tuple[str, tuple[object, ...]],
        list[tuple[datetime, Path, dict]],
    ] = {}

    for path, fragment in fragments:
        matches_candidate = _candidate_identity(fragment["candidate"]) == candidate_id
        generated = _timestamp(fragment.get("generated_at"))
        if generated is None:
            raise ValueError(f"{path}: generated_at is invalid")
        if generated > now + CLOCK_SKEW_TOLERANCE:
            raise ValueError(f"{path}: generated_at is in the future")
        producer = fragment["producer"]
        for index, observation in enumerate(fragment["observations"]):
            if not isinstance(observation, dict):
                raise ValueError(f"{path}: observations[{index}] must be an object")
            if not matches_candidate:
                continue
            missing = [field for field in (*IDENTITY_FIELDS, *OBSERVATION_FIELDS) if field not in observation]
            if missing:
                raise ValueError(f"{path}: observations[{index}] missing {', '.join(missing)}")
            if not isinstance(observation.get("source_sha"), str) or not SHA_RE.fullmatch(observation["source_sha"]):
                raise ValueError(f"{path}: observations[{index}].source_sha must be a full 40-character SHA")
            if not isinstance(observation.get("producer_source_sha"), str) or not SHA_RE.fullmatch(observation["producer_source_sha"]):
                raise ValueError(f"{path}: observations[{index}].producer_source_sha must be a full 40-character SHA")
            if not isinstance(observation.get("image_digest"), str) or not DIGEST_RE.fullmatch(observation["image_digest"]):
                raise ValueError(f"{path}: observations[{index}].image_digest must be a sha256 digest")
            requirement_test_ids = observation.get("test_ids")
            if "test_ids" in observation and not isinstance(requirement_test_ids, list):
                raise ValueError(f"{path}: observations[{index}].test_ids must be an array")
            observation_key = _governed_identity(observation)
            if observation_key not in requirement_keys:
                raise ValueError(
                    f"observations do not resolve to requirements: "
                    f"{(observation_key, producer, str(path))}"
                )
            fragment_candidate = fragment["candidate"]
            if observation["source_sha"] != fragment_candidate["source_sha"]:
                raise ValueError(f"{path}: observations[{index}].source_sha does not match fragment candidate")
            if observation["image_digest"] != fragment_candidate["image_digest"]:
                raise ValueError(f"{path}: observations[{index}].image_digest does not match fragment candidate")
            fixture_template = requirement_by_key[observation_key]["fixture_revision"]
            expected_fixture = fixture_template.replace("{source_sha}", observation["source_sha"])
            if observation["fixture_revision"] != expected_fixture:
                raise ValueError(f"{path}: observations[{index}].fixture_revision does not match requirement")
            if observation["contract_revision"] != requirement_by_key[observation_key]["contract_revision"]:
                raise ValueError(f"{path}: observations[{index}].contract_revision does not match requirement")
            if observation["auth_policy_revision"] != requirement_by_key[observation_key]["auth_policy_revision"]:
                raise ValueError(f"{path}: observations[{index}].auth_policy_revision does not match requirement")
            result = observation.get("result")
            if result not in OBSERVATION_RESULTS:
                raise ValueError(
                    f"{path}: observations[{index}].result must be one of {sorted(OBSERVATION_RESULTS)}, got {result!r}"
                )
            evidence_uri = observation.get("evidence_uri")
            evidence_digest = observation.get("evidence_digest")
            evidence_receipt = observation.get("evidence_receipt")
            facet_results = observation.get("facet_results")
            if result == "skip":
                if any(value is not None for value in (
                    evidence_uri, evidence_digest, evidence_receipt, facet_results,
                )):
                    raise ValueError(
                        f"{path}: observations[{index}] skipped result must not contain evidence"
                    )
            else:
                digest_match = DIGEST_RE.fullmatch(evidence_digest) if isinstance(evidence_digest, str) else None
                if digest_match is None or not _valid_evidence_uri(evidence_uri, evidence_digest):
                    raise ValueError(
                        f"{path}: observations[{index}].evidence_uri must be content-addressed by evidence_digest"
                    )
                facets = requirement_by_key[observation_key]["scenario_facets"]
                if not isinstance(facet_results, dict) or set(facet_results) != set(facets):
                    raise ValueError(
                        f"{path}: observations[{index}].facet_results must cover every governed facet"
                    )
                for facet, facet_result in facet_results.items():
                    if (
                        not isinstance(facet_result, dict)
                        or set(facet_result) != {"result", "evidence_digest"}
                        or facet_result["result"] not in {"pass", "fail"}
                        or facet_result["evidence_digest"] != evidence_digest
                    ):
                        raise ValueError(
                            f"{path}: observations[{index}].facet_results[{facet!r}] "
                            "must be bound to evidence_digest"
                        )
                if result == "pass" and any(item["result"] != "pass" for item in facet_results.values()):
                    raise ValueError(f"{path}: observations[{index}].facet_results must all pass")
                if result == "fail" and all(item["result"] == "pass" for item in facet_results.values()):
                    raise ValueError(f"{path}: observations[{index}].facet_results must include a failure")
                receipt = _receipt_bytes(evidence_receipt)
                if not _valid_receipt(observation, requirement_by_key[observation_key]):
                    raise ValueError(
                        f"{path}: observations[{index}].evidence_receipt is not semantically bound"
                    )
                if receipt is None or hashlib.sha256(receipt).hexdigest() != digest_match.group(1):
                    raise ValueError(
                        f"{path}: observations[{index}].evidence_receipt bytes do not match evidence_digest"
                    )
            skip_reason = observation.get("skip_reason")
            if result == "skip" and (not isinstance(skip_reason, str) or not skip_reason.strip()):
                raise ValueError(f"{path}: observations[{index}].skip_reason is required for a skipped result")
            if result != "skip" and skip_reason is not None:
                raise ValueError(f"{path}: observations[{index}].skip_reason must be null unless result is skip")
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
            composite = (producer, observation_key)
            by_producer_key.setdefault(composite, []).append((completed, path, observation))

    newest_by_producer_key: dict[tuple[str, tuple[object, ...]], tuple[datetime, Path, dict]] = {}
    for composite, candidates in by_producer_key.items():
        newest_completed = max(candidate[0] for candidate in candidates)
        newest = [candidate for candidate in candidates if candidate[0] == newest_completed]
        selected = newest[0]
        if any(candidate[2] != selected[2] for candidate in newest[1:]):
            paths = " and ".join(str(candidate[1]) for candidate in newest)
            raise ValueError(
                f"conflicting observations tie for newest producer/cell {composite}: {paths}"
            )
        newest_by_producer_key[composite] = selected

    observations_by_key: dict[tuple[object, ...], list[tuple[str, Path, dict]]] = {}
    for (producer, key), (_, path, observation) in newest_by_producer_key.items():
        observations_by_key.setdefault(key, []).append((producer, path, observation))

    cells: list[dict] = []
    for requirement in requirements:
        key = _governed_identity(requirement)
        matches = observations_by_key.get(key, [])
        producers = {producer for producer, _, _ in matches}
        if len(producers) > 1:
            detail = ", ".join(f"{producer} ({path})" for producer, path, _ in matches)
            raise ValueError(f"ambiguous cross-producer evidence for {key}: {detail}")

        cell = {field: requirement[field] for field in POLICY_FIELDS}
        cell.update({field: requirement[field] for field in OPTIONAL_POLICY_FIELDS if field in requirement})
        # Addressability is owned by the requirement catalogue. Producer
        # observations cannot turn a non-addressable client operation into a
        # pass (or any other executable result).
        if not requirement["addressable_by_client"]:
            cell.update({
                "result": "not-addressable",
                "skip_reason": None,
                "source_sha": None,
                "producer_source_sha": None,
                "image_digest": None,
                "fixture_revision": None,
                "evidence_uri": None,
                "evidence_digest": None,
                "evidence_receipt": None,
                "facet_results": None,
                "started_at": None,
                "completed_at": None,
                "budget_observations": None,
            })
        elif matches:
            observation = matches[0][2]
            cell.update({field: observation[field] for field in OBSERVATION_FIELDS})
            cell["budget_observations"] = observation.get("budget_observations")
        else:
            cell.update({
                "result": "skip",
                "skip_reason": "no producer evidence for required certification cell",
                "source_sha": None,
                "producer_source_sha": None,
                "image_digest": None,
                "fixture_revision": None,
                "evidence_uri": None,
                "evidence_digest": None,
                "evidence_receipt": None,
                "facet_results": None,
                "started_at": None,
                "completed_at": None,
                "budget_observations": None,
            })
        cells.append(cell)

    return {
        "schema": LEDGER_SCHEMA,
        "requirements_revision": requirements_revision,
        "requirements_source_revision": requirements_source_revision,
        "requirements_complete": requirements_complete,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "candidate": candidate,
        "cells": cells,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--requirements-source-revision", required=True)
    parser.add_argument("--producers", default="data/producers/protocol-certification", type=Path)
    parser.add_argument("--output", default="data/protocol-certification.v1.json", type=Path)
    parser.add_argument("--summary", default="data/protocol-certification-summary.v1.json", type=Path)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image-digest", required=True)
    parser.add_argument("--candidate-cut-at", required=True)
    args = parser.parse_args(argv)

    if not all((args.candidate_source_sha, args.candidate_image_digest, args.candidate_cut_at)):
        parser.error("candidate source SHA, image digest, and cut must all be nonempty")

    revision, complete, requirements = load_requirements(args.requirements)
    fragments = load_fragments(args.producers)
    candidate = choose_candidate(fragments, (
        args.candidate_source_sha, args.candidate_image_digest, args.candidate_cut_at,
    ))
    ledger = build_ledger(revision, args.requirements_source_revision, complete, requirements, fragments, candidate)
    for cell in ledger["cells"]:
        receipt = _receipt_bytes(cell.get("evidence_receipt"))
        digest = cell.get("evidence_digest")
        digest_match = DIGEST_RE.fullmatch(digest) if isinstance(digest, str) else None
        if receipt is None or digest_match is None:
            continue
        receipt_path = args.output.parent / "sha256" / digest_match.group(1)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_bytes(receipt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = build_summary(ledger)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output}: {len(ledger['cells'])} required cell(s), candidate {candidate['source_sha']}")
    print(f"wrote {args.summary}: dimensional and scenario-depth coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
