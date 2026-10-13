from __future__ import annotations

import copy
import unittest
from pathlib import Path

from test_aggregate_certification import module, requirement, observation, rebind_receipt, as_receipt_v2, fragment, CANDIDATE, REQUIREMENTS_SOURCE_SHA


class ProducerRunReceiptTests(unittest.TestCase):
    def setUp(self):
        self.req = requirement(client="@honua/sdk-js")
        self.req["client_lane"] = "sdk-js-certification"
        self.observed = observation(self.req)
        self.identity = {"repository": "honua-io/honua-sdk-js", "workflow": "integration.yml",
                         "run_id": 77, "run_attempt": 2, "dispatch_id": "nightly-certification-55-1"}
        self.observed["evidence_receipt"]["identity"]["producer_run"] = self.identity
        rebind_receipt(self.observed)

    def test_explicit_identity_survives_canonical_receipt_addressing(self):
        self.assertTrue(module._valid_receipt(self.observed, self.req))
        raw = module._receipt_bytes(self.observed["evidence_receipt"])
        import json
        self.assertEqual(json.loads(raw)["identity"]["producer_run"], self.identity)
        import hashlib
        self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(), self.observed["evidence_digest"])
        version2 = as_receipt_v2(copy.deepcopy(self.observed), self.req)
        self.assertTrue(module._valid_receipt(version2, self.req, None, "rev-1"))

    def test_full_ledger_preserves_producer_identity_and_original_digest(self):
        for observed in (self.observed, as_receipt_v2(copy.deepcopy(self.observed), self.req)):
            ledger = module.build_ledger(
                "rev-1", REQUIREMENTS_SOURCE_SHA, True, [self.req],
                [(Path("sdk.json"), fragment("honua-sdk-js", [observed]))], CANDIDATE,
            )
            cell = ledger["cells"][0]
            self.assertEqual(cell["evidence_receipt"]["identity"]["producer_run"], self.identity)
            self.assertEqual(cell["evidence_digest"], observed["evidence_digest"])
            self.assertEqual(module._receipt_bytes(cell["evidence_receipt"]),
                             module._receipt_bytes(observed["evidence_receipt"]))

    def test_identity_contract_rejects_malformed_or_unrelated_fields(self):
        for key, value in [("repository", "other/fork"), ("workflow", "other.yml"),
                           ("run_id", True), ("run_id", "77"), ("run_attempt", 0),
                           ("dispatch_id", "manual-55"), ("dispatch_id", "nightly-certification-0-1"),
                           ("unexpected", "value")]:
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(self.observed)
                changed["evidence_receipt"]["identity"]["producer_run"][key] = value
                rebind_receipt(changed)
                self.assertFalse(module._valid_receipt(changed, self.req))
        for key in self.identity:
            with self.subTest(missing=key):
                changed = copy.deepcopy(self.observed)
                del changed["evidence_receipt"]["identity"]["producer_run"][key]
                rebind_receipt(changed)
                self.assertFalse(module._valid_receipt(changed, self.req))

    def test_does_not_relax_other_identity_or_client_contracts(self):
        self.observed["evidence_receipt"]["identity"]["unreviewed"] = True
        rebind_receipt(self.observed)
        self.assertFalse(module._valid_receipt(self.observed, self.req))
        other = requirement()
        observed = observation(other)
        observed["evidence_receipt"]["identity"]["producer_run"] = self.identity
        rebind_receipt(observed)
        self.assertFalse(module._valid_receipt(observed, other))
