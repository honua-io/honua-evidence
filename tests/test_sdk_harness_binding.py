"""Ruling sdk-harness-trunk-pinned-package-2026-10-27: each SDK certification harness runs from its
repository trunk (the HARNESS commit) against the exact pinned published package. Runs are looked up at
the harness pin; fragments still name the published source pin as producer_source_sha, and every
executed receipt binds identity.harness_commit to the run head inside its evidence digest."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from test_fetch_certification_producers import (
    MODULE, PINNED_SHA, NEWER_SHA, CANDIDATE, _archive, _artifact, _fragment, _run,
)
from test_aggregate_certification import (
    CANDIDATE as AGG_CANDIDATE, module as AGG, observation, rebind_receipt,
)
from test_dotnet_package_identity import DIGEST as NUPKG_DIGEST, PIN, dotnet_observation, dotnet_requirement, ledger

HARNESS_SHA = "9abcdef0" * 5
REGISTRY = Path(__file__).resolve().parents[1] / "config/protocol-certification-producers.v1.json"


def _configured(producer: str) -> dict:
    registry = MODULE.load_registry(REGISTRY)
    return copy.deepcopy(next(row for row in registry["sources"] if row["producer"] == producer))


class HarnessRegistryTests(unittest.TestCase):
    def test_sdk_producers_declare_their_harness_pin(self):
        keys = {row["producer"]: row.get("harness_revision_key")
                for row in MODULE.load_registry(REGISTRY)["sources"]}
        self.assertEqual(keys.pop("honua-sdk-dotnet"), "sdk-dotnet-harness")
        self.assertEqual(keys.pop("honua-sdk-python"), "sdk-python-harness")
        self.assertEqual(set(keys.values()), {None})

    def test_invalid_harness_revision_key_is_refused(self):
        base = _configured("honua-sdk-dotnet")
        for value in (True, "", 7, "sdk-dotnet", ["sdk-dotnet-harness"]):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                registry = Path(directory) / "registry.json"
                registry.write_text(json.dumps({"schema": MODULE.REGISTRY_SCHEMA,
                                                "sources": [{**base, "harness_revision_key": value}]}))
                with self.assertRaisesRegex(ValueError, "harness_revision_key"):
                    MODULE.load_registry(registry)
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "registry.json"
            interop = {**base, "normalizer": "client-interop-cert-v1"}
            interop.pop("candidate_tag_dispatch_title")
            registry.write_text(json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [interop]}))
            with self.assertRaisesRegex(ValueError, "harness_revision_key"):
                MODULE.load_registry(registry)


class HarnessFetchTests(unittest.TestCase):
    producer_name = "honua-sdk-dotnet"

    def setUp(self):
        self.source = _configured(self.producer_name)
        self.source["required"] = True
        repository = self.source["repository"]
        self.pins = {self.source["source_revision_key"]: PINNED_SHA,
                     self.source["harness_revision_key"]: HARNESS_SHA}
        self.run = _run(77, HARNESS_SHA)
        self.run.update(event="workflow_dispatch", head_branch=f"nightly-candidate/{HARNESS_SHA}",
                        repository={"full_name": repository}, head_repository={"full_name": repository},
                        path=self.source["workflow_path"],
                        display_title=f"{self.source['candidate_tag_dispatch_title']} [nightly-certification-55-1]")
        self.artifact = _artifact(88, 77, HARNESS_SHA)
        self.artifact["name"] = self.source["artifact_prefix"] + "77-1"
        self.member = self.source["fragment_globs"][0].replace("**/", "")
        self.payload = _fragment(PINNED_SHA)
        self.payload["producer"] = self.producer_name
        self.producer = {"repository": repository, "workflow": self.source["workflow_path"].rsplit("/", 1)[-1],
                         "run_id": 77, "run_attempt": 1, "dispatch_id": "nightly-certification-55-1"}
        self.payload["observations"][0].update(result="pass", evidence_receipt={
            "schema": "honua.certification-evidence-receipt/v2",
            "identity": {"producer_run": self.producer, "harness_commit": HARNESS_SHA,
                         "producer_source_sha": PINNED_SHA},
            "result": "pass",
        })
        self.tag_sha = HARNESS_SHA
        self.listed = []

    def drive(self):
        receipt = self.payload["observations"][0]["evidence_receipt"]
        if receipt is not None:
            self.payload["observations"][0]["evidence_digest"] = MODULE._receipt_digest(receipt)
        archive = _archive({self.member: self.payload})
        self.artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()

        def request(url, token, accept):
            if "/git/ref/tags/" in url:
                tag = url.rsplit("/", 1)[-1].replace("%2F", "/")
                return json.dumps({"ref": f"refs/tags/{tag}",
                                   "object": {"type": "commit", "sha": self.tag_sha}}).encode()
            if "/git/ref/heads/" in url:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            if url == self.artifact["archive_download_url"]:
                return archive
            raise AssertionError(url)

        def runs(repository, workflow_path, head_sha, token):
            self.listed.append(head_sha)
            return [self.run]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "registry.json"
            registry.write_text(json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [self.source]}))
            with mock.patch.object(MODULE, "list_runs_at_revision", side_effect=runs), \
                 mock.patch.object(MODULE, "list_run_artifacts", return_value=[self.artifact]), \
                 mock.patch.object(MODULE, "request_bytes", side_effect=request):
                MODULE.fetch(registry, root / "out", "token", self.pins, candidate=CANDIDATE)
            return [json.loads(line) for line in (root / "out/.fetch-manifest.ndjson").read_text().splitlines()]

    def test_harness_run_with_published_pin_fragment_is_fetched(self):
        rows = self.drive()
        self.assertEqual(self.listed, [HARNESS_SHA])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["head_sha"], HARNESS_SHA)
        self.assertEqual(rows[0]["pinned_source_sha"], PINNED_SHA)
        self.assertEqual(rows[0]["harness_revision_key"], self.source["harness_revision_key"])
        self.assertEqual(rows[0]["harness_source_sha"], HARNESS_SHA)
        self.assertTrue(rows[0]["candidate_tag_verified"])

    def test_harness_commit_that_is_not_the_run_head_is_refused(self):
        for value in (NEWER_SHA, PINNED_SHA, HARNESS_SHA.upper(), None):
            with self.subTest(value=value):
                self.setUp()
                self.payload["observations"][0]["evidence_receipt"]["identity"]["harness_commit"] = value
                with self.assertRaisesRegex(ValueError, "harness_commit"):
                    self.drive()
        self.setUp()
        del self.payload["observations"][0]["evidence_receipt"]["identity"]["harness_commit"]
        with self.assertRaisesRegex(ValueError, "harness_commit"):
            self.drive()

    def test_fragment_naming_the_harness_as_producer_source_is_refused(self):
        self.payload["observations"][0]["producer_source_sha"] = HARNESS_SHA
        with self.assertRaisesRegex(ValueError, "producer_source_sha"):
            self.drive()

    def test_executed_observation_without_receipt_is_refused(self):
        self.payload["observations"][0]["evidence_receipt"] = None
        with self.assertRaisesRegex(ValueError, "(?i)harness"):
            self.drive()

    def test_run_or_tag_off_the_harness_pin_is_refused(self):
        for label, change in {
            "run at published pin": lambda: (self.run.update(head_sha=PINNED_SHA,
                                                              head_branch=f"nightly-candidate/{PINNED_SHA}"),
                                             self.artifact["workflow_run"].update(head_sha=PINNED_SHA)),
            "tag at published pin": lambda: setattr(self, "tag_sha", PINNED_SHA),
            "tag name of published pin": lambda: self.run.update(head_branch=f"nightly-candidate/{PINNED_SHA}"),
        }.items():
            with self.subTest(label=label):
                self.setUp()
                change()
                with self.assertRaisesRegex(ValueError, "no normalized fragments"):
                    self.drive()

    def test_catalog_without_harness_pin_falls_back_to_the_published_pin(self):
        del self.pins[self.source["harness_revision_key"]]
        self.run.update(head_sha=PINNED_SHA, head_branch=f"nightly-candidate/{PINNED_SHA}")
        self.artifact["workflow_run"]["head_sha"] = PINNED_SHA
        self.tag_sha = PINNED_SHA
        del self.payload["observations"][0]["evidence_receipt"]["identity"]["harness_commit"]
        rows = self.drive()
        self.assertEqual(self.listed, [PINNED_SHA])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["head_sha"], PINNED_SHA)
        self.assertNotIn("harness_source_sha", rows[0])


class PythonHarnessFetchTests(HarnessFetchTests):
    producer_name = "honua-sdk-python"


HARNESS_RECEIPT_FIXTURE = Path(__file__).parent / "fixtures/python-receipt-v2-seam.json"


class HarnessReceiptAggregationTests(unittest.TestCase):
    def dotnet(self, **identity):
        req = dotnet_requirement()
        observed = dotnet_observation(req)
        observed["evidence_receipt"]["identity"].update(identity)
        rebind_receipt(observed)
        return req, observed

    def test_dotnet_receipt_binding_harness_and_package_aggregates(self):
        req, observed = self.dotnet(harness_commit=HARNESS_SHA, client_package="Honua.Sdk",
                                    client_package_digest=NUPKG_DIGEST)
        self.assertTrue(AGG._valid_receipt(observed, req))
        for pin in (None, PIN):
            with self.subTest(pin=pin):
                cell = ledger(req, copy.deepcopy(observed), pin)["cells"][0]
                self.assertEqual(cell["result"], "pass")
                self.assertEqual(cell["evidence_receipt"]["identity"]["harness_commit"], HARNESS_SHA)
                self.assertEqual(cell["client_package_digest"], NUPKG_DIGEST)

    def test_python_receipt_binding_harness_aggregates(self):
        bundle = json.loads(HARNESS_RECEIPT_FIXTURE.read_text())
        row, req = bundle["fragment"]["observations"][0], bundle["requirements"]["requirements"][0]
        row["evidence_receipt"]["identity"]["harness_commit"] = HARNESS_SHA
        rebind_receipt(row)
        self.assertTrue(AGG._valid_receipt(row, req, AGG_CANDIDATE["cut_at"], "python-v2-seam-test"))
        for value in (HARNESS_SHA.upper(), HARNESS_SHA[:39], 7, None):
            with self.subTest(value=value):
                wrong = copy.deepcopy(row)
                wrong["evidence_receipt"]["identity"]["harness_commit"] = value
                rebind_receipt(wrong)
                self.assertFalse(AGG._valid_receipt(wrong, req, AGG_CANDIDATE["cut_at"], "python-v2-seam-test"))

    def test_harness_commit_outside_the_digest_is_refused(self):
        req, observed = self.dotnet(harness_commit=HARNESS_SHA)
        observed["evidence_receipt"]["identity"]["harness_commit"] = NEWER_SHA
        with self.assertRaisesRegex(ValueError, "do not match evidence_digest"):
            ledger(req, observed)

    def test_wrong_package_digest_in_receipt_identity_is_refused(self):
        for identity in ({"client_package": "Honua.Sdk", "client_package_digest": "sha256:" + "5" * 64},
                         {"client_package": "Honua.Sdk.Fork", "client_package_digest": NUPKG_DIGEST},
                         {"client_package_digest": NUPKG_DIGEST}, {"client_package": "Honua.Sdk"}):
            with self.subTest(identity=identity):
                req, observed = self.dotnet(harness_commit=HARNESS_SHA, **identity)
                self.assertFalse(AGG._valid_receipt(observed, req))
                with self.assertRaisesRegex(ValueError, "not semantically bound"):
                    ledger(req, observed)
        req, observed = self.dotnet(harness_commit=HARNESS_SHA, client_package="Honua.Sdk",
                                    client_package_digest=NUPKG_DIGEST)
        with self.assertRaisesRegex(ValueError, "pinned NuGet artifact"):
            ledger(req, observed, {**PIN, "digest": "sha256:" + "5" * 64})

    def test_package_identity_requires_the_observed_package(self):
        req = dotnet_requirement()
        observed = observation(req)
        observed["evidence_receipt"]["identity"].update(client_package="Honua.Sdk",
                                                        client_package_digest=NUPKG_DIGEST)
        rebind_receipt(observed)
        self.assertFalse(AGG._valid_receipt(observed, req))

    def test_unknown_identity_field_stays_refused(self):
        for field in ("harness_ref", "sdk_source_sha", "client_package_version"):
            with self.subTest(field=field):
                req, observed = self.dotnet(harness_commit=HARNESS_SHA, **{field: "x"})
                self.assertFalse(AGG._valid_receipt(observed, req))

    def test_other_lanes_cannot_carry_harness_or_package_identity(self):
        from test_aggregate_certification import requirement
        req = requirement()
        for identity in ({"harness_commit": HARNESS_SHA},
                         {"client_package": "Honua.Sdk", "client_package_digest": NUPKG_DIGEST}):
            with self.subTest(identity=identity):
                observed = observation(req)
                observed["evidence_receipt"]["identity"].update(identity)
                rebind_receipt(observed)
                self.assertFalse(AGG._valid_receipt(observed, req))
        bundle = json.loads(HARNESS_RECEIPT_FIXTURE.read_text())
        row, py_req = bundle["fragment"]["observations"][0], bundle["requirements"]["requirements"][0]
        row["evidence_receipt"]["identity"]["client_package_digest"] = NUPKG_DIGEST
        rebind_receipt(row)
        self.assertFalse(AGG._valid_receipt(row, py_req, AGG_CANDIDATE["cut_at"], "python-v2-seam-test"))


if __name__ == "__main__":
    unittest.main()
