"""Founder decision D7 (honua-evidence#64): the .NET certification producer's installed NuGet
identity is accepted, verified against the pinned artifact when one is known, and preserved."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from test_aggregate_certification import (
    CANDIDATE, REQUIREMENTS_SOURCE_SHA, fragment, module, observation, rebind_receipt, requirement,
)

DIGEST = "sha256:" + "4" * 64
PIN = {"package": "Honua.Sdk", "version": "1.0", "digest": DIGEST}
MANIFEST = """\
components:
  honua-sdk-dotnet:
    version: "9.9.9"
clientArtifacts:
  honua-sdk-js:
    package: "@honua/sdk-js"
    digest: "sha512-x"
  honua-sdk-dotnet:
    ecosystem: nuget
    package: "Honua.Sdk"
    version: "1.12.0"
    digest: "sha256:45e63f896a524ae4c620bef4065b632a270a79e4cfa274a349f2ffbccb8f051d"
    targets: [net10.0]
  honua-sdk-python:
    package: "honua-sdk"
"""


def dotnet_requirement():
    req = requirement(client="Honua SDK .NET")
    req["client_lane"] = "sdk-dotnet-certification"
    return req


def dotnet_observation(req, **fields):
    observed = observation(req)
    observed.update({"client_package": "Honua.Sdk", "client_package_digest": DIGEST, **fields})
    rebind_receipt(observed)
    return observed


def ledger(req, observed, pin=None):
    return module.build_ledger(
        "rev-1", REQUIREMENTS_SOURCE_SHA, True, [req],
        [(Path("dotnet.json"), fragment("honua-sdk-dotnet", [observed]))], CANDIDATE,
        expected_sdk_dotnet=pin,
    )


class DotnetPackageIdentityTests(unittest.TestCase):
    def setUp(self):
        self.req = dotnet_requirement()

    def test_carried_identity_aggregates_and_both_fields_reach_the_cell(self):
        for pin in (None, PIN):
            with self.subTest(pin=pin):
                cell = ledger(self.req, dotnet_observation(self.req), pin)["cells"][0]
                self.assertEqual(cell["result"], "pass")
                self.assertEqual(cell["client_package"], "Honua.Sdk")
                self.assertEqual(cell["client_package_digest"], DIGEST)

    def test_receipt_identity_stays_closed(self):
        observed = dotnet_observation(self.req)
        self.assertTrue(module._valid_receipt(observed, self.req))
        self.assertNotIn("client_package", observed["evidence_receipt"]["identity"])
        observed["evidence_receipt"]["identity"]["client_package_digest"] = DIGEST
        rebind_receipt(observed)
        self.assertFalse(module._valid_receipt(observed, self.req))

    def test_producer_that_sends_neither_field_is_unchanged(self):
        cell = ledger(self.req, observation(self.req), PIN)["cells"][0]
        self.assertEqual(cell["result"], "pass")
        self.assertNotIn("client_package", cell)
        self.assertNotIn("client_package_digest", cell)

    def test_partial_null_wrong_or_noncanonical_identity_is_refused(self):
        cases = [
            {"client_package": None}, {"client_package_digest": None},
            {"client_package": "Honua.Sdk.Fork"}, {"client_package_digest": "sha256:" + "A" * 64},
            {"client_package_digest": "sha512-" + "a" * 86}, {"client_package_digest": "4" * 64},
        ]
        for change in cases:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, "NuGet"):
                    ledger(self.req, dotnet_observation(self.req, **change))
        for missing in ("client_package", "client_package_digest"):
            with self.subTest(missing=missing):
                observed = dotnet_observation(self.req)
                del observed[missing]
                with self.assertRaisesRegex(ValueError, "NuGet"):
                    ledger(self.req, observed)

    def test_identity_that_differs_from_the_pin_is_refused(self):
        for pin in ({**PIN, "digest": "sha256:" + "5" * 64}, {**PIN, "version": "2.0"},
                    {**PIN, "package": "Other.Sdk"}):
            with self.subTest(pin=pin):
                with self.assertRaisesRegex(ValueError, "pinned NuGet artifact"):
                    ledger(self.req, dotnet_observation(self.req), pin)

    def test_other_lanes_keep_the_refusal(self):
        other = requirement()
        observed = observation(other)
        observed.update({"client_package": "Honua.Sdk", "client_package_digest": DIGEST})
        rebind_receipt(observed)
        with self.assertRaisesRegex(ValueError, "only permitted"):
            module.build_ledger("rev-1", REQUIREMENTS_SOURCE_SHA, True, [other],
                                [(Path("x.json"), fragment("honua-server-cng", [observed]))], CANDIDATE)

    def test_governed_rows_cannot_carry_the_observed_identity(self):
        for field, value in (("client_package", "Honua.Sdk"), ("client_package_digest", DIGEST)):
            with self.subTest(field=field):
                req = {**dotnet_requirement(), field: value}
                with self.assertRaises(ValueError):
                    ledger(req, dotnet_observation(req))

    def test_manifest_pin_is_read_from_client_artifacts_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform-manifest.yaml"
            path.write_text(MANIFEST, encoding="utf-8")
            self.assertEqual(module.load_sdk_dotnet_pin(path), {
                "package": "Honua.Sdk", "version": "1.12.0",
                "digest": "sha256:45e63f896a524ae4c620bef4065b632a270a79e4cfa274a349f2ffbccb8f051d",
            })
            for broken in (MANIFEST.replace('    digest: "sha256:45e6', '    digest: "sha256:XX'),
                           MANIFEST.replace('    package: "Honua.Sdk"\n', ""),
                           MANIFEST.replace('    version: "1.12.0"\n', '    version: "1.12.0"\n    version: "1.13.0"\n')):
                with self.subTest(broken=broken[-80:]):
                    path.write_text(broken, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        module.load_sdk_dotnet_pin(path)
            path.write_text(MANIFEST.replace("  honua-sdk-dotnet:\n    ecosystem", "  other:\n    ecosystem"),
                            encoding="utf-8")
            self.assertIsNone(module.load_sdk_dotnet_pin(path))

    def test_cli_verifies_against_the_manifest_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "platform-manifest.yaml"
            manifest.write_text(MANIFEST.replace("1.12.0", "1.0").replace(
                "45e63f896a524ae4c620bef4065b632a270a79e4cfa274a349f2ffbccb8f051d", "4" * 64), encoding="utf-8")
            requirements = root / "requirements.json"
            import json
            requirements.write_text(json.dumps({
                "schema": module.REQUIREMENTS_SCHEMA, "revision": "rev-1", "complete": True,
                "requirements": [self.req]}), encoding="utf-8")
            producers = root / "producers"
            producers.mkdir()
            observed = dotnet_observation(self.req)
            (producers / "dotnet.json").write_text(json.dumps(fragment("honua-sdk-dotnet", [observed])),
                                                   encoding="utf-8")
            args = ["--requirements", str(requirements), "--requirements-source-revision", REQUIREMENTS_SOURCE_SHA,
                    "--producers", str(producers), "--output", str(root / "out" / "ledger.json"),
                    "--summary", str(root / "out" / "summary.json"),
                    "--candidate-source-sha", CANDIDATE["source_sha"],
                    "--candidate-image-digest", CANDIDATE["image_digest"],
                    "--candidate-cut-at", CANDIDATE["cut_at"], "--sdk-dotnet-pin-manifest", str(manifest),
                    "--cloud-native-inventory",
                    str(Path(__file__).resolve().parents[1] / "config/cloud-native-client-inventory.v1.json")]
            self.assertEqual(module.main(args), 0)
            cell = json.loads((root / "out" / "ledger.json").read_text(encoding="utf-8"))["cells"][0]
            self.assertEqual((cell["client_package"], cell["client_package_digest"]), ("Honua.Sdk", DIGEST))
            mismatched = copy.deepcopy(observed)
            mismatched["client_package_digest"] = "sha256:" + "5" * 64
            rebind_receipt(mismatched)
            (producers / "dotnet.json").write_text(json.dumps(fragment("honua-sdk-dotnet", [mismatched])),
                                                   encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "pinned NuGet artifact"):
                module.main(args)


if __name__ == "__main__":
    unittest.main()
