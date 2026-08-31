from __future__ import annotations

import copy
import importlib.util
import json
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
        self.assertGreater(len(self.inventory["entries"]), 40)
        for entry in self.inventory["entries"]:
            self.assertTrue(entry["owner"])
            self.assertTrue(entry["rationale"])
            self.assertIn(entry["classification"], validator.CLASSIFICATIONS)

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
