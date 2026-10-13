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
    MODULE, PINNED_SHA, NEWER_SHA, CANDIDATE, _archive, _artifact, _fragment, _run, _sdk_js_source,
)


class CandidateTagProducerTests(unittest.TestCase):
    def setUp(self):
        self.source = _sdk_js_source(
            trusted_events=["schedule", "workflow_dispatch"],
            candidate_tag_dispatch_title="SDK Integration",
        )
        self.run = _run(77, PINNED_SHA)
        self.run.update(event="workflow_dispatch", head_branch=f"nightly-candidate/{PINNED_SHA}",
                        repository={"full_name": self.source["repository"]},
                        display_title="SDK Integration [nightly-certification-55-1]")
        self.artifact = _artifact(88, 77, PINNED_SHA)
        self.payload = _fragment(PINNED_SHA)
        self.producer = {"repository": self.source["repository"], "workflow": "integration.yml",
                         "run_id": 77, "run_attempt": 1, "dispatch_id": "nightly-certification-55-1"}
        self.payload["observations"][0].update(result="pass", evidence_receipt={
            "schema": "honua.certification-evidence-receipt/v1",
            "identity": {"producer_run": self.producer}, "result": "pass",
        })
        self.ref = {"ref": f"refs/tags/nightly-candidate/{PINNED_SHA}",
                    "object": {"type": "commit", "sha": PINNED_SHA}}
        self.branch_exists = False
        self.bad_archive = False
        self.add_old_candidate = False
        self.calls = []

    def drive(self):
        receipt = self.payload["observations"][0]["evidence_receipt"]
        if receipt is not None:
            self.payload["observations"][0]["evidence_digest"] = MODULE._receipt_digest(receipt)
        archive = _archive({"protocol-certification-fragment.json": self.payload})
        self.artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
        if self.bad_archive:
            self.artifact["digest"] = "sha256:" + "0" * 64

        artifacts = [self.artifact]
        archives = {self.artifact["archive_download_url"]: archive}
        if self.add_old_candidate:
            old = copy.deepcopy(self.payload)
            old["candidate"]["source_sha"] = NEWER_SHA
            old_archive = _archive({"protocol-certification-fragment.json": old})
            old_artifact = _artifact(89, 77, PINNED_SHA)
            old_artifact.update(created_at="2026-08-22T07:40:00Z",
                                digest="sha256:" + hashlib.sha256(old_archive).hexdigest())
            artifacts.insert(0, old_artifact)
            archives[old_artifact["archive_download_url"]] = old_archive

        def request(url, token, accept):
            self.calls.append(url)
            if "/git/ref/tags/" in url:
                return json.dumps(self.ref).encode()
            if "/git/ref/heads/" in url:
                if not self.branch_exists:
                    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
                return json.dumps({"ref": "refs/heads/" + self.run["head_branch"]}).encode()
            if url in archives:
                return archives[url]
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "registry.json"
            registry.write_text(json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [self.source]}))
            with mock.patch.object(MODULE, "list_runs_at_revision", return_value=[self.run]), \
                 mock.patch.object(MODULE, "list_run_artifacts", return_value=artifacts), \
                 mock.patch.object(MODULE, "request_bytes", side_effect=request):
                MODULE.fetch(registry, root / "out", "token", {self.source["source_revision_key"]: PINNED_SHA}, candidate=CANDIDATE)
            return [json.loads(line) for line in (root / "out/.fetch-manifest.ndjson").read_text().splitlines()]

    def test_exact_tag_fetch_preserves_verified_source_and_attempt(self):
        rows = self.drive()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pinned_source_sha"], PINNED_SHA)
        self.assertTrue(rows[0]["candidate_tag_verified"])
        self.assertEqual(rows[0]["run_attempt"], 1)
        self.assertEqual(len([url for url in self.calls if "/git/ref/" in url]), 2)

    def test_other_candidate_does_not_hide_requested_candidate_at_usable_limit(self):
        self.source.update(max_artifacts=1, max_candidates=2)
        self.add_old_candidate = True
        rows = self.drive()
        self.assertEqual([row["artifact_id"] for row in rows], [88])

    def test_tag_requires_explicit_registry_opt_in(self):
        self.source.pop("candidate_tag_dispatch_title")
        with self.assertRaisesRegex(ValueError, "no normalized fragments"):
            self.drive()
        self.assertEqual(self.calls, [])

    def test_ref_and_run_adversaries_refused(self):
        changes = {
            "arbitrary tag": lambda: self.run.update(head_branch="v0.2.0"),
            "wrong pin": lambda: self.run.update(head_sha=NEWER_SHA),
            "branch lookalike": lambda: setattr(self, "branch_exists", True),
            "wrong ref": lambda: self.ref.update(ref="refs/heads/trunk"),
            "wrong tag commit": lambda: self.ref["object"].update(sha=NEWER_SHA),
            "annotated tag": lambda: self.ref["object"].update(type="tag"),
            "push event": lambda: self.run.update(event="push"),
            "foreign repo": lambda: self.run.update(repository={"full_name": "other/fork"}),
            "foreign head repo": lambda: self.run.update(head_repository={"full_name": "other/fork"}),
            "wrong workflow": lambda: self.run.update(path=".github/workflows/other.yml"),
            "unaccepted conclusion": lambda: self.run.update(conclusion="cancelled"),
            "unfinished run": lambda: self.run.update(status="in_progress"),
            "wrong artifact run": lambda: self.artifact["workflow_run"].update(id=78),
            "wrong artifact source": lambda: self.artifact["workflow_run"].update(head_sha=NEWER_SHA),
            "stale artifact": lambda: self.artifact.update(created_at="2026-08-22T07:00:00Z"),
            "bad archive digest": lambda: setattr(self, "bad_archive", True),
            "wrong candidate": lambda: self.payload["candidate"].update(source_sha=NEWER_SHA),
        }
        if "failure" not in self.source.get("accepted_conclusions", ["success"]):
            changes["failed run"] = lambda: self.run.update(conclusion="failure")
        for label, change in changes.items():
            with self.subTest(label=label):
                self.setUp()
                # _fragment uses the shared candidate fixture; avoid mutating it.
                self.payload["candidate"] = copy.deepcopy(CANDIDATE)
                change()
                with self.assertRaises(ValueError):
                    self.drive()

    def test_receipt_run_identity_adversaries_refused_even_with_valid_digest(self):
        for key, value in [("repository", "other/fork"), ("workflow", "other.yml"),
                           ("run_id", 78), ("run_attempt", 2), ("run_attempt", True),
                           ("dispatch_id", "another-dispatch"), ("dispatch_id", ""),
                           ("extra", "not-in-contract")]:
            with self.subTest(key=key, value=value):
                self.setUp()
                self.producer[key] = value
                with self.assertRaises(ValueError):
                    self.drive()

    def test_unbound_digest_and_missing_receipts_refused(self):
        self.payload["observations"][0]["evidence_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "evidence digest"):
            MODULE.validate_candidate_tag_fragment(self.payload, self.run, self.source, CANDIDATE)
        self.payload["observations"][0].update(result="skip", evidence_receipt=None)
        with self.assertRaisesRegex(ValueError, "no executed receipt"):
            self.drive()

    def test_tag_lookup_does_not_treat_permission_or_network_errors_as_absent_branch(self):
        for code in (401, 403, 500):
            with self.subTest(code=code):
                with mock.patch.object(MODULE, "request_bytes", side_effect=urllib.error.HTTPError(
                    "https://api.github.com", code, "Refused", {}, None
                )):
                    with self.assertRaises(urllib.error.HTTPError):
                        MODULE.verified_candidate_tag(self.run, self.source, PINNED_SHA, "token")

    def test_registry_rejects_invalid_opt_in(self):
        for title in (True, "", "SDK [unbounded]", "x" * 81):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                self.source["candidate_tag_dispatch_title"] = title
                registry = Path(directory) / "registry.json"
                registry.write_text(json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [self.source]}))
                with self.assertRaisesRegex(ValueError, "candidate tag dispatch"):
                    MODULE.load_registry(registry)


class ConfiguredServerCandidateTagTests(CandidateTagProducerTests):
    # Run the existing API/ref/hash/receipt adversaries against each actual
    # configured server producer, not a more permissive synthetic registry.
    producer_name = "honua-server-cite"

    def setUp(self):
        super().setUp()
        registry = MODULE.load_registry(Path(__file__).resolve().parents[1] /
                                        "config/protocol-certification-producers.v1.json")
        self.source = next(row for row in registry["sources"]
                           if row["producer"] == self.producer_name)
        self.source = copy.deepcopy(self.source)
        # CITE is optional in broad discovery, but this focused fetch requires
        # its matching fragment so refusal cannot silently pass with zero rows.
        self.source["required"] = True
        repository = self.source["repository"]
        self.run.update(repository={"full_name": repository},
                        head_repository={"full_name": repository},
                        path=self.source["workflow_path"],
                        display_title=f"{self.source['candidate_tag_dispatch_title']} [nightly-certification-55-1]")
        self.artifact["name"] = self.source["artifact_prefix"] + "77-1"
        self.payload["producer"] = self.producer_name
        self.producer.update(repository=repository,
                             workflow=self.source["workflow_path"].rsplit("/", 1)[-1])

    def test_exact_configured_run_title_is_required(self):
        self.run["display_title"] = "SDK Integration [nightly-certification-55-1]"
        with self.assertRaisesRegex(ValueError, "run title"):
            self.drive()


class ConfiguredHarnessCandidateTagTests(ConfiguredServerCandidateTagTests):
    producer_name = "server-protocol-harness"

    def test_completed_harness_failure_remains_evidentiary(self):
        self.run["conclusion"] = "failure"
        observation = self.payload["observations"][0]
        observation["result"] = "fail"
        observation["evidence_receipt"]["result"] = "fail"
        rows = self.drive()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["candidate_tag_verified"])
        self.assertEqual(observation["evidence_receipt"]["result"], "fail")
