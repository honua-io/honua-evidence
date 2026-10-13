"""Synthetic outputs of actual server producers; never live certification proof."""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

from test_aggregate_certification import module as aggregate, rebind_receipt, requirement
from test_fetch_certification_producers import MODULE as fetch, _archive

FIXTURES = Path(__file__).parent / "fixtures/server-producer-v2"
REGISTRY = Path(__file__).resolve().parents[1] / "config/protocol-certification-producers.v1.json"


class ServerProducerV2IntakeTests(unittest.TestCase):
    def inputs(self, name):
        context = json.loads((FIXTURES / name / "context.json").read_text())
        fragment = json.loads((FIXTURES / name / "fragment.json").read_text())
        requirements = []
        for row in context["requirements"]:
            req = requirement(client=row["canonical_client"])
            req.update(row)
            requirements.append(req)
        source = next(row for row in fetch.load_registry(REGISTRY)["sources"]
                      if row["producer"] == fragment["producer"])
        return context, fragment, requirements, source

    def fetch_then_aggregate(self, name, mutate=None):
        context, fragment, requirements, source = self.inputs(name)
        if mutate:
            mutate(fragment, requirements)
        pin = context["producer_source_sha"]
        identity = fragment["observations"][0]["evidence_receipt"]["identity"]["producer_run"]
        tag = "nightly-candidate/" + pin
        run = {"id": identity["run_id"], "run_attempt": identity["run_attempt"],
               "head_sha": pin, "head_branch": tag, "event": "workflow_dispatch",
               "repository": {"full_name": source["repository"]},
               "head_repository": {"full_name": source["repository"]},
               "path": source["workflow_path"], "status": "completed", "conclusion": "success",
               "run_started_at": fragment["observations"][0]["started_at"],
               "display_title": f"{source['candidate_tag_dispatch_title']} [{identity['dispatch_id']}]"}
        archive = _archive({"protocol-certification-fragment.json": fragment})
        artifact = {"id": 88, "name": source["artifact_prefix"] + f"{run['id']}-{run['run_attempt']}",
                    "expired": False, "created_at": fragment["generated_at"],
                    "archive_download_url": "https://api.github.com/synthetic/archive",
                    "workflow_run": {"id": run["id"], "head_sha": pin},
                    "digest": "sha256:" + hashlib.sha256(archive).hexdigest()}

        def request(url, token, accept):
            if "/git/ref/tags/" in url:
                return json.dumps({"ref": "refs/tags/" + tag,
                                   "object": {"type": "commit", "sha": pin}}).encode()
            if "/git/ref/heads/" in url:
                raise urllib.error.HTTPError(url, 404, "Absent synthetic branch", {}, None)
            self.assertEqual(url, artifact["archive_download_url"])
            return archive

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "registry.json"
            registry.write_text(json.dumps({"schema": fetch.REGISTRY_SCHEMA, "sources": [source]}))
            with mock.patch.object(fetch, "list_runs_at_revision", return_value=[run]), \
                 mock.patch.object(fetch, "list_run_artifacts", return_value=[artifact]), \
                 mock.patch.object(fetch, "request_bytes", side_effect=request):
                fetch.fetch(registry, root / "fetched", "synthetic-token",
                            {source["source_revision_key"]: pin}, candidate=context["candidate"])
            manifest = [json.loads(line) for line in (root / "fetched/.fetch-manifest.ndjson").read_text().splitlines()]
            self.assertEqual(len(manifest), 1)
            path = Path(manifest[0]["destination"])
            fetched = json.loads(path.read_text())
            self.assertEqual(fetched, fragment)
            ledger = aggregate.build_ledger(
                context["requirements_revision"], "d" * 40, True, requirements,
                [(path, fetched)], context["candidate"],
                now=datetime.fromisoformat(fragment["generated_at"].replace("Z", "+00:00")),
            )
            self.assertEqual(len(ledger["cells"]), len(fragment["observations"]))
            for cell in ledger["cells"]:
                # Compare by receipt identity because the ledger may sort cells.
                matching = next(row for row in fragment["observations"]
                                if row["evidence_digest"] == cell["evidence_digest"])
                self.assertEqual(aggregate._receipt_bytes(cell["evidence_receipt"]),
                                 aggregate._receipt_bytes(matching["evidence_receipt"]))
                self.assertEqual(cell["result"], matching["result"])
            return ledger

    def test_actual_cite_generator_output_survives_fetch_and_ledger(self):
        self.assertEqual(len(self.fetch_then_aggregate("cite")["cells"]), 9)

    def test_actual_harness_generator_output_survives_fetch_and_ledger(self):
        self.assertEqual(len(self.fetch_then_aggregate("harness")["cells"]), 42)

    def test_server_receipts_refuse_wrong_lane_and_workflow_with_rebound_digest(self):
        for name in ("cite", "harness"):
            context, fragment, requirements, _ = self.inputs(name)
            for key, value in (("repository", "honua-io/honua-sdk-js"), ("workflow", "integration.yml"),
                               ("run_attempt", True), ("run_id", "123"), ("unexpected", 1)):
                with self.subTest(name=name, key=key):
                    observed = copy.deepcopy(fragment["observations"][0])
                    observed["evidence_receipt"]["identity"]["producer_run"][key] = value
                    rebind_receipt(observed)
                    self.assertFalse(aggregate._valid_receipt(
                        observed, requirements[0], context["candidate"]["cut_at"], context["requirements_revision"]))
            wrong_lane = {**requirements[0], "client_lane": "unreviewed-lane"}
            self.assertFalse(aggregate._valid_receipt(fragment["observations"][0], wrong_lane,
                                                    context["candidate"]["cut_at"], context["requirements_revision"]))

    def test_harness_null_url_requires_bound_execution_and_governed_methods(self):
        changes = {
            "foreign profile": lambda o, r: o.update(protocol_profile="unreviewed"),
            "candidate image": lambda o, r: o.update(image_digest="sha256:" + "e" * 64),
            "foreign producer source": lambda o, r: o.update(producer_source_sha="e" * 40),
            "unbound exercised facets": lambda o, r: o.update(exercised_capabilities=[]),
            "missing test IDs": lambda o, r: r.pop("test_ids"),
            "TLS": lambda o, r: r["scenario_facets"].append("tls"),
        }
        for label, change in changes.items():
            def mutate(fragment, requirements):
                change(fragment["observations"][0], requirements[0])
                rebind_receipt(fragment["observations"][0])
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.fetch_then_aggregate("harness", mutate)

    def test_cite_executed_null_url_is_still_refused(self):
        def mutate(fragment, requirements):
            fragment["observations"][0]["request_url"] = None
        with self.assertRaisesRegex(ValueError, "request_url"):
            self.fetch_then_aggregate("cite", mutate)

    def test_harness_payload_binding_refuses_rehashed_execution_and_outcome_changes(self):
        mutations = {
            "missing execution": lambda p: p.pop("execution"),
            "changed execution": lambda p: p["execution"].update(protocol_profile="other"),
            "extra execution field": lambda p: p["execution"].update(extra=True),
            "wrong outcome": lambda p: p["test_outcomes"].update(
                {next(iter(p["test_outcomes"])): "fail"}),
            "missing outcome": lambda p: p["test_outcomes"].clear(),
            "malformed outcome": lambda p: p["test_outcomes"].update(
                {next(iter(p["test_outcomes"])): []}),
        }
        for label, change in mutations.items():
            def mutate(fragment, requirements):
                observed = fragment["observations"][0]
                receipt = observed["evidence_receipt"]
                payload = json.loads(base64.b64decode(receipt["payload_base64"]))
                change(payload)
                receipt["payload_base64"] = base64.b64encode(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).decode()
                rebind_receipt(observed)
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "request_url"):
                self.fetch_then_aggregate("harness", mutate)
