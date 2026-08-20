from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "aggregate-certification.py"
SPEC = importlib.util.spec_from_file_location("aggregate_certification", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
CANDIDATE = {"source_sha": SHA, "image_digest": DIGEST, "cut_at": "2026-08-20T09:00:00Z"}


def requirement(client="Rasterio", addressable=True):
    return {
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
        "addressable_by_client": addressable,
        "addressability_reason": None if addressable else "client has no operation",
        "scenario_facets": ["positive", "range-efficiency"],
        "contract_revision": "cog-1.0",
        "auth_policy_revision": "anonymous-v1",
    }


def observation(req, result="pass", completed="2026-08-20T10:05:00Z"):
    value = {field: req[field] for field in module.IDENTITY_FIELDS}
    value.update({
        "result": result,
        "skip_reason": None,
        "source_sha": SHA,
        "image_digest": DIGEST,
        "fixture_revision": "fixture-v1",
        "evidence_uri": "https://evidence.honua.io/run/1",
        "started_at": "2026-08-20T10:00:00Z",
        "completed_at": completed,
    })
    return value


def fragment(producer, observations, generated="2026-08-20T10:06:00Z"):
    return {
        "schema": module.FRAGMENT_SCHEMA,
        "producer": producer,
        "generated_at": generated,
        "candidate": CANDIDATE,
        "observations": observations,
    }


class CertificationAggregationTests(unittest.TestCase):
    def test_missing_observation_materializes_skip(self):
        ledger = module.build_ledger("rev-1", False, [requirement()], [], CANDIDATE)
        self.assertEqual("skip", ledger["cells"][0]["result"])
        self.assertIn("no producer evidence", ledger["cells"][0]["skip_reason"])

    def test_non_addressable_requirement_materializes_truthful_result(self):
        ledger = module.build_ledger("rev-1", False, [requirement(addressable=False)], [], CANDIDATE)
        self.assertEqual("not-addressable", ledger["cells"][0]["result"])

    def test_latest_observation_from_same_producer_wins(self):
        req = requirement()
        fragments = [
            (Path("old.json"), fragment("server", [observation(req, result="fail", completed="2026-08-20T10:01:00Z")])),
            (Path("new.json"), fragment("server", [observation(req, result="pass", completed="2026-08-20T10:05:00Z")])),
        ]
        ledger = module.build_ledger("rev-1", False, [req], fragments, CANDIDATE)
        self.assertEqual("pass", ledger["cells"][0]["result"])

    def test_two_producers_for_same_cell_are_rejected(self):
        req = requirement()
        fragments = [
            (Path("a.json"), fragment("server", [observation(req)])),
            (Path("b.json"), fragment("sdk", [observation(req)])),
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous cross-producer"):
            module.build_ledger("rev-1", False, [req], fragments, CANDIDATE)

    def test_unknown_observation_is_rejected(self):
        req = requirement()
        unknown = requirement(client="GDAL")
        fragments = [(Path("a.json"), fragment("server", [observation(unknown)]))]
        with self.assertRaisesRegex(ValueError, "do not resolve"):
            module.build_ledger("rev-1", False, [req], fragments, CANDIDATE)

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


if __name__ == "__main__":
    unittest.main()
