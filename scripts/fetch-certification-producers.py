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


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "-" for character in value)


def fetch(registry_path: Path, output: Path, token: str) -> int:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if registry.get("schema") != REGISTRY_SCHEMA or not isinstance(registry.get("sources"), list):
        raise ValueError("Invalid protocol certification producer registry.")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    manifest: list[dict[str, Any]] = []
    for source in registry["sources"]:
        repository = source["repository"]
        if not repository.startswith("honua-io/") or repository.count("/") != 1:
            raise ValueError(f"Untrusted producer repository: {repository!r}")
        listing = json.loads(request_bytes(
            f"https://api.github.com/repos/{repository}/actions/artifacts?per_page=100",
            token,
            "application/vnd.github+json",
        ))
        artifacts = select_artifacts(
            listing.get("artifacts", []), source["artifact_prefix"], int(source.get("max_artifacts", 10))
        )
        producer_dir = output / safe_name(source["producer"])
        for artifact in artifacts:
            archive = request_bytes(artifact["archive_download_url"], token, "application/octet-stream")
            for member, payload in extract_fragments(archive, source["fragment_globs"]):
                destination = producer_dir / str(artifact["id"]) / f"{safe_name(member)}.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                manifest.append({
                    "producer": source["producer"], "repository": repository,
                    "artifact_id": artifact["id"], "artifact_name": artifact["name"],
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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token-env", default="HONUA_EVIDENCE_TOKEN")
    args = parser.parse_args()
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
