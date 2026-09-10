#!/usr/bin/env python3
"""Validate the governed Cloud Native Geospatial client/tool inventory."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SCHEMA = "honua.cloud-native-client-inventory/v1"
CLASSIFICATIONS = frozenset({
    "required-consumer", "optional-consumer", "producer", "supporting-tool", "not-applicable",
})
MATURITIES = frozenset({"supported", "preview", "roadmap", "deprecated"})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ENTRY_FIELDS = frozenset({
    "format", "tool", "classification", "maturity", "rationale", "owner", "ledger_clients",
})


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field {key!r}")
        result[key] = value
    return result


def load_inventory(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(document, dict) or set(document) != {
        "schema", "source", "governance", "entries",
    }:
        raise ValueError(f"{path}: inventory root has unknown or missing fields")
    if document.get("schema") != SCHEMA:
        raise ValueError(f"{path}: schema must be {SCHEMA}")
    source = document.get("source")
    if not isinstance(source, dict) or set(source) != {
        "guide", "repository", "revision", "inventory_source", "inventory_source_revision",
    }:
        raise ValueError(f"{path}: source must carry exact guide and inventory revision pins")
    if not SHA_RE.fullmatch(str(source.get("revision", ""))):
        raise ValueError(f"{path}: source.revision must be a full commit SHA")
    if not SHA_RE.fullmatch(str(source.get("inventory_source_revision", ""))):
        raise ValueError(f"{path}: source.inventory_source_revision must be a full commit SHA")
    for field in ("guide", "repository", "inventory_source"):
        if not isinstance(source.get(field), str) or not source[field].startswith("https://"):
            raise ValueError(f"{path}: source.{field} must be an HTTPS URL")
    if f"/blob/{source['inventory_source_revision']}/" not in source["inventory_source"]:
        raise ValueError(f"{path}: inventory_source URL must match inventory_source_revision")

    governance = document.get("governance")
    if not isinstance(governance, dict) or set(governance) != {
        "owner", "tracking_issue", "classifications", "release_required_classifications",
    }:
        raise ValueError(f"{path}: governance has unknown or missing fields")
    if governance.get("classifications") != sorted(CLASSIFICATIONS):
        raise ValueError(f"{path}: governance.classifications must enumerate the normalized vocabulary")
    if governance.get("release_required_classifications") != ["producer", "required-consumer"]:
        raise ValueError(f"{path}: release-required classifications must be producer and required-consumer")
    if not isinstance(governance.get("owner"), str) or not governance["owner"]:
        raise ValueError(f"{path}: governance.owner must be non-empty")
    if not isinstance(governance.get("tracking_issue"), str) or not governance["tracking_issue"].startswith("https://"):
        raise ValueError(f"{path}: governance.tracking_issue must be an HTTPS URL")

    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: entries must be a non-empty array")
    seen: set[tuple[str, str]] = set()
    seen_clients: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        prefix = f"{path}: entries[{index}]"
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            raise ValueError(f"{prefix} has unknown or missing fields")
        for field in ("format", "tool", "rationale", "owner"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"{prefix}.{field} must be non-empty")
        if entry["classification"] not in CLASSIFICATIONS:
            raise ValueError(f"{prefix}.classification is not governed")
        if entry["maturity"] not in MATURITIES:
            raise ValueError(f"{prefix}.maturity is not governed")
        clients = entry["ledger_clients"]
        if (
            not isinstance(clients, list)
            or any(not isinstance(client, str) or not client for client in clients)
            or len(clients) != len(set(clients))
        ):
            raise ValueError(f"{prefix}.ledger_clients must be a unique string array")
        if entry["classification"] in {"required-consumer", "producer"} and entry["maturity"] == "supported" and not clients:
            raise ValueError(f"{prefix} is release-required but has no ledger client join")
        if entry["classification"] == "not-applicable" and clients:
            raise ValueError(f"{prefix} is not applicable but claims ledger clients")
        key = (entry["format"], entry["tool"])
        if key in seen:
            raise ValueError(f"{prefix} duplicates inventory identity {key}")
        seen.add(key)
        for client in clients:
            client_key = (entry["format"], client)
            if client_key in seen_clients:
                raise ValueError(f"{prefix} has ambiguous ledger client {client_key}")
            seen_clients.add(client_key)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", nargs="?", type=Path, default=Path("config/cloud-native-client-inventory.v1.json"))
    args = parser.parse_args(argv)
    inventory = load_inventory(args.inventory)
    print(f"validated {len(inventory['entries'])} governed Cloud Native Geospatial inventory entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
