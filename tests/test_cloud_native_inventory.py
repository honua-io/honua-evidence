from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = ROOT / "config" / "cloud-native-client-inventory.v1.json"
VALIDATOR_PATH = ROOT / "scripts" / "validate-cloud-native-inventory.py"
AGGREGATOR_PATH = ROOT / "scripts" / "aggregate-certification.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = load_module("validate_cng_inventory", VALIDATOR_PATH)
aggregator = load_module("aggregate_for_cng_inventory", AGGREGATOR_PATH)


class CloudNativeInventoryTests(unittest.TestCase):
    def setUp(self):
        self.inventory = validator.load_inventory(INVENTORY_PATH)

    def write_invalid(self, document: dict) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "inventory.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_committed_inventory_is_revision_pinned_and_every_tool_is_governed(self):
        self.assertRegex(self.inventory["source"]["revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(self.inventory["source"]["inventory_source_revision"], r"^[0-9a-f]{40}$")
        self.assertEqual(53, len(self.inventory["entries"]))
        for entry in self.inventory["entries"]:
            self.assertTrue(entry["owner"])
            self.assertTrue(entry["rationale"])
            self.assertIn(entry["classification"], validator.CLASSIFICATIONS)

    def test_projection_contains_every_tool_in_the_independently_pinned_source(self):
        source = ROOT / "tests/fixtures/protocol-certification/cloud-native-client-inventory.upstream.yaml"
        self.assertEqual(
            "a3533deb93348ea3f9d7f65b286cdbbf4c6ef7e857fa7d073723812631bfed75",
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        text = source.read_text()
        self.assertIn(f"  revision: {self.inventory['source']['revision']}\n", text)
        # Read only the flat format/tool roster from the frozen upstream YAML;
        # do not derive the expected identities from our normalized inventory.
        expected = set()
        current_format, in_tools = None, False
        for line in text.split("\nformats:\n", 1)[1].splitlines():
            format_match = re.fullmatch(r"  ([a-z0-9-]+):", line)
            if format_match:
                current_format, in_tools = format_match[1], False
            elif line == "    tools:":
                in_tools = True
            elif in_tools:
                tool_match = re.fullmatch(r"      ([^:]+): [a-z-]+", line)
                if tool_match:
                    expected.add((current_format, tool_match[1]))
        self.assertEqual(53, len(expected))
        self.assertEqual(expected, {(entry["format"], entry["tool"]) for entry in self.inventory["entries"]})

    def test_duplicate_format_tool_identity_is_rejected(self):
        invalid = copy.deepcopy(self.inventory)
        invalid["entries"].append(copy.deepcopy(invalid["entries"][0]))
        with self.assertRaisesRegex(ValueError, "duplicates inventory identity"):
            validator.load_inventory(self.write_invalid(invalid))

    def test_required_supported_tool_without_ledger_join_is_rejected(self):
        invalid = copy.deepcopy(self.inventory)
        required = next(item for item in invalid["entries"] if item["classification"] == "required-consumer")
        required["ledger_clients"] = []
        with self.assertRaisesRegex(ValueError, "no ledger client join"):
            validator.load_inventory(self.write_invalid(invalid))

    def test_duplicate_json_fields_are_rejected(self):
        path = self.write_invalid(self.inventory)
        path.write_text(path.read_text().replace('"schema":', '"schema": "ignored", "schema":', 1))
        with self.assertRaisesRegex(ValueError, "duplicate JSON field"):
            validator.load_inventory(path)

    def test_inventory_url_must_bind_its_declared_revision(self):
        invalid = copy.deepcopy(self.inventory)
        invalid["source"]["inventory_source_revision"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "URL must match"):
            validator.load_inventory(self.write_invalid(invalid))

    def test_two_tools_cannot_claim_the_same_format_client(self):
        invalid = copy.deepcopy(self.inventory)
        invalid["entries"][1]["ledger_clients"] = invalid["entries"][0]["ledger_clients"]
        with self.assertRaisesRegex(ValueError, "ambiguous ledger client"):
            validator.load_inventory(self.write_invalid(invalid))

    def test_summary_joins_format_cells_and_reports_provenance(self):
        item = next(
            entry for entry in self.inventory["entries"]
            if entry["format"] == "cog" and entry["tool"] == "Rasterio"
        )
        ledger = {
            "cells": [{
                "capability_key": "format.cog", "canonical_client": "Rasterio",
                "result": "pass", "addressable_by_client": True,
                "source_sha": "a" * 40, "producer_source_sha": "b" * 40,
                "image_digest": "sha256:" + "c" * 64, "fixture_revision": "fixture-v1",
                "deployment_target": "local-docker",
            }]
        }
        summary = aggregator._cloud_native_inventory_summary(
            ledger,
            {**self.inventory, "entries": [item]},
        )
        joined = summary["entries"][0]["ledger"]
        self.assertEqual(1, joined["required"])
        self.assertEqual(1, joined["passed"])
        self.assertEqual(1, joined["provenance_complete"])
        self.assertEqual("pass", summary["entries"][0]["status"])
        self.assertEqual(1, summary["passing_required_tools"])

        # A required consumer absent from the denominator must remain visible.
        missing = aggregator._cloud_native_inventory_summary(
            {"cells": []}, {**self.inventory, "entries": [item]},
        )
        self.assertEqual(1, missing["required_tools"])
        self.assertEqual(1, missing["missing_required_tools"])
        self.assertEqual(0, missing["passing_required_tools"])
        self.assertEqual("missing", missing["entries"][0]["status"])

        for result in ("skip", "not-addressable", "fail"):
            with self.subTest(result=result):
                ledger["cells"][0]["result"] = result
                report = aggregator._cloud_native_inventory_summary(
                    ledger, {**self.inventory, "entries": [item]},
                )
                self.assertEqual("non-passing", report["entries"][0]["status"])
                self.assertEqual(0, report["passing_required_tools"])
                self.assertEqual(int(result == "fail"), report["entries"][0]["ledger"]["provenance_complete"])

    def test_required_scenario_depth_facets_are_published_even_at_zero(self):
        ledger = {
            "requirements_revision": "r1", "requirements_source_revision": "d" * 40,
            "requirements_complete": True, "generated_at": "2026-08-20T00:00:00Z",
            "candidate": {}, "cells": [],
        }
        summary = aggregator.build_summary(ledger)
        self.assertEqual(
            set(aggregator.REQUIRED_SCENARIO_DEPTH_FACETS),
            set(summary["scenario_facets"]),
        )
        self.assertTrue(all(value["required"] == 0 for value in summary["scenario_facets"].values()))


if __name__ == "__main__":
    unittest.main()
