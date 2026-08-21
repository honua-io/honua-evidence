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

    def test_observation_cannot_override_non_addressable_policy(self):
        req = requirement(addressable=False)
        fragments = [(Path("producer.json"), fragment("server", [observation(req, result="pass")]))]
        ledger = module.build_ledger("rev-1", False, [req], fragments, CANDIDATE)
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
            module.build_ledger("rev-1", False, [req], fragments, CANDIDATE)

    def test_latest_observation_from_same_producer_wins(self):
        req = requirement()
        fragments = [
            (Path("old.json"), fragment("server", [observation(req, result="fail", completed="2026-08-20T10:01:00Z")])),
            (Path("new.json"), fragment("server", [observation(req, result="pass", completed="2026-08-20T10:05:00Z")])),
        ]
        ledger = module.build_ledger("rev-1", False, [req], fragments, CANDIDATE)
        self.assertEqual("pass", ledger["cells"][0]["result"])

    def test_future_observation_is_rejected_before_latest_wins(self):
        req = requirement()
        poisoned = observation(req, result="pass", completed="2099-01-01T00:00:00Z")
        poisoned["started_at"] = "2098-12-31T23:59:00Z"
        doc = fragment("server", [poisoned], generated="2099-01-01T00:01:00Z")
        with self.assertRaisesRegex(ValueError, "in the future"):
            module.build_ledger(
                "rev-1",
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
