#!/usr/bin/env python3
"""Fetch normalized certification fragments from registered Actions artifacts."""

from __future__ import annotations

import argparse
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


def list_artifacts(repository: str, token: str, max_pages: int = 5) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        listing = json.loads(request_bytes(
            f"https://api.github.com/repos/{repository}/actions/artifacts?per_page=100&page={page}",
            token,
            "application/vnd.github+json",
        ))
        batch = listing.get("artifacts", [])
        if not isinstance(batch, list):
            raise ValueError(f"Invalid artifact listing for {repository}.")
        artifacts.extend(batch)
        if len(batch) < 100:
            return artifacts
        page += 1
    return artifacts


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
    payload: dict[str, Any], expected: str, producer_source_sha: str | None = None
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
        if not isinstance(observation, dict) or observation.get("producer_source_sha") != producer_source_sha:
            raise ValueError(
                f"Fragment observation {index} producer_source_sha does not match trusted run head "
                f"{producer_source_sha}."
            )


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
            "trusted_events", "max_artifacts", "max_artifact_pages",
            "max_candidates", "required", "accepted_conclusions",
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
        max_artifact_pages = source.get("max_artifact_pages", 5)
        if (
            isinstance(max_artifact_pages, bool)
            or not isinstance(max_artifact_pages, int)
            or not 1 <= max_artifact_pages <= 20
        ):
            raise ValueError(
                f"Producer {producer!r} max_artifact_pages must be an integer from 1 through 20."
            )
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
    return registry


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "-" for character in value)


def fragment_destination_name(member: str) -> str:
    member_digest = hashlib.sha256(member.encode("utf-8")).hexdigest()[:16]
    return f"{member_digest}-{safe_name(member)}.json"


def fetch(registry_path: Path, output: Path, token: str) -> int:
    registry = load_registry(registry_path)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    manifest: list[dict[str, Any]] = []
    for source in registry["sources"]:
        repository = source["repository"]
        candidates = select_artifacts(
            list_artifacts(
                repository,
                token,
                int(source.get("max_artifact_pages", 5)),
            ),
            source["artifact_prefix"],
            int(source.get("max_candidates", 100)),
            source.get("artifact_name_regex"),
        )
        producer_fragment_count = 0
        usable_artifact_count = 0
        producer_dir = output / safe_name(source["producer"])
        for artifact in candidates:
            if usable_artifact_count >= int(source.get("max_artifacts", 10)):
                break
            workflow_run = artifact.get("workflow_run")
            if not isinstance(workflow_run, dict) or not isinstance(workflow_run.get("id"), int):
                continue
            run = json.loads(request_bytes(
                f"https://api.github.com/repos/{repository}/actions/runs/{workflow_run['id']}",
                token,
                "application/vnd.github+json",
            ))
            if not trusted_run(run, artifact, source):
                continue
            archive = request_bytes(
                artifact["archive_download_url"], token, "application/vnd.github+json"
            )
            extracted = extract_fragments(archive, source["fragment_globs"])
            if not extracted:
                continue
            usable_artifact_count += 1
            for member, payload in extracted:
                validate_fragment_producer(payload, source["producer"], run["head_sha"])
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
                    "created_at": artifact.get("created_at"), "member": member,
                    "destination": destination.as_posix(),
                })
                producer_fragment_count += 1
        if source.get("required", False) and producer_fragment_count == 0:
            raise ValueError(
                f"Required certification producer {source['producer']!r} yielded no normalized fragments."
            )
    (output / ".fetch-manifest.ndjson").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in manifest), encoding="utf-8"
    )
    print(f"Fetched {len(manifest)} normalized certification fragment(s) from {len(registry['sources'])} source(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--token-env", default="HONUA_EVIDENCE_TOKEN")
    args = parser.parse_args()
    registry = load_registry(args.registry)
    if args.validate_only:
        print(f"Validated {len(registry['sources'])} protocol certification producer(s).")
        return 0
    if args.output is None:
        parser.error("--output is required unless --validate-only is used")
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
    return fetch(args.registry, args.output, token)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"::error::Certification producer fetch failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
