"""Exercise the published CLI boundary with independently enumerated counts."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from test_aggregate_certification import (
    CANDIDATE, REQUIREMENTS_SOURCE_SHA, FIXTURES, module, requirement, observation,
    fragment, bind_format_payload,
)


class CertificationJoinFixtureTests(unittest.TestCase):
    def test_six_producer_families_publish_exact_counts_and_receipt_provenance(self):
        fixture = json.loads((FIXTURES / "join-scenarios.json").read_text())
        requirements = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            producers = root / "producers"
            producers.mkdir()
            for index, scenario in enumerate(fixture["scenarios"]):
                req = requirement(scenario["client"], scenario["result"] != "not-addressable")
                req.update(
                    capability_key=scenario["capability"], surface=scenario["surface"],
                    deployment_target=scenario["target"], required_tier=scenario["tier"],
                    scenario_facets=fixture["facets"],
                )
                if scenario["client"] == "ArcPy":
                    req.update(licensed=True, entitlement_policy_revision="esri-arcgis-pro-arcpy-v1",
                               auth_policy_revision="anonymous-and-protected-v1")
                if scenario["capability"] == "format.cog":
                    req["budget_expectations"] = fixture["format_budget"]
                requirements.append(req)
                observations = []
                if scenario["result"] in {"pass", "fail"}:
                    obs = observation(req, scenario["result"])
                    obs["producer_source_sha"] = scenario["sha_digit"] * 40
                    receipt = obs["evidence_receipt"]
                    receipt["identity"]["producer_source_sha"] = obs["producer_source_sha"]
                    if scenario["capability"] == "format.cog":
                        obs["budget_observations"] = fixture["format_observations"]
                        bind_format_payload(obs)
                    else:
                        digest = "sha256:" + hashlib.sha256(
                            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest()
                        obs["evidence_digest"] = digest
                        obs["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + digest[7:]
                        for facet in obs["facet_results"].values():
                            facet["evidence_digest"] = digest
                    observations.append(obs)
                (producers / f"{index}.json").write_text(json.dumps(fragment(scenario["producer"], observations)))

            requirements_path = root / "requirements.json"
            requirements_path.write_text(json.dumps({
                "schema": "honua.protocol-certification-requirements/v1", "revision": "join-fixture-v1",
                "complete": True, "requirements": requirements,
            }))
            ledger_path, summary_path = root / "ledger.json", root / "summary.json"
            self.assertEqual(0, module.main([
                "--requirements", str(requirements_path), "--requirements-source-revision", REQUIREMENTS_SOURCE_SHA,
                "--producers", str(producers), "--output", str(ledger_path), "--summary", str(summary_path),
                "--cloud-native-inventory", str(FIXTURES.parents[2] / "config/cloud-native-client-inventory.v1.json"),
                "--candidate-source-sha", CANDIDATE["source_sha"],
                "--candidate-image-digest", CANDIDATE["image_digest"], "--candidate-cut-at", CANDIDATE["cut_at"],
            ]))
            ledger, summary = json.loads(ledger_path.read_text()), json.loads(summary_path.read_text())
            # Expected totals are counted from the seven explicitly authored scenarios;
            # no production aggregation/counting helper computes this oracle.
            def counts(required, addressable, passed, failed, skipped, not_addressable):
                return dict(zip(("required", "required_addressable", "passed", "failed", "skipped", "not_addressable"),
                                (required, addressable, passed, failed, skipped, not_addressable)))

            self.assertEqual(counts(7, 6, 3, 1, 2, 1), summary["overall"])
            self.assertEqual({
                "nightly": counts(3, 2, 1, 0, 1, 1), "release": counts(3, 3, 1, 1, 1, 0),
                "pr": counts(1, 1, 1, 0, 0, 0),
            }, summary["by_required_tier"])
            self.assertEqual({
                "local-docker": counts(4, 3, 2, 0, 1, 1), "windows-licensed": counts(1, 1, 0, 1, 0, 0),
                "source-test-host": counts(1, 1, 1, 0, 0, 0), "aws-ecs": counts(1, 1, 0, 0, 1, 0),
            }, summary["by_target"])
            expected_groups = [
                ("CITE", "ogc-api-features", counts(1, 1, 1, 0, 0, 0)),
                ("ArcPy", "featureserver", counts(1, 1, 0, 1, 0, 0)),
                ("sdk-dotnet", "rest", counts(1, 1, 1, 0, 0, 0)),
                ("grpc-dotnet", "grpc", counts(1, 1, 0, 0, 1, 0)),
                ("mcp", "mcp", counts(1, 0, 0, 0, 0, 1)),
                ("Rasterio", "cog", counts(2, 2, 1, 0, 1, 0)),
            ]
            self.assertEqual({client: expected for client, _, expected in expected_groups}, summary["by_client"])
            self.assertEqual({surface: expected for _, surface, expected in expected_groups}, summary["by_surface"])
            self.assertEqual(counts(7, 6, 3, 1, 2, 1), summary["scenario_facets"]["positive"])
            for facet in ("negative", "auth", "pagination", "limit", "metadata", "range-efficiency"):
                self.assertEqual(counts(7, 6, 4, 0, 2, 1), summary["scenario_facets"][facet])
            self.assertEqual({"required_operations": 1, "passed_operations": 0},
                             summary["canonical_client_operation_depth"]["Rasterio"])
            self.assertEqual({"required": 6, "passed": 3, "percent": 50.0}, summary["supported_operation_coverage"])
            self.assertEqual(CANDIDATE, ledger["candidate"])
            self.assertEqual(REQUIREMENTS_SOURCE_SHA, ledger["requirements_source_revision"])
            for scenario, cell in zip(fixture["scenarios"], ledger["cells"]):
                if scenario["result"] not in {"pass", "fail"}:
                    self.assertIsNone(cell["producer_source_sha"])
                    self.assertIsNone(cell["evidence_receipt"])
                    continue
                self.assertEqual(CANDIDATE["source_sha"], cell["source_sha"])
                self.assertEqual(scenario["sha_digit"] * 40, cell["producer_source_sha"])
                self.assertEqual("fixture-v1", cell["fixture_revision"])
                self.assertEqual(None if scenario["target"] == "source-test-host" else CANDIDATE["image_digest"],
                                 cell["image_digest"])
                receipt_bytes = (root / "sha256" / cell["evidence_digest"][7:]).read_bytes()
                self.assertEqual(cell["evidence_digest"], "sha256:" + hashlib.sha256(receipt_bytes).hexdigest())
                self.assertEqual(cell["evidence_receipt"], json.loads(receipt_bytes))
            rasterio = next(item for item in summary["cloud_native_inventory"]["entries"]
                            if item["format"] == "cog" and item["tool"] == "Rasterio")
            self.assertEqual("non-passing", rasterio["status"])
            self.assertEqual(1, rasterio["ledger"]["provenance_complete"])
