#!/usr/bin/env python3
"""Fetch normalized certification fragments from registered Actions artifacts."""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

FRAGMENT_SCHEMA = "honua.protocol-certification-fragment/v1"
REGISTRY_SCHEMA = "honua.protocol-certification-producers/v1"


def select_artifacts(artifacts: list[dict[str, Any]], prefix: str, limit: int) -> list[dict[str, Any]]:
    eligible = [
        artifact for artifact in artifacts
        if not artifact.get("expired", False)
        and isinstance(artifact.get("name"), str)
        and artifact["name"].startswith(prefix)
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
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        return response.read()


def list_artifacts(repository: str, token: str) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    page = 1
    while True:
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


def trusted_run(run: dict[str, Any], artifact: dict[str, Any], source: dict[str, Any]) -> bool:
    repository = source["repository"]
    artifact_run = artifact.get("workflow_run")
    path = run.get("path")
    return (
        isinstance(artifact_run, dict)
        and artifact_run.get("id") == run.get("id")
        and artifact_run.get("head_sha") == run.get("head_sha")
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
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
        producer = source.get("producer")
        repository = source.get("repository")
        if not isinstance(producer, str) or not producer or producer in producers:
            raise ValueError(f"Invalid or duplicate registry producer: {producer!r}")
        producers.add(producer)
        if not isinstance(repository, str) or not repository.startswith("honua-io/") or repository.count("/") != 1:
            raise ValueError(f"Untrusted producer repository: {repository!r}")
        for field in (
            "workflow_path", "artifact_prefix", "fragment_globs", "trusted_branches", "trusted_events"
        ):
            if not source.get(field):
                raise ValueError(f"Producer {producer!r} is missing {field}.")
    return registry


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "-" for character in value)


def fetch(registry_path: Path, output: Path, token: str) -> int:
    registry = load_registry(registry_path)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    manifest: list[dict[str, Any]] = []
    for source in registry["sources"]:
        repository = source["repository"]
        candidates = select_artifacts(
            list_artifacts(repository, token), source["artifact_prefix"], sys.maxsize
        )
        artifacts: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for artifact in candidates:
            workflow_run = artifact.get("workflow_run")
            if not isinstance(workflow_run, dict) or not isinstance(workflow_run.get("id"), int):
                continue
            run = json.loads(request_bytes(
                f"https://api.github.com/repos/{repository}/actions/runs/{workflow_run['id']}",
                token,
                "application/vnd.github+json",
            ))
            if trusted_run(run, artifact, source):
                artifacts.append((artifact, run))
            if len(artifacts) >= int(source.get("max_artifacts", 10)):
                break
        producer_dir = output / safe_name(source["producer"])
        for artifact, run in artifacts:
            archive = request_bytes(artifact["archive_download_url"], token, "application/octet-stream")
            for member, payload in extract_fragments(archive, source["fragment_globs"]):
                validate_fragment_producer(payload, source["producer"], run["head_sha"])
                destination = producer_dir / str(artifact["id"]) / f"{safe_name(member)}.json"
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
        print(f"::notice::{args.token_env} is unavailable; cross-repository certification fragments remain missing.")
        return 0
    return fetch(args.registry, args.output, token)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"::error::Certification producer fetch failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
