from __future__ import annotations

import base64
import importlib.util
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "aggregate-certification.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "protocol-certification"
SPEC = importlib.util.spec_from_file_location("aggregate_certification", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)

SHA = "a" * 40
REQUIREMENTS_SOURCE_SHA = "d" * 40
DIGEST = "sha256:" + "b" * 64
CANDIDATE = {"source_sha": SHA, "image_digest": DIGEST, "cut_at": "2026-08-20T09:00:00Z"}


def requirement(client="Rasterio", addressable=True, test_ids=None):
    value = {
        "capability_key": "serve.cog",
        "surface": "cog",
        "operation": "window-read",
        "maturity": "supported",
        "canonical_client": client,
        "client_lane": client.lower(),
        "client_version": "1.0",
        "deployment_target": "local-docker",
        "required_tier": "nightly",
        "licensed": False,
        "entitlement_policy_revision": None,
        "addressable_by_client": addressable,
        "addressability_reason": None if addressable else "client has no operation",
        "scenario_facets": ["positive", "range-efficiency"],
        "budget_expectations": None,
        "contract_revision": "cog-1.0",
        "auth_policy_revision": "anonymous-v1",
        "fixture_revision": "fixture-v1",
    }
    if test_ids is not None:
        value["test_ids"] = test_ids
    return value


def observation(
    req,
    result="pass",
    completed="2026-08-20T10:05:00Z",
    candidate_cut_at=None,
):
    value = {field: req[field] for field in module.IDENTITY_FIELDS}
    if "test_ids" in req:
        value["test_ids"] = req["test_ids"]
    value.update({
        "result": result,
        "skip_reason": None,
        "source_sha": SHA,
        "producer_source_sha": "c" * 40,
        "image_digest": None if req["deployment_target"] == "source-test-host" else DIGEST,
        "fixture_revision": "fixture-v1",
        "contract_revision": req["contract_revision"],
        "auth_policy_revision": req["auth_policy_revision"],
        "evidence_uri": None,
        "evidence_digest": None,
        "evidence_receipt": None,
        "facet_results": None,
        "started_at": "2026-08-20T10:00:00Z",
        "completed_at": completed,
        "client_id": req["canonical_client"],
        "runner_lane": req["client_lane"],
        "protocol_version": "1.0",
        "protocol_profile": "core",
        "performed_by": req["canonical_client"],
        "request_url": "https://candidate.example.test/operation",
        "exercised_capabilities": list(req["scenario_facets"]),
        "budget_observations": None,
    })
    if result == "skip":
        return value
    facet_values = {facet: "pass" for facet in req["scenario_facets"]}
    if result == "fail":
        facet_values[req["scenario_facets"][0]] = "fail"
    receipt = {
        "schema": "honua.certification-evidence-receipt/v1",
        "identity": {
            field: (req["capability_key"] if field == "capability_key" else value[field])
            for field in module.RECEIPT_ID_FIELDS
        },
        "result": result,
        "facets": facet_values,
        "payload_base64": "dGVzdA==",
    }
    if "test_ids" in req:
        receipt["identity"]["test_ids"] = req["test_ids"]
    if candidate_cut_at is not None:
        receipt["identity"]["candidate_cut_at"] = candidate_cut_at
    if req["licensed"]:
        receipt["identity"]["entitlement_policy_revision"] = req["entitlement_policy_revision"]
        receipt["entitlement"] = {
            "policy_revision": req["entitlement_policy_revision"],
            "capability_key": req["capability_key"],
            "deployment_target": req["deployment_target"],
            "verification": "live-server-capability-probe-v1",
            "status": "active",
            "checked_at": "2026-08-20T10:02:00Z",
            "license_fingerprint": "sha256:" + "e" * 64,
        }
    receipt_digest = "sha256:" + hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    value["evidence_receipt"] = receipt
    value["evidence_digest"] = receipt_digest
    value["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + receipt_digest[7:]
    value["facet_results"] = {
        facet: {"result": facet_values[facet], "evidence_digest": receipt_digest}
        for facet in req["scenario_facets"]
    }
    return value


def bind_format_payload(value):
    payload = {
        "schema": "honua.format-budget-observations/v1",
        "budget_observations": value["budget_observations"],
    }
    receipt = value["evidence_receipt"]
    receipt["payload_base64"] = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    digest = "sha256:" + hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    value["evidence_digest"] = digest
    value["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + digest[7:]
    for facet_result in value["facet_results"].values():
        facet_result["evidence_digest"] = digest
    return value


def fragment(producer, observations, generated="2026-08-20T10:06:00Z"):
    return {
        "schema": module.FRAGMENT_SCHEMA,
        "producer": producer,
        "generated_at": generated,
        "candidate": CANDIDATE,
        "operation_scope": {"complete": True},
        "observations": observations,
    }


class CertificationAggregationTests(unittest.TestCase):
    def test_real_client_raw_receipt_normalizes_end_to_end_into_candidate_ledger(self):
        fetch_script = SCRIPT.parent / "fetch-certification-producers.py"
        fetch_spec = importlib.util.spec_from_file_location("fetch_for_interop_test", fetch_script)
        assert fetch_spec and fetch_spec.loader
        fetch_module = importlib.util.module_from_spec(fetch_spec)
        fetch_spec.loader.exec_module(fetch_module)
        req = requirement(client="GeoPandas", test_ids=["CERT-DISC-01"])
        req.update({
            "surface": "ogc-features", "operation": "collections",
            "client_lane": "py-geopandas", "client_version": "1.0.1",
            "contract_revision": "config-v1", "auth_policy_revision": "auth-v1",
        })
        raw = {
            "schema_version": "1.0", "run_id": "42", "run_date": "2026-08-20T10:05:00Z",
            "server_commit": SHA, "producer_source_sha": SHA, "image_digest": DIGEST,
            "fixture_revision": "fixture-v1", "server_config_revision": "config-v1",
            "auth_policy_revision": "auth-v1", "client_id": "GeoPandas", "runner_lane": "py-geopandas",
            "client_version": "1.0.1", "protocol": "ogc-features", "protocol_version": "1.0",
            "protocol_profile": "core",
            "environment": "local-docker", "results": [{
                "test_case_id": "CERT-DISC-01", "status": "pass", "duration_ms": 5,
                "notes": "GeoPandas discovered collections",
                "performed_by": "GeoPandas", "request_url": "https://candidate.test/collections",
                "exercised_capabilities": ["positive", "range-efficiency"],
            }],
        }
        normalized = fetch_module.normalize_client_interop(raw, [req], CANDIDATE, SHA)
        ledger = module.build_ledger(
            "rev-1", REQUIREMENTS_SOURCE_SHA, True, [req],
            [(Path("real-client.cert.json"), normalized)], CANDIDATE,
            now=datetime(2026, 8, 20, 10, 10, tzinfo=timezone.utc),
        )
        self.assertEqual("pass", ledger["cells"][0]["result"])
        self.assertEqual(SHA, ledger["cells"][0]["source_sha"])
        self.assertEqual(DIGEST, ledger["cells"][0]["image_digest"])

    def test_source_test_host_evidence_does_not_claim_candidate_image_execution(self):
        req = requirement(test_ids=["HarnessTests.ExactOperation"])
        req["deployment_target"] = "source-test-host"
        observed = observation(req)
        ledger = module.build_ledger(
            "rev-1", REQUIREMENTS_SOURCE_SHA, False, [req],
            [(Path("source.json"), fragment("server-protocol-harness", [observed]))],
            CANDIDATE,
        )
        self.assertEqual(DIGEST, ledger["candidate"]["image_digest"])
        self.assertIsNone(ledger["cells"][0]["image_digest"])
        self.assertIsNone(ledger["cells"][0]["evidence_receipt"]["identity"]["image_digest"])

        falsely_bound = observation(req)
        falsely_bound["image_digest"] = DIGEST
        falsely_bound["evidence_receipt"]["identity"]["image_digest"] = DIGEST
        with self.assertRaisesRegex(ValueError, "must be null for source-test-host"):
            module.build_ledger(
                "rev-1", REQUIREMENTS_SOURCE_SHA, False, [req],
                [(Path("false.json"), fragment("server-protocol-harness", [falsely_bound]))],
                CANDIDATE,
            )

        deployed = requirement()
        missing = observation(deployed)
        missing["image_digest"] = None
        missing["evidence_receipt"]["identity"]["image_digest"] = None
        with self.assertRaisesRegex(ValueError, "must be a sha256 digest"):
            module.build_ledger(
                "rev-1", REQUIREMENTS_SOURCE_SHA, False, [deployed],
                [(Path("missing.json"), fragment("server", [missing]))], CANDIDATE,
            )

    def test_governed_test_ids_are_receipt_bound_and_emitted(self):
        req = requirement(test_ids=["HarnessTests.ExactOperation"])
        exact = observation(req)
        ledger = module.build_ledger(
            "rev-1", REQUIREMENTS_SOURCE_SHA, True, [req],
            [(Path("exact.json"), fragment("server-protocol-harness", [exact]))], CANDIDATE,
        )
        self.assertEqual(req["test_ids"], ledger["cells"][0]["test_ids"])
        self.assertEqual(req["test_ids"], ledger["cells"][0]["evidence_receipt"]["identity"]["test_ids"])

        for replacement in (None, ["HarnessTests.WrongOperation"]):
            with self.subTest(test_ids=replacement):
                invalid = observation(req)
                if replacement is None:
                    invalid.pop("test_ids")
                    invalid["evidence_receipt"]["identity"].pop("test_ids")
                else:
                    invalid["test_ids"] = replacement
                    invalid["evidence_receipt"]["identity"]["test_ids"] = replacement
                with self.assertRaisesRegex(ValueError, "do not resolve"):
                    module.build_ledger(
                        "rev-1", REQUIREMENTS_SOURCE_SHA, True, [req],
                        [(Path("invalid.json"), fragment("server-protocol-harness", [invalid]))], CANDIDATE,
                    )

    def test_every_python_lane_receipt_is_bound_to_the_fragment_candidate_cut(self):
        for lane, contract in (
            ("sdk-python", "sdk-python-coverage@" + "c" * 40),
            ("sdk-python-certification", "sdk-python-certification@" + "c" * 40),
            ("sdk-python-ogc", "ogc-api-features-1.0"),
        ):
            with self.subTest(lane=lane):
                req = requirement(client="Honua SDK Python")
                req["client_lane"] = lane
                req["contract_revision"] = contract

                unbound = observation(req)
                with self.assertRaisesRegex(ValueError, "not semantically bound"):
                    module.build_ledger(
                        "rev-1", REQUIREMENTS_SOURCE_SHA, True, [req],
                        [(Path("unbound.json"), fragment("honua-sdk-python", [unbound]))],
                        CANDIDATE,
                    )
                bound = observation(req, candidate_cut_at=CANDIDATE["cut_at"])
                ledger = module.build_ledger(
                    "rev-1", REQUIREMENTS_SOURCE_SHA, True, [req],
                    [(Path("bound.json"), fragment("honua-sdk-python", [bound]))],
                    CANDIDATE,
                )
                self.assertEqual(
                    CANDIDATE["cut_at"],
                    ledger["cells"][0]["evidence_receipt"]["identity"]["candidate_cut_at"],
                )

                wrong_cut = observation(req, candidate_cut_at="2026-08-20T09:00:01Z")
                with self.assertRaisesRegex(ValueError, "not semantically bound"):
                    module.build_ledger(
                        "rev-1", REQUIREMENTS_SOURCE_SHA, True, [req],
                        [(Path("wrong-cut.json"), fragment("honua-sdk-python", [wrong_cut]))],
                        CANDIDATE,
                    )

    def test_format_budget_observations_are_bound_inside_the_receipt_payload(self):
        req = requirement()
        req["capability_key"] = "format.cog"
        req["budget_expectations"] = {
            "max_requests": 4,
            "max_transferred_bytes": 2048,
            "max_full_object_downloads": 0,
            "min_range_requests": 1,
            "min_cache_hits": 1,
            "max_coordinate_error": 0.001,
            "max_geometry_error": 0.001,
            "required_metadata": ["crs"],
            "expected_metadata": {"crs": "EPSG:4326"},
        }
        observed = observation(req)
        observed["budget_observations"] = {
            "requests": 3,
            "transferred_bytes": 1024,
            "full_object_downloads": 0,
            "range_requests": 2,
            "cache_hits": 1,
            "coordinate_error": 0.0,
            "geometry_error": 0.0,
            "metadata_assertions": ["crs"],
            "metadata_values": {"crs": "EPSG:4326"},
        }
        self.assertFalse(module._valid_receipt(observed, req))

        payload = {
            "schema": "honua.format-budget-observations/v1",
            "budget_observations": observed["budget_observations"],
        }
        observed["evidence_receipt"]["payload_base64"] = base64.b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        self.assertTrue(module._valid_receipt(observed, req))

        observed["budget_observations"]["requests"] = 2
        self.assertFalse(module._valid_receipt(observed, req))

        null_observation = observation(req)
        null_payload = {
            "schema": "honua.format-budget-observations/v1",
            "budget_observations": None,
        }
        null_observation["evidence_receipt"]["payload_base64"] = base64.b64encode(
            json.dumps(null_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        self.assertFalse(module._valid_receipt(null_observation, req))

        for field, invalid in (
            ("requests", True),
            ("coordinate_error", float("nan")),
            ("coordinate_error", 10**400),
            ("metadata_assertions", ["crs", "crs"]),
            ("metadata_assertions", []),
            ("metadata_values", None),
            ("metadata_values", {}),
            ("metadata_values", {"crs": "EPSG:3857"}),
        ):
            with self.subTest(field=field):
                malformed = observation(req)
                malformed["budget_observations"] = dict(payload["budget_observations"])
                malformed["budget_observations"][field] = invalid
                malformed_payload = {
                    "schema": "honua.format-budget-observations/v1",
                    "budget_observations": malformed["budget_observations"],
                }
                malformed["evidence_receipt"]["payload_base64"] = base64.b64encode(
                    json.dumps(malformed_payload, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ).decode("ascii")
                self.assertFalse(module._valid_receipt(malformed, req))

        for field, invalid in (
            ("requests", 5),
            ("transferred_bytes", 2049),
            ("full_object_downloads", 1),
            ("range_requests", 0),
            ("cache_hits", 0),
            ("coordinate_error", 0.01),
            ("geometry_error", 0.01),
        ):
            with self.subTest(governed_limit=field):
                over_budget = observation(req)
                over_budget["budget_observations"] = dict(payload["budget_observations"])
                over_budget["budget_observations"][field] = invalid
                bind_format_payload(over_budget)
                self.assertFalse(module._valid_receipt(over_budget, req))

        failed = observation(req, result="fail")
        failed["budget_observations"] = {
            **payload["budget_observations"],
            "requests": 999,
            "range_requests": 0,
            "coordinate_error": 99.0,
            "metadata_assertions": ["crs"],
            "metadata_values": {"crs": "EPSG:3857"},
        }
        bind_format_payload(failed)
        ledger = module.build_ledger(
            "rev-1",
            REQUIREMENTS_SOURCE_SHA,
            True,
            [req],
            [(Path("format-failure.json"), fragment("format-producer", [failed]))],
            CANDIDATE,
        )
        self.assertEqual("fail", ledger["cells"][0]["result"])
        self.assertEqual("EPSG:3857", ledger["cells"][0]["budget_observations"]["metadata_values"]["crs"])

    def test_format_budget_payload_rejects_ambiguous_or_pathological_json(self):
        req = requirement()
        req["capability_key"] = "format.cog"
        req["budget_expectations"] = {
            "max_requests": 4,
            "max_transferred_bytes": 2048,
            "max_full_object_downloads": 0,
            "min_range_requests": 1,
            "min_cache_hits": 1,
            "max_coordinate_error": 0.001,
            "max_geometry_error": 0.001,
            "required_metadata": ["crs"],
            "expected_metadata": {"crs": "EPSG:4326"},
        }
        observed = observation(req)
        observed["budget_observations"] = {
            "requests": 3,
            "transferred_bytes": 1024,
            "full_object_downloads": 0,
            "range_requests": 2,
            "cache_hits": 1,
            "coordinate_error": 0.0,
            "geometry_error": 0.0,
            "metadata_assertions": ["crs"],
            "metadata_values": {"crs": "EPSG:4326"},
        }

        duplicate = (
            b'{"schema":"honua.format-budget-observations/v1",'
            b'"budget_observations":{"metadata_values":{"crs":"EPSG:3857","crs":"EPSG:4326"}}}'
        )
        deeply_nested = (
            b'{"schema":"honua.format-budget-observations/v1","budget_observations":'
            + b"[" * 2000
            + b"0"
            + b"]" * 2000
            + b"}"
        )
        oversized_integer = (
            b'{"schema":"honua.format-budget-observations/v1","budget_observations":{"requests":'
            + b"9" * 5000
            + b"}}"
        )
        oversized = b"x" * (module.MAX_FORMAT_RECEIPT_PAYLOAD_BYTES + 1)
        for name, payload_bytes in (
            ("duplicate", duplicate),
            ("deeply-nested", deeply_nested),
            ("oversized-integer", oversized_integer),
            ("oversized", oversized),
        ):
            with self.subTest(payload=name):
                candidate = observation(req)
                candidate["budget_observations"] = observed["budget_observations"]
                candidate["evidence_receipt"]["payload_base64"] = base64.b64encode(
                    payload_bytes
                ).decode("ascii")
                self.assertFalse(module._valid_receipt(candidate, req))

    def test_non_format_receipt_payload_is_not_subject_to_the_format_budget(self):
        req = requirement()
        observed = observation(req)
        observed["evidence_receipt"]["payload_base64"] = base64.b64encode(
            b"x" * (module.MAX_FORMAT_RECEIPT_PAYLOAD_BYTES + 1)
        ).decode("ascii")

        self.assertTrue(module._valid_receipt(observed, req))

    def test_missing_observation_materializes_skip(self):
        ledger = module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [requirement()], [], CANDIDATE)
        self.assertEqual(REQUIREMENTS_SOURCE_SHA, ledger["requirements_source_revision"])
        self.assertEqual("skip", ledger["cells"][0]["result"])
        self.assertEqual(DIGEST, ledger["cells"][0]["image_digest"])
        self.assertIn("no producer evidence", ledger["cells"][0]["skip_reason"])

    def test_non_addressable_requirement_materializes_truthful_result(self):
        ledger = module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [requirement(addressable=False)], [], CANDIDATE)
        self.assertEqual("not-addressable", ledger["cells"][0]["result"])
        self.assertEqual(DIGEST, ledger["cells"][0]["image_digest"])

    def test_source_host_gaps_never_claim_candidate_image_provenance(self):
        for addressable in (True, False):
            with self.subTest(addressable=addressable):
                req = requirement(addressable=addressable)
                req["deployment_target"] = "source-test-host"
                ledger = module.build_ledger(
                    "rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], [], CANDIDATE,
                )
                self.assertIsNone(ledger["cells"][0]["image_digest"])

    def test_licensed_receipt_requires_live_entitlement_binding(self):
        req = requirement()
        req.update({
            "licensed": True,
            "entitlement_policy_revision": "honua-pro-feature-subscriptions-v1",
            "deployment_target": "licensed-release",
            "auth_policy_revision": "api-key-protected-v1",
        })
        observed = observation(req)
        self.assertTrue(module._valid_receipt(observed, req))

        observed["evidence_receipt"]["entitlement"]["deployment_target"] = "local-docker"
        self.assertFalse(module._valid_receipt(observed, req))

    def test_canonical_licensed_receipt_fixture_and_every_entitlement_binding(self):
        req = requirement()
        req.update({
            "licensed": True,
            "entitlement_policy_revision": "honua-pro-feature-subscriptions-v1",
            "deployment_target": "licensed-release",
            "auth_policy_revision": "api-key-protected-v1",
        })
        observed = observation(req)
        fixture = json.loads(
            (FIXTURES / "licensed-entitlement-receipt.v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture, observed["evidence_receipt"])
        self.assertEqual(module.ENTITLEMENT_RECEIPT_FIELDS, set(fixture["entitlement"]))
        self.assertTrue(module._valid_receipt(observed, req))

        invalid_values = {
            "policy_revision": "unknown-policy-v1",
            "capability_key": "serve.wrong",
            "deployment_target": "local-docker",
            "verification": "synthetic-client-check-v1",
            "status": "expired",
            "checked_at": "2026-08-20T09:59:59Z",
            "license_fingerprint": "raw-license-key",
        }
        for field, invalid_value in invalid_values.items():
            with self.subTest(field=field):
                malformed = observation(req)
                malformed["evidence_receipt"]["entitlement"][field] = invalid_value
                self.assertFalse(module._valid_receipt(malformed, req))

        for mutation in ("missing", "additional"):
            with self.subTest(mutation=mutation):
                malformed = observation(req)
                if mutation == "missing":
                    malformed["evidence_receipt"]["entitlement"].pop("checked_at")
                else:
                    malformed["evidence_receipt"]["entitlement"]["raw_license"] = "secret"
                self.assertFalse(module._valid_receipt(malformed, req))

    def test_canonical_unlicensed_v1_receipt_remains_unchanged(self):
        req = requirement()
        observed = observation(req)
        fixture = json.loads(
            (FIXTURES / "unlicensed-receipt.v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture, observed["evidence_receipt"])
        self.assertNotIn("entitlement", fixture)
        self.assertNotIn("entitlement_policy_revision", fixture["identity"])
        self.assertTrue(module._valid_receipt(observed, req))

        smuggled = observation(req)
        smuggled["evidence_receipt"]["entitlement"] = fixture
        self.assertFalse(module._valid_receipt(smuggled, req))

    def test_requirements_reject_unlicensed_entitlement_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.json"
            malformed = requirement()
            malformed["entitlement_policy_revision"] = "honua-pro-feature-subscriptions-v1"
            path.write_text(json.dumps({
                "schema": module.REQUIREMENTS_SCHEMA,
                "revision": "rev-1",
                "complete": True,
                "requirements": [malformed],
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must agree"):
                module.load_requirements(path)

    def test_requirements_reject_mislabeled_licensed_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.json"
            malformed = requirement()
            malformed.update({
                "licensed": True,
                "entitlement_policy_revision": "honua-pro-feature-subscriptions-v1",
                "deployment_target": "local-docker",
                "auth_policy_revision": "anonymous-public-v1",
            })
            path.write_text(json.dumps({
                "schema": module.REQUIREMENTS_SCHEMA,
                "revision": "rev-1",
                "complete": True,
                "requirements": [malformed],
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "target/auth"):
                module.load_requirements(path)

    def test_requirements_reject_non_array_scenario_facets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.json"
            malformed = requirement()
            malformed["scenario_facets"] = "positive"
            path.write_text(json.dumps({
                "schema": module.REQUIREMENTS_SCHEMA,
                "revision": "rev-1",
                "complete": True,
                "requirements": [malformed],
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "scenario_facets"):
                module.load_requirements(path)

    def test_requirements_reject_duplicate_scenario_facets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.json"
            malformed = requirement()
            malformed["scenario_facets"] = ["positive", "positive"]
            path.write_text(json.dumps({
                "schema": module.REQUIREMENTS_SCHEMA,
                "revision": "rev-1",
                "complete": True,
                "requirements": [malformed],
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "scenario_facets"):
                module.load_requirements(path)

    def test_summary_publishes_dimensions_and_scenario_depth(self):
        rasterio = requirement()
        gdal = requirement(client="GDAL", addressable=False)
        fragments = [(Path("rasterio.json"), fragment("server", [observation(rasterio)]))]
        ledger = module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [rasterio, gdal], fragments, CANDIDATE)

        summary = module.build_summary(ledger)

        self.assertEqual(module._result_counts(ledger["cells"]), summary["overall"])
        self.assertEqual(2, summary["by_surface"]["cog"]["required"])
        self.assertEqual(1, summary["by_client"]["Rasterio"]["passed"])
        self.assertEqual(1, summary["by_client"]["GDAL"]["not_addressable"])
        self.assertEqual(2, summary["scenario_facets"]["positive"]["required"])
        self.assertEqual(1, summary["supported_operation_coverage"]["passed"])

    def test_summary_excludes_wholly_non_addressable_operation_groups(self):
        ledger = module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [requirement(addressable=False)], [], CANDIDATE)
        self.assertEqual(0, module.build_summary(ledger)["supported_operation_coverage"]["required"])

    def test_observation_cannot_override_non_addressable_policy(self):
        req = requirement(addressable=False)
        fragments = [(Path("producer.json"), fragment("server", [observation(req, result="pass")]))]
        ledger = module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], fragments, CANDIDATE)
        cell = ledger["cells"][0]
        self.assertEqual("not-addressable", cell["result"])
        self.assertIsNone(cell["evidence_uri"])

    def test_candidate_selection_uses_release_cut_not_fragment_arrival(self):
        older_candidate = {
            "source_sha": "c" * 40,
            "image_digest": "sha256:" + "d" * 64,
            "cut_at": "2026-08-19T09:00:00Z",
        }
        delayed_old = fragment("delayed", [], generated="2026-08-21T12:00:00Z")
        delayed_old["candidate"] = older_candidate
        current = fragment("current", [], generated="2026-08-20T10:06:00Z")
        selected = module.choose_candidate(
            [(Path("delayed.json"), delayed_old), (Path("current.json"), current)],
            (None, None, None),
        )
        self.assertEqual(CANDIDATE, selected)

    def test_same_cut_with_conflicting_candidate_identity_is_rejected(self):
        conflicting = fragment("conflict", [])
        conflicting["candidate"] = {
            "source_sha": "c" * 40,
            "image_digest": "sha256:" + "d" * 64,
            "cut_at": CANDIDATE["cut_at"],
        }
        with self.assertRaisesRegex(ValueError, "ambiguous candidates"):
            module.choose_candidate(
                [(Path("a.json"), fragment("a", [])), (Path("b.json"), conflicting)],
                (None, None, None),
            )

    def test_equivalent_cut_offsets_share_candidate_identity(self):
        equivalent = fragment("equivalent", [])
        equivalent["candidate"] = {
            **CANDIDATE,
            "cut_at": "2026-08-20T11:00:00+02:00",
        }

        selected = module.choose_candidate(
            [(Path("utc.json"), fragment("utc", [])), (Path("offset.json"), equivalent)],
            (None, None, None),
        )

        self.assertEqual(SHA, selected["source_sha"])
        self.assertEqual(DIGEST, selected["image_digest"])
        self.assertEqual(
            module._timestamp(CANDIDATE["cut_at"]),
            module._timestamp(selected["cut_at"]),
        )

    def test_future_candidate_cut_is_rejected_before_selection(self):
        future = fragment("future", [], generated="2026-08-20T10:06:00Z")
        future["candidate"] = {
            "source_sha": "c" * 40,
            "image_digest": "sha256:" + "d" * 64,
            "cut_at": "2099-01-01T00:00:00Z",
        }
        with self.assertRaisesRegex(ValueError, "after fragment generation|in the future"):
            module.choose_candidate(
                [(Path("future.json"), future)],
                (None, None, None),
                now=datetime(2026, 8, 20, 10, 10, tzinfo=timezone.utc),
            )

    def test_conflicting_observations_tied_for_newest_are_rejected(self):
        req = requirement()
        tied_pass = observation(req, result="pass", completed="2026-08-20T10:05:00Z")
        tied_fail = observation(req, result="fail", completed="2026-08-20T10:05:00Z")
        fragments = [
            (Path("a.json"), fragment("server", [tied_pass])),
            (Path("b.json"), fragment("server", [tied_fail])),
        ]
        with self.assertRaisesRegex(ValueError, "tie for newest"):
            module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], fragments, CANDIDATE)

    def test_latest_observation_from_same_producer_wins(self):
        req = requirement()
        fragments = [
            (Path("old.json"), fragment("server", [observation(req, result="fail", completed="2026-08-20T10:01:00Z")])),
            (Path("new.json"), fragment("server", [observation(req, result="pass", completed="2026-08-20T10:05:00Z")])),
        ]
        ledger = module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], fragments, CANDIDATE)
        self.assertEqual("pass", ledger["cells"][0]["result"])

    def test_future_observation_is_rejected_before_latest_wins(self):
        req = requirement()
        poisoned = observation(req, result="pass", completed="2099-01-01T00:00:00Z")
        poisoned["started_at"] = "2098-12-31T23:59:00Z"
        doc = fragment("server", [poisoned], generated="2099-01-01T00:01:00Z")
        with self.assertRaisesRegex(ValueError, "in the future"):
            module.build_ledger(
                "rev-1",
                REQUIREMENTS_SOURCE_SHA,
                False,
                [req],
                [(Path("future.json"), doc)],
                CANDIDATE,
                now=datetime(2026, 8, 20, 10, 10, tzinfo=timezone.utc),
            )

    def test_observation_after_fragment_generation_is_rejected(self):
        req = requirement()
        late = observation(req, completed="2026-08-20T10:20:00Z")
        doc = fragment("server", [late], generated="2026-08-20T10:06:00Z")
        with self.assertRaisesRegex(ValueError, "after fragment generation"):
            module.build_ledger(
                "rev-1",
                REQUIREMENTS_SOURCE_SHA,
                False,
                [req],
                [(Path("late.json"), doc)],
                CANDIDATE,
                now=datetime(2026, 8, 20, 10, 30, tzinfo=timezone.utc),
            )

    def test_two_producers_for_same_cell_are_rejected(self):
        req = requirement()
        fragments = [
            (Path("a.json"), fragment("server", [observation(req)])),
            (Path("b.json"), fragment("sdk", [observation(req)])),
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous cross-producer"):
            module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], fragments, CANDIDATE)

    def test_unknown_observation_is_rejected(self):
        req = requirement()
        unknown = requirement(client="GDAL")
        fragments = [(Path("a.json"), fragment("server", [observation(unknown)]))]
        with self.assertRaisesRegex(ValueError, "do not resolve"):
            module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], fragments, CANDIDATE)

    def test_observation_must_match_fragment_candidate(self):
        req = requirement()
        mismatched = observation(req)
        mismatched["source_sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "does not match fragment candidate"):
            module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], [(Path("mismatch.json"), fragment("server", [mismatched]))], CANDIDATE)

    def test_observation_must_match_requirement_fixture(self):
        req = requirement()
        stale = observation(req)
        stale["fixture_revision"] = "stale"
        with self.assertRaisesRegex(ValueError, "fixture_revision does not match requirement"):
            module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], [(Path("stale.json"), fragment("server", [stale]))], CANDIDATE)

    def test_observation_must_match_requirement_revisions(self):
        req = requirement()
        for field in ("contract_revision", "auth_policy_revision"):
            with self.subTest(field=field):
                stale = observation(req)
                stale[field] = "stale"
                with self.assertRaisesRegex(ValueError, f"{field} does not match requirement"):
                    module.build_ledger(
                        "rev-1", REQUIREMENTS_SOURCE_SHA, False, [req],
                        [(Path("stale.json"), fragment("server", [stale]))], CANDIDATE,
                    )

    def test_passing_observation_requires_trusted_receipt_uri(self):
        req = requirement()
        for uri in (None, "", "file:///tmp/evidence.json", "https://attacker.invalid/evidence"):
            with self.subTest(uri=uri):
                invalid = observation(req)
                invalid["evidence_uri"] = uri
                with self.assertRaisesRegex(ValueError, "content-addressed by evidence_digest"):
                    module.build_ledger(
                        "rev-1", REQUIREMENTS_SOURCE_SHA, False, [req],
                        [(Path("invalid.json"), fragment("server", [invalid]))], CANDIDATE,
                    )

    def test_skipped_observation_cannot_smuggle_receipt_path(self):
        req = requirement()
        invalid = observation(req)
        invalid["result"] = "skip"
        invalid["skip_reason"] = "not executed"
        invalid["evidence_digest"] = "sha256:../../scripts/validate-site.py"
        with self.assertRaisesRegex(ValueError, "must not contain evidence"):
            module.build_ledger(
                "rev-1", REQUIREMENTS_SOURCE_SHA, False, [req],
                [(Path("invalid.json"), fragment("server", [invalid]))], CANDIDATE,
            )

    def test_passing_observation_requires_digest_bound_results_for_every_facet(self):
        req = requirement()
        invalid_observations = []
        missing = observation(req)
        missing["facet_results"].pop("positive")
        invalid_observations.append(missing)
        mismatched = observation(req)
        mismatched["facet_results"]["positive"]["evidence_digest"] = "sha256:" + "f" * 64
        invalid_observations.append(mismatched)
        failed = observation(req)
        failed["facet_results"]["positive"]["result"] = "fail"
        invalid_observations.append(failed)

        for invalid in invalid_observations:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "facet_results"):
                    module.build_ledger(
                        "rev-1", REQUIREMENTS_SOURCE_SHA, False, [req],
                        [(Path("invalid.json"), fragment("server", [invalid]))], CANDIDATE,
                    )

    def test_explicit_candidate_is_validated(self):
        with self.assertRaisesRegex(ValueError, "full 40-character SHA"):
            module.choose_candidate([], ("abc", DIGEST, CANDIDATE["cut_at"]))

    def test_cli_rejects_empty_required_candidate_values_before_loading_files(self):
        with self.assertRaises(SystemExit):
            module.main([
                "--requirements", "missing.json",
                "--requirements-source-revision", REQUIREMENTS_SOURCE_SHA,
                "--candidate-source-sha", "",
                "--candidate-image-digest", "",
                "--candidate-cut-at", "",
            ])

    def test_observation_from_older_candidate_is_ignored(self):
        req = requirement()
        unknown = requirement(client="GDAL")
        old = fragment("old", [observation(unknown)])
        old["observations"][0]["fixture_revision"] = "historical-fixture"
        del old["observations"][0]["contract_revision"]
        del old["observations"][0]["auth_policy_revision"]
        old["candidate"] = {
            "source_sha": "c" * 40,
            "image_digest": "sha256:" + "d" * 64,
            "cut_at": "2026-08-19T09:00:00Z",
        }
        fragments = [
            (Path("old.json"), old),
            (Path("current.json"), fragment("current", [])),
        ]

        ledger = module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], fragments, CANDIDATE)
        self.assertEqual("skip", ledger["cells"][0]["result"])

    def test_fragment_loader_rejects_non_string_candidate_identity(self):
        document = fragment("server", [])
        document["candidate"] = {**CANDIDATE, "source_sha": [SHA]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fragment.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "candidate.source_sha"):
                module.load_fragments(Path(tmp))

    def test_invalid_observation_result_is_rejected(self):
        req = requirement()
        for result in ("passed", "blocked", "not-addressable", None):
            with self.subTest(result=result):
                invalid = observation(req, result=result)
                with self.assertRaisesRegex(ValueError, "result must be one of"):
                    module.build_ledger(
                        "rev-1", REQUIREMENTS_SOURCE_SHA, False, [req],
                        [(Path("invalid.json"), fragment("server", [invalid]))], CANDIDATE,
                    )

    def test_skip_reason_is_required_only_for_skip(self):
        req = requirement()
        skipped = observation(req, result="skip")
        with self.assertRaisesRegex(ValueError, "required for a skipped result"):
            module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], [(Path("skip.json"), fragment("server", [skipped]))], CANDIDATE)

        passed = observation(req, result="pass")
        passed["skip_reason"] = "not actually skipped"
        with self.assertRaisesRegex(ValueError, "must be null"):
            module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, False, [req], [(Path("pass.json"), fragment("server", [passed]))], CANDIDATE)

    def test_requirements_loader_rejects_duplicate_denominator(self):
        req = requirement()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requirements.json"
            path.write_text(json.dumps({
                "schema": module.REQUIREMENTS_SCHEMA,
                "revision": "rev-1",
                "complete": False,
                "requirements": [req, req],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate requirement"):
                module.load_requirements(path)

    def test_requirements_loader_rejects_non_boolean_addressability(self):
        req = {**requirement(), "addressable_by_client": "false"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requirements.json"
            path.write_text(json.dumps({
                "schema": module.REQUIREMENTS_SCHEMA,
                "revision": "rev-1",
                "complete": False,
                "requirements": [req],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be a boolean"):
                module.load_requirements(path)


if __name__ == "__main__":
    unittest.main()
