#!/usr/bin/env python3
"""Fetch normalized certification fragments from registered Actions artifacts."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import sys
import urllib.request
from urllib.parse import urlparse
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

FRAGMENT_SCHEMA = "honua.protocol-certification-fragment/v1"
REGISTRY_SCHEMA = "honua.protocol-certification-producers/v1"
REQUIREMENTS_SCHEMA = "honua.protocol-certification-requirements/v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RESERVED_CLIENT_LANE_PREFIX = "canonical-client-unassigned-"
REQUIRED_OBSERVATION_FIELDS = (
    "contract_revision", "auth_policy_revision", "fixture_revision",
    "producer_source_sha", "surface", "operation", "canonical_client",
    "client_version", "deployment_target",
)


class CredentialStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow artifact redirects without leaking GitHub API credentials to storage."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (
            redirected is not None
            and urlparse(req.full_url).hostname != urlparse(newurl).hostname
        ):
            for header in ("Authorization", "X-GitHub-Api-Version", "Accept"):
                redirected.remove_header(header)
        return redirected


def select_artifacts(
    artifacts: list[dict[str, Any]],
    prefix: str,
    limit: int,
    name_regex: str | None = None,
) -> list[dict[str, Any]]:
    name_pattern = re.compile(name_regex) if name_regex is not None else None
    eligible = [
        artifact for artifact in artifacts
        if not artifact.get("expired", False)
        and isinstance(artifact.get("name"), str)
        and artifact["name"].startswith(prefix)
        and (name_pattern is None or name_pattern.fullmatch(artifact["name"]))
        and isinstance(artifact.get("archive_download_url"), str)
    ]
    eligible.sort(key=lambda artifact: artifact.get("created_at", ""), reverse=True)
    return eligible[:limit]


def extract_fragments(archive: bytes, patterns: list[str]) -> list[tuple[str, dict[str, Any]]]:
    fragments: list[tuple[str, dict[str, Any]]] = []
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for member in bundle.namelist():
            normalized = member.replace("\\", "/")
            matches = any(
                fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(f"x/{normalized}", pattern)
                for pattern in patterns
            )
            if not matches:
                continue
            payload = json.loads(bundle.read(member))
            if isinstance(payload, dict) and payload.get("schema") == FRAGMENT_SCHEMA:
                fragments.append((normalized, payload))
    return fragments


def extract_json_documents(archive: bytes, patterns: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Extract matching JSON objects; unlike fragments these may be raw receipts."""
    documents: list[tuple[str, dict[str, Any]]] = []
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for member in bundle.namelist():
            normalized = member.replace("\\", "/")
            if not any(
                fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(f"x/{normalized}", pattern)
                for pattern in patterns
            ):
                continue
            try:
                payload = json.loads(bundle.read(member))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ValueError(f"Malformed JSON receipt {normalized!r}.") from error
            if not isinstance(payload, dict):
                raise ValueError(f"Raw receipt {normalized!r} must be a JSON object.")
            documents.append((normalized, payload))
    return documents


def _receipt_digest(receipt: dict[str, Any]) -> str:
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_client_interop(
    raw: dict[str, Any], requirements: list[dict[str, Any]], candidate: dict[str, str],
    producer_sha: str,
) -> dict[str, Any]:
    """Convert one server client-interop ``.cert.json`` into governed observations."""
    required = (
        "schema_version", "run_id", "run_date", "server_commit", "producer_source_sha", "image_digest",
        "fixture_revision", "server_config_revision", "auth_policy_revision",
        "client_lane", "client_version", "protocol", "protocol_version", "environment", "results",
    )
    missing = [field for field in required if field not in raw]
    if missing:
        raise ValueError(f"Malformed client interop receipt missing fields: {missing}.")
    if raw["schema_version"] != "1.0" or not isinstance(raw["results"], list):
        raise ValueError("Malformed client interop receipt schema/results.")
    if raw["server_commit"] != candidate["source_sha"]:
        raise ValueError("Client interop receipt server_commit does not match the candidate SHA.")
    if raw["producer_source_sha"] != producer_sha:
        raise ValueError("Client interop receipt producer_source_sha does not match the trusted run SHA.")
    if raw["image_digest"] != candidate["image_digest"] or not DIGEST_RE.fullmatch(raw["image_digest"]):
        raise ValueError("Client interop receipt image_digest does not match the exact candidate.")
    if not isinstance(raw["run_date"], str) or not raw["run_date"]:
        raise ValueError("Client interop receipt run_date must be present.")

    observations: list[dict[str, Any]] = []
    rows = [*raw["results"], *raw.get("extensions", [])]
    for index, result in enumerate(rows):
        if not isinstance(result, dict):
            raise ValueError(f"Client interop result {index} must be an object.")
        test_id, status = result.get("test_case_id"), result.get("status")
        if not isinstance(test_id, str) or status not in {"pass", "fail", "skip", "not_applicable"}:
            raise ValueError(f"Malformed client interop result {index}.")
        if status == "not_applicable":
            continue
        matches = [
            requirement for requirement in requirements
            if requirement.get("client_lane") == raw["client_lane"]
            and requirement.get("client_version") == raw["client_version"]
            and requirement.get("surface") == raw["protocol"]
            and test_id in requirement.get("test_ids", [])
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Client interop result {test_id!r} resolves to {len(matches)} governed requirements."
            )
        requirement = matches[0]
        expected_fixture = requirement["fixture_revision"].replace("{source_sha}", candidate["source_sha"])
        for raw_field, expected in (
            ("fixture_revision", expected_fixture),
            ("server_config_revision", requirement["contract_revision"]),
            ("auth_policy_revision", requirement["auth_policy_revision"]),
        ):
            if raw[raw_field] != expected:
                raise ValueError(f"Client interop receipt {raw_field} does not match its governed requirement.")
        normalized_result = "skip" if status == "skip" else status
        identity = {
            "capability_key": requirement["capability_key"],
            "surface": requirement["surface"], "operation": requirement["operation"],
            "canonical_client": requirement["canonical_client"],
            "client_version": requirement["client_version"],
            "deployment_target": requirement["deployment_target"],
            "source_sha": candidate["source_sha"], "producer_source_sha": producer_sha,
            "image_digest": candidate["image_digest"], "fixture_revision": expected_fixture,
            "contract_revision": requirement["contract_revision"],
            "auth_policy_revision": requirement["auth_policy_revision"],
            "started_at": raw["run_date"], "completed_at": raw["run_date"],
            "candidate_cut_at": candidate["cut_at"], "test_ids": requirement["test_ids"],
        }
        observation = {
            key: identity[key] for key in (
                "surface", "operation", "canonical_client", "client_version", "deployment_target",
                "source_sha", "producer_source_sha", "image_digest", "fixture_revision",
                "contract_revision", "auth_policy_revision", "started_at", "completed_at", "test_ids",
            )
        }
        observation.update({
            "result": normalized_result,
            "skip_reason": ((result.get("notes") or "client interop lane skipped")
                            if normalized_result == "skip" else None),
            "evidence_uri": None, "evidence_digest": None, "evidence_receipt": None,
            "facet_results": None,
        })
        if normalized_result != "skip":
            facets = {facet: normalized_result for facet in requirement["scenario_facets"]}
            receipt = {
                "schema": "honua.certification-evidence-receipt/v1", "identity": identity,
                "result": normalized_result, "facets": facets,
                "payload_base64": base64.b64encode(json.dumps(result, sort_keys=True).encode()).decode(),
            }
            digest = _receipt_digest(receipt)
            observation.update({
                "evidence_receipt": receipt, "evidence_digest": digest,
                "evidence_uri": f"https://evidence.honua.io/data/sha256/{digest[7:]}",
                "facet_results": {
                    facet: {"result": value, "evidence_digest": digest}
                    for facet, value in facets.items()
                },
            })
        observations.append(observation)
    return {
        "schema": FRAGMENT_SCHEMA, "producer": "honua-server-client-interop",
        "generated_at": raw["run_date"], "candidate": candidate,
        "operation_scope": {"complete": True}, "observations": observations,
    }


def request_bytes(url: str, token: str, accept: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Authorization": f"Bearer {token}",
            "User-Agent": "honua-evidence-certification-aggregator",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = urllib.request.build_opener(CredentialStrippingRedirectHandler())
    with opener.open(request, timeout=60) as response:  # noqa: S310
        return response.read()


def trusted_run(run: dict[str, Any], artifact: dict[str, Any], source: dict[str, Any]) -> bool:
    repository = source["repository"]
    trusted_events = source.get("trusted_events")
    trusted_branches = source.get("trusted_branches")
    if not (
        isinstance(trusted_events, list)
        and trusted_events
        and all(isinstance(value, str) and value for value in trusted_events)
        and isinstance(trusted_branches, list)
        and trusted_branches
        and all(isinstance(value, str) and value for value in trusted_branches)
    ):
        return False
    artifact_run = artifact.get("workflow_run")
    path = run.get("path")
    try:
        run_started_at = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
        artifact_created_at = datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return (
        isinstance(artifact_run, dict)
        and artifact_run.get("id") == run.get("id")
        and artifact_run.get("head_sha") == run.get("head_sha")
        and run.get("status") == "completed"
        and run.get("conclusion") in source.get("accepted_conclusions", ["success"])
        and isinstance(run.get("run_attempt"), int)
        and run["run_attempt"] > 0
        and artifact_created_at > run_started_at
        and run.get("event") in source["trusted_events"]
        and run.get("head_branch") in source["trusted_branches"]
        and isinstance(path, str)
        and path.split("@", 1)[0] == source["workflow_path"]
        and run.get("head_repository", {}).get("full_name") == repository
    )


def validate_fragment_producer(
    payload: dict[str, Any], expected: str, producer_source_sha: str | None = None,
    repository: str | None = None,
) -> None:
    if payload.get("producer") != expected:
        raise ValueError(
            f"Fragment producer {payload.get('producer')!r} does not match registry producer {expected!r}."
        )
    if producer_source_sha is None:
        return
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError(f"Fragment from {expected!r} has no observations array.")
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise ValueError(f"Fragment observation {index} from {expected!r} is not an object.")
        if observation.get("producer_source_sha") != producer_source_sha:
            raise ValueError(
                f"Fragment observation {index} producer_source_sha does not match trusted run head "
                f"{producer_source_sha}."
            )
        missing = [field for field in REQUIRED_OBSERVATION_FIELDS if not observation.get(field)]
        if missing:
            raise ValueError(
                f"Fragment observation {index} from {expected!r} is missing governed fields: {missing}."
            )
        for field in ("canonical_client", "client_lane"):
            value = observation.get(field)
            if isinstance(value, str) and value.startswith(RESERVED_CLIENT_LANE_PREFIX):
                raise ValueError(
                    f"Fragment observation {index} attempts to satisfy reserved lane {value!r}."
                )
        uri = observation["evidence_uri"]
        parsed = urlparse(uri) if isinstance(uri, str) else None
        trusted_github_path = (
            repository is not None
            and parsed is not None
            and parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.path.startswith(f"/{repository}/actions/runs/")
        )
        trusted_ledger_path = (
            parsed is not None
            and parsed.scheme == "https"
            and parsed.hostname == "evidence.honua.io"
            and parsed.path.startswith("/data/sha256/")
        )
        if uri is not None and not (trusted_github_path or trusted_ledger_path):
            raise ValueError(
                f"Fragment observation {index} has untrusted evidence_uri {uri!r}."
            )


def load_pinned_revisions(path: Path) -> dict[str, str]:
    """Derive producer pins from the governed denominator.

    The pins are NEVER hand-maintained here: the release repository owns
    `source_revisions`, and this function only reads them. A second copy in
    this repository could drift out of the freeze and silently certify the
    wrong producer build.
    """
    requirements = json.loads(path.read_text(encoding="utf-8"))
    if requirements.get("schema") != REQUIREMENTS_SCHEMA:
        raise ValueError(f"{path}: not a governed protocol certification requirements catalog.")
    revisions = requirements.get("source_revisions")
    if not isinstance(revisions, dict) or not revisions:
        raise ValueError(f"{path}: requirements catalog has no source_revisions map.")
    pins: dict[str, str] = {}
    for name, entry in revisions.items():
        commit = entry.get("commit") if isinstance(entry, dict) else None
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
            raise ValueError(f"{path}: source_revisions[{name!r}] has no full 40-character commit SHA.")
        pins[name] = commit
    return pins


def list_runs_at_revision(
    repository: str, workflow_path: str, head_sha: str, token: str
) -> list[dict[str, Any]]:
    """Return completed runs of one workflow at exactly `head_sha`.

    Addressing runs by revision is what makes the harvest replayable. Paging the
    repository-wide artifact listing cannot reach a pinned revision in a busy
    repository (honua-server buries it well past the 20-page ceiling), and any
    depth limit there degrades into "newest wins" -- the exact defect this
    replaces.
    """
    if not SHA_RE.fullmatch(head_sha):
        raise ValueError(f"Refusing to harvest {repository} at non-immutable revision {head_sha!r}.")
    runs: list[dict[str, Any]] = []
    for page in range(1, 101):
        listing = json.loads(request_bytes(
            f"https://api.github.com/repos/{repository}/actions/runs"
            f"?head_sha={head_sha}&per_page=100&page={page}",
            token,
            "application/vnd.github+json",
        ))
        batch = listing.get("workflow_runs")
        if not isinstance(batch, list):
            raise ValueError(f"Invalid workflow run listing for {repository} at {head_sha}.")
        runs.extend(run for run in batch if isinstance(run, dict))
        if len(batch) < 100:
            break
    else:
        raise ValueError(f"Workflow run pagination exceeded safety limit for {repository} at {head_sha}.")
    matched = [
        run for run in runs
        if isinstance(run, dict)
        and isinstance(run.get("path"), str)
        and run["path"].split("@", 1)[0] == workflow_path
        and run.get("head_sha") == head_sha
    ]
    matched.sort(key=lambda run: str(run.get("run_started_at", "")), reverse=True)
    return matched


def list_run_artifacts(repository: str, run_id: int, token: str) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for page in range(1, 101):
        listing = json.loads(request_bytes(
            f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/artifacts"
            f"?per_page=100&page={page}",
            token,
            "application/vnd.github+json",
        ))
        batch = listing.get("artifacts")
        if not isinstance(batch, list):
            raise ValueError(f"Invalid artifact listing for {repository} run {run_id}.")
        artifacts.extend(artifact for artifact in batch if isinstance(artifact, dict))
        if len(batch) < 100:
            break
    else:
        raise ValueError(f"Artifact pagination exceeded safety limit for {repository} run {run_id}.")
    return artifacts


def load_registry(path: Path) -> dict[str, Any]:
    registry = json.loads(path.read_text(encoding="utf-8"))
    sources = registry.get("sources")
    if registry.get("schema") != REGISTRY_SCHEMA or not isinstance(sources, list) or not sources:
        raise ValueError("Invalid protocol certification producer registry.")
    producers: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Protocol certification producer entries must be objects.")
        allowed_fields = {
            "producer", "repository", "workflow_path", "artifact_prefix",
            "artifact_name_regex", "fragment_globs", "trusted_branches",
            "trusted_events", "max_artifacts",
            "max_candidates", "required", "accepted_conclusions",
            "source_revision_key",
            "implementation_issue",
            "normalizer", "related_issues",
        }
        unknown_fields = set(source) - allowed_fields
        if unknown_fields:
            raise ValueError(
                f"Protocol certification producer has unknown fields: {sorted(unknown_fields)}"
            )
        producer = source.get("producer")
        repository = source.get("repository")
        if not isinstance(producer, str) or not producer or producer in producers:
            raise ValueError(f"Invalid or duplicate registry producer: {producer!r}")
        producers.add(producer)
        if producer.startswith(RESERVED_CLIENT_LANE_PREFIX):
            raise ValueError(f"Reserved unassigned lane cannot be a producer: {producer!r}")
        if not isinstance(repository, str) or not repository.startswith("honua-io/") or repository.count("/") != 1:
            raise ValueError(f"Untrusted producer repository: {repository!r}")
        for field in ("workflow_path", "artifact_prefix"):
            value = source.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"Producer {producer!r} has invalid {field}.")
        artifact_name_regex = source.get("artifact_name_regex")
        if artifact_name_regex is not None:
            if not isinstance(artifact_name_regex, str) or not artifact_name_regex:
                raise ValueError(f"Producer {producer!r} has invalid artifact_name_regex.")
            try:
                re.compile(artifact_name_regex)
            except re.error as error:
                raise ValueError(
                    f"Producer {producer!r} has invalid artifact_name_regex: {error}"
                ) from error
        max_artifacts = source.get("max_artifacts", 10)
        if (
            isinstance(max_artifacts, bool)
            or not isinstance(max_artifacts, int)
            or not 1 <= max_artifacts <= 50
        ):
            raise ValueError(f"Producer {producer!r} max_artifacts must be an integer from 1 through 50.")
        max_candidates = source.get("max_candidates", 100)
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or not max_artifacts <= max_candidates <= 500
        ):
            raise ValueError(
                f"Producer {producer!r} max_candidates must be an integer from max_artifacts through 500."
            )
        if "required" in source and not isinstance(source["required"], bool):
            raise ValueError(f"Producer {producer!r} required must be a boolean.")
        accepted_conclusions = source.get("accepted_conclusions", ["success"])
        if (
            not isinstance(accepted_conclusions, list)
            or not accepted_conclusions
            or any(value not in {"success", "failure"} for value in accepted_conclusions)
            or len(set(accepted_conclusions)) != len(accepted_conclusions)
        ):
            raise ValueError(
                f"Producer {producer!r} accepted_conclusions must be a unique non-empty subset of success/failure."
            )
        for field in ("fragment_globs", "trusted_branches", "trusted_events"):
            value = source.get(field)
            if not (
                isinstance(value, list)
                and value
                and all(isinstance(item, str) and item for item in value)
            ):
                raise ValueError(f"Producer {producer!r} has invalid {field}.")
        source_revision_key = source.get("source_revision_key")
        if not isinstance(source_revision_key, str) or not source_revision_key:
            raise ValueError(
                f"Producer {producer!r} must declare source_revision_key so its harvest is "
                f"pinned to the governed denominator."
            )
        implementation_issue = source.get("implementation_issue")
        if implementation_issue is not None and not (
            isinstance(implementation_issue, str)
            and implementation_issue.startswith("https://github.com/honua-io/")
            and "/issues/" in implementation_issue
        ):
            raise ValueError(f"Producer {producer!r} must link its implementation_issue.")
        if source.get("normalizer") not in {None, "client-interop-cert-v1"}:
            raise ValueError(f"Producer {producer!r} has an unsupported normalizer.")
        related_issues = source.get("related_issues", [])
        if not isinstance(related_issues, list) or any(
            not isinstance(value, str) or not value.startswith("https://github.com/honua-io/")
            for value in related_issues
        ):
            raise ValueError(f"Producer {producer!r} has invalid related_issues.")
    return registry


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "-" for character in value)


def fragment_destination_name(member: str) -> str:
    member_digest = hashlib.sha256(member.encode("utf-8")).hexdigest()[:16]
    return f"{member_digest}-{safe_name(member)}.json"


def fetch(registry_path: Path, output: Path, token: str, pins: dict[str, str],
          requirements: list[dict[str, Any]] | None = None,
          candidate: dict[str, str] | None = None) -> int:
    registry = load_registry(registry_path)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    manifest: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for source in registry["sources"]:
        repository = source["repository"]
        revision_key = source["source_revision_key"]
        if revision_key not in pins:
            raise ValueError(
                f"Producer {source['producer']!r} pins source_revision_key {revision_key!r}, "
                f"which the governed denominator does not define."
            )
        pinned_sha = pins[revision_key]
        producer_fragment_count = 0
        usable_artifact_count = 0
        producer_dir = output / safe_name(source["producer"])
        for run in list_runs_at_revision(
            repository, source["workflow_path"], pinned_sha, token
        ):
            if usable_artifact_count >= int(source.get("max_artifacts", 10)):
                break
            candidates = select_artifacts(
                list_run_artifacts(repository, run["id"], token),
                source["artifact_prefix"],
                int(source.get("max_candidates", 100)),
                source.get("artifact_name_regex"),
            )
            for artifact in candidates:
                if usable_artifact_count >= int(source.get("max_artifacts", 10)):
                    break
                if not trusted_run(run, artifact, source):
                    continue
                # Defence in depth: trusted_run already ties the artifact to this
                # run, and the run was addressed by revision -- never accept a
                # fragment whose provenance is not the pin.
                if run.get("head_sha") != pinned_sha:
                    continue
                archive = request_bytes(
                    artifact["archive_download_url"], token, "application/vnd.github+json"
                )
                if source.get("normalizer") == "client-interop-cert-v1":
                    if requirements is None or candidate is None:
                        raise ValueError(
                            "Client interop normalization requires requirements and exact candidate."
                        )
                    extracted = [
                        (member, normalize_client_interop(
                            payload, requirements, candidate, run["head_sha"]
                        ))
                        for member, payload in extract_json_documents(
                            archive, source["fragment_globs"]
                        )
                    ]
                else:
                    extracted = extract_fragments(archive, source["fragment_globs"])
                if not extracted:
                    continue
                usable_artifact_count += 1
                for member, payload in extracted:
                    validate_fragment_producer(
                        payload, source["producer"], run["head_sha"], repository
                    )
                    destination = (
                        producer_dir / str(artifact["id"]) /
                        fragment_destination_name(member)
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                    manifest.append({
                        "producer": source["producer"], "repository": repository,
                        "artifact_id": artifact["id"], "artifact_name": artifact["name"],
                        "workflow_run_id": run["id"], "workflow_path": source["workflow_path"],
                        "head_branch": run["head_branch"], "head_sha": run["head_sha"],
                        "source_revision_key": revision_key, "pinned_source_sha": pinned_sha,
                        "created_at": artifact.get("created_at"), "member": member,
                        "destination": destination.as_posix(),
                    })
                    producer_fragment_count += 1
        if producer_fragment_count == 0:
            # Fail closed. A producer with no evidence AT THE PIN is a gap, and a
            # gap is reported as a gap. Falling back to a newer run would certify
            # a build the denominator never froze.
            if source.get("required", False):
                raise ValueError(
                    f"Required certification producer {source['producer']!r} has no normalized "
                    f"fragments at pinned {revision_key} revision {pinned_sha}."
                )
            gaps.append({
                "producer": source["producer"], "repository": repository,
                "workflow_path": source["workflow_path"],
                "source_revision_key": revision_key, "pinned_source_sha": pinned_sha,
                "reason": "no trusted producer run with normalized fragments at the pinned revision",
            })
            print(
                f"::warning::Certification producer {source['producer']!r} has no evidence at pinned "
                f"{revision_key} revision {pinned_sha}; recording a gap."
            )
    (output / ".fetch-manifest.ndjson").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in manifest), encoding="utf-8"
    )
    (output / ".fetch-gaps.ndjson").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in gaps), encoding="utf-8"
    )
    print(
        f"Fetched {len(manifest)} normalized certification fragment(s) from "
        f"{len(registry['sources'])} pinned source(s); {len(gaps)} source(s) recorded as gaps."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument(
        "--requirements", type=Path,
        help="governed protocol certification requirements catalog that owns source_revisions",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--candidate-source-sha")
    parser.add_argument("--candidate-image-digest")
    parser.add_argument("--candidate-cut-at")
    parser.add_argument("--token-env", default="HONUA_EVIDENCE_TOKEN")
    args = parser.parse_args()
    registry = load_registry(args.registry)
    if args.validate_only:
        print(f"Validated {len(registry['sources'])} protocol certification producer(s).")
        return 0
    if args.output is None:
        parser.error("--output is required unless --validate-only is used")
    if args.requirements is None:
        parser.error("--requirements is required unless --validate-only is used")
    pins = load_pinned_revisions(args.requirements)
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        if any(source.get("required", False) for source in registry["sources"]):
            print(
                f"::error::{args.token_env} is required for mandatory cross-repository certification producers.",
                file=sys.stderr,
            )
            return 1
        print(f"::notice::{args.token_env} is unavailable; cross-repository certification fragments remain missing.")
        return 0
    requirements_document = json.loads(args.requirements.read_text(encoding="utf-8"))
    requirements = requirements_document.get("requirements")
    if not isinstance(requirements, list):
        raise ValueError(f"{args.requirements}: requirements must be an array.")
    candidate_values = (
        args.candidate_source_sha, args.candidate_image_digest, args.candidate_cut_at,
    )
    if any(candidate_values) and not all(candidate_values):
        parser.error("candidate source SHA, image digest, and cut must be supplied together")
    candidate = None
    if all(candidate_values):
        candidate = {
            "source_sha": args.candidate_source_sha,
            "image_digest": args.candidate_image_digest,
            "cut_at": args.candidate_cut_at,
        }
    return fetch(args.registry, args.output, token, pins, requirements, candidate)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"::error::Certification producer fetch failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
