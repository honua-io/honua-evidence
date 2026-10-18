"""Synthetic intake controls; none are candidate qualification receipts."""
import base64
from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest

from test_aggregate_certification import module, requirement, observation, rebind_receipt, fragment, CANDIDATE, REQUIREMENTS_SOURCE_SHA

SPEC = importlib.util.spec_from_file_location("grpc_digest_fetch", Path(__file__).resolve().parents[1] / "scripts/fetch-certification-producers.py")
FETCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FETCH)


def example(lane):
    req = requirement("Generated gRPC fixture")
    req.update(surface="grpc", operation="FeatureService/QueryFeatures", client_lane=lane,
               client_version="1.0.3", capability_key="grpc.feature-service",
               client_package_digest=("sha512-" + base64.b64encode(b"x" * 64).decode()
                                      if lane == "grpc-typescript" else "sha256:" + "f" * 64))
    observed = observation(req)
    observed["client_package_digest"] = req["client_package_digest"]
    receipt = observed["evidence_receipt"]
    receipt["schema"] = "honua.certification-evidence-receipt/v2"
    receipt["identity"].update(client_package_digest=observed["client_package_digest"],
                               maturity="supported", required_tier="nightly", requirements_revision="fixture-v2")
    return req, rebind_receipt(observed)


class GrpcPackageDigestTests(unittest.TestCase):
    def join(self, req, observed):
        return module.build_ledger("fixture-v2", REQUIREMENTS_SOURCE_SHA, True, [req],
                                   [(Path("fixture.json"), fragment("geospatial-grpc", [observed]))], CANDIDATE)

    def test_sha256_and_sri_original_identity_survive_intake_and_join(self):
        for lane in ("grpc-dotnet", "grpc-python", "grpc-typescript"):
            with self.subTest(lane=lane):
                req, observed = example(lane)
                self.assertTrue(module._valid_receipt(observed, req, requirements_revision="fixture-v2"))
                FETCH.validate_fragment_producer(fragment("geospatial-grpc", [observed]), "geospatial-grpc", "c" * 40,
                                                 "honua-io/geospatial-grpc")
                cell = self.join(req, observed)["cells"][0]
                self.assertEqual("pass", cell["result"])
                self.assertEqual(req["client_package_digest"], cell["client_package_digest"])
                self.assertEqual(observed["evidence_digest"], cell["evidence_digest"])
                self.assertEqual(observed["evidence_receipt"], cell["evidence_receipt"])

    def test_missing_wrong_and_internally_consistent_repacked_bytes_are_refused(self):
        req, original = example("grpc-dotnet")
        for change in ("missing-observation", "wrong-observation", "missing-receipt", "wrong-receipt", "repacked"):
            with self.subTest(change=change):
                observed = deepcopy(original)
                if change == "missing-observation":
                    observed.pop("client_package_digest")
                elif change in {"wrong-observation", "repacked"}:
                    observed["client_package_digest"] = "sha256:" + "0" * 64
                if change == "missing-receipt":
                    observed["evidence_receipt"]["identity"].pop("client_package_digest")
                elif change in {"wrong-receipt", "repacked"}:
                    observed["evidence_receipt"]["identity"]["client_package_digest"] = "sha256:" + "0" * 64
                rebind_receipt(observed)
                self.assertFalse(module._valid_receipt(observed, req, requirements_revision="fixture-v2"))
                with self.assertRaises(ValueError):
                    self.join(req, observed)

    def test_legacy_grpc_receipt_cannot_use_digest_to_bypass_v2_context(self):
        req, observed = example("grpc-dotnet")
        observed["evidence_receipt"]["schema"] = "honua.certification-evidence-receipt/v1"
        for field in ("maturity", "required_tier", "requirements_revision"):
            observed["evidence_receipt"]["identity"].pop(field)
        rebind_receipt(observed)
        self.assertFalse(module._valid_receipt(observed, req, requirements_revision="fixture-v2"))
        with self.assertRaises(ValueError):
            self.join(req, observed)

    def test_fetch_refuses_missing_or_different_observed_receipt_digest(self):
        _, observed = example("grpc-typescript")
        for field in ("observation", "receipt"):
            broken = deepcopy(observed)
            if field == "observation":
                broken.pop("client_package_digest")
            else:
                broken["evidence_receipt"]["identity"].pop("client_package_digest")
            with self.subTest(field=field), self.assertRaises(ValueError):
                FETCH.validate_fragment_producer(fragment("geospatial-grpc", [broken]), "geospatial-grpc", "c" * 40)

    def test_absent_execution_remains_a_required_skip_without_fabricated_receipt(self):
        req, _ = example("grpc-dotnet")
        ledger = module.build_ledger("fixture-v2", REQUIREMENTS_SOURCE_SHA, True, [req], [], CANDIDATE)
        cell = ledger["cells"][0]
        self.assertEqual("skip", cell["result"])
        self.assertIsNone(cell["evidence_receipt"])
        self.assertIsNone(cell["producer_source_sha"])
        self.assertEqual(req["client_package_digest"], cell["client_package_digest"])


if __name__ == "__main__":
    unittest.main()
