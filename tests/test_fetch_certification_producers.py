from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "fetch-certification-producers.py"
REGISTRY = Path(__file__).parents[1] / "config" / "protocol-certification-producers.v1.json"
FIXTURES = Path(__file__).parent / "fixtures" / "protocol-certification-producers"
SPEC = importlib.util.spec_from_file_location("fetch_certification_producers", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _archive(files: dict[str, dict]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        for name, payload in files.items():
            bundle.writestr(name, json.dumps(payload))
    return stream.getvalue()


def test_select_artifacts_filters_expired_and_keeps_newest() -> None:
    artifacts = [
        {"name":"cert-1","expired":False,"created_at":"2026-01-01","archive_download_url":"a"},
        {"name":"cert-2","expired":True,"created_at":"2026-01-03","archive_download_url":"b"},
        {"name":"other","expired":False,"created_at":"2026-01-04","archive_download_url":"c"},
        {"name":"cert-3","expired":False,"created_at":"2026-01-02","archive_download_url":"d"},
    ]
    assert [row["name"] for row in MODULE.select_artifacts(artifacts, "cert-", 1)] == ["cert-3"]


def test_select_artifacts_applies_full_name_regex() -> None:
    artifacts = [
        {"name":"python-sdk-conformance-server-7-1","expired":False,"created_at":"2026-01-03","archive_download_url":"a"},
        {"name":"python-sdk-conformance-7-1","expired":False,"created_at":"2026-01-02","archive_download_url":"b"},
    ]
    selected = MODULE.select_artifacts(
        artifacts,
        "python-sdk-conformance-",
        10,
        r"^python-sdk-conformance-[0-9]+-[0-9]+$",
    )
    assert [row["name"] for row in selected] == ["python-sdk-conformance-7-1"]


PINNED_SHA = "3a80040bff4060d5a816488ea65fd3fe928dd964"
NEWER_SHA = "bfe12dd91819cb770657911be66928b0800b55c3"
CANDIDATE = {
    "source_sha": "e3ab87cebb7bf2d32c4e8cdb145f8d626b864d8e",
    "image_digest": "sha256:d7a45c871bf318b4882ec8e1c32004803e6d0210246be30120751f05dee1a14d",
    "cut_at": "2026-08-21T15:13:36Z",
}


def _requirements(path: Path, pins: dict[str, str]) -> Path:
    path.write_text(
        json.dumps({
            "schema": MODULE.REQUIREMENTS_SCHEMA,
            "source_revisions": {name: {"commit": sha} for name, sha in pins.items()},
        }),
        encoding="utf-8",
    )
    return path


def _sdk_js_source(**overrides) -> dict:
    source = {
        "producer": "honua-sdk-js",
        "repository": "honua-io/honua-sdk-js",
        "workflow_path": ".github/workflows/integration.yml",
        "artifact_prefix": "integration-meta",
        "fragment_globs": ["**/protocol-certification-fragment.json"],
        "trusted_branches": ["trunk"],
        "trusted_events": ["push", "schedule"],
        "source_revision_key": "sdk-js",
        "max_artifacts": 10,
        "required": True,
    }
    source.update(overrides)
    return source


def _run(run_id: int, head_sha: str) -> dict:
    return {
        "id": run_id, "status": "completed", "conclusion": "success",
        "event": "schedule", "head_branch": "trunk", "head_sha": head_sha,
        "run_attempt": 1, "run_started_at": "2026-08-22T07:30:00Z",
        "path": ".github/workflows/integration.yml",
        "head_repository": {"full_name": "honua-io/honua-sdk-js"},
    }


def _artifact(artifact_id: int, run_id: int, head_sha: str) -> dict:
    return {
        "id": artifact_id, "name": "integration-meta", "expired": False,
        "created_at": "2026-08-22T07:36:32Z",
        "archive_download_url": f"archive-{artifact_id}",
        "workflow_run": {"id": run_id, "head_sha": head_sha},
    }


def _fragment(head_sha: str) -> dict:
    return {
        "schema": MODULE.FRAGMENT_SCHEMA,
        "producer": "honua-sdk-js",
        "candidate": CANDIDATE,
        "observations": [{
            "producer_source_sha": head_sha,
            "contract_revision": f"sdk-js-certification@{head_sha}",
            "auth_policy_revision": "auth-v1",
            "fixture_revision": "fixture-v1",
            "surface": "ogc-features",
            "operation": "collections",
            "canonical_client": "honua-sdk-js",
            "client_version": "1.0.0",
            "deployment_target": "synthetic",
            "client_id": "honua-sdk-js",
            "runner_lane": "sdk-js",
            "protocol_version": "1.0",
            "protocol_profile": "core",
            "performed_by": "honua-sdk-js",
            "request_url": "https://candidate.test/collections",
            "exercised_capabilities": ["positive"],
            "evidence_uri": "https://evidence.honua.io/data/sha256/" + "a" * 64,
        }],
    }


def _fetch(source: dict, runs_by_sha: dict[str, list[int]], artifacts: dict[int, list[dict]]):
    """Drive fetch() against a fake GitHub, honouring the head_sha run filter."""
    def request(url, _token, _accept):
        if "/actions/runs?head_sha=" in url:
            sha = url.split("head_sha=")[1].split("&")[0]
            return json.dumps({
                "workflow_runs": [_run(rid, sha) for rid in runs_by_sha.get(sha, [])]
            }).encode()
        if "/artifacts?per_page=100" in url and "/actions/runs/" in url:
            run_id = int(url.split("/actions/runs/")[1].split("/")[0])
            return json.dumps({"artifacts": artifacts.get(run_id, [])}).encode()
        if url.startswith("archive-"):
            return _archive({"protocol-certification-fragment.json": _fragment(
                NEWER_SHA if url == "archive-99" else PINNED_SHA
            )})
        raise AssertionError(f"unexpected request: {url}")

    directory = tempfile.TemporaryDirectory()
    root = Path(directory.name)
    registry = root / "registry.json"
    registry.write_text(
        json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [source]}), encoding="utf-8"
    )
    output = root / "out"
    with mock.patch.object(MODULE, "request_bytes", side_effect=request):
        MODULE.fetch(registry, output, "token", {"sdk-js": PINNED_SHA})
    manifest = [
        json.loads(line)
        for line in (output / ".fetch-manifest.ndjson").read_text().splitlines()
    ]
    gaps = [
        json.loads(line)
        for line in (output / ".fetch-gaps.ndjson").read_text().splitlines()
    ]
    directory.cleanup()
    return manifest, gaps


def test_fetch_selects_the_run_at_the_pinned_revision() -> None:
    # Fixture mirrors the real sdk-js evidence: run 32560044494 / artifact
    # 9472525605, whose observations carry sdk-js-certification@3a80040b... and
    # the exact frozen candidate triple.
    manifest, gaps = _fetch(
        _sdk_js_source(),
        {PINNED_SHA: [32560044494], NEWER_SHA: [99999999999]},
        {32560044494: [_artifact(9472525605, 32560044494, PINNED_SHA)]},
    )
    assert gaps == []
    assert [(row["artifact_id"], row["head_sha"]) for row in manifest] == [
        (9472525605, PINNED_SHA)
    ]
    assert manifest[0]["pinned_source_sha"] == PINNED_SHA
    assert manifest[0]["source_revision_key"] == "sdk-js"


def test_fetch_never_falls_back_to_a_newer_run_when_the_pin_has_none() -> None:
    # THE point of the change: a newer, greener run must not be substituted for
    # missing evidence at the pin. Required producers fail closed...
    try:
        _fetch(
            _sdk_js_source(),
            {PINNED_SHA: [], NEWER_SHA: [99]},
            {99: [_artifact(99, 99, NEWER_SHA)]},
        )
    except ValueError as error:
        assert "no normalized fragments at pinned" in str(error)
        assert PINNED_SHA in str(error)
    else:
        raise AssertionError("required producer with no pinned evidence must fail closed")

    # ...and optional producers record an honest gap rather than newer evidence.
    manifest, gaps = _fetch(
        _sdk_js_source(required=False),
        {PINNED_SHA: [], NEWER_SHA: [99]},
        {99: [_artifact(99, 99, NEWER_SHA)]},
    )
    assert manifest == []
    assert len(gaps) == 1
    assert gaps[0]["pinned_source_sha"] == PINNED_SHA
    assert gaps[0]["producer"] == "honua-sdk-js"


def test_fetch_ignores_artifacts_whose_run_head_is_not_the_pin() -> None:
    # Defence in depth: even if the runs endpoint were to return an off-pin run,
    # its artifacts must not be harvested.
    manifest, gaps = _fetch(
        _sdk_js_source(required=False),
        {PINNED_SHA: [77]},
        {77: [_artifact(99, 77, NEWER_SHA)]},
    )
    assert manifest == []
    assert len(gaps) == 1


def test_fetch_counts_only_artifacts_with_fragments() -> None:
    head_sha = "a" * 40
    source = {
        "producer": "honua-sdk-python",
        "repository": "honua-io/honua-sdk-python",
        "workflow_path": ".github/workflows/conformance.yml",
        "artifact_name_regex": r"^python-sdk-conformance-[0-9]+-[0-9]+$",
        "artifact_prefix": "python-sdk-conformance-",
        "fragment_globs": ["**/protocol-certification-fragment.json"],
        "trusted_branches": ["trunk"],
        "trusted_events": ["workflow_dispatch"],
        "source_revision_key": "sdk-python",
        "max_artifacts": 1,
        "required": True,
    }
    run = {
        "id": 8, "status": "completed", "conclusion": "success",
        "event": "workflow_dispatch", "head_branch": "trunk", "head_sha": head_sha,
        "run_attempt": 1, "run_started_at": "2026-08-20T10:00:00Z",
        "path": ".github/workflows/conformance.yml",
        "head_repository": {"full_name": "honua-io/honua-sdk-python"},
    }
    artifacts = [
        {
            "id": 8, "name": "python-sdk-conformance-8-1", "expired": False,
            "created_at": "2026-08-20T10:03:00Z", "archive_download_url": "archive-8",
            "workflow_run": {"id": 8, "head_sha": head_sha},
        },
        {
            "id": 7, "name": "python-sdk-conformance-7-1", "expired": False,
            "created_at": "2026-08-20T10:02:00Z", "archive_download_url": "archive-7",
            "workflow_run": {"id": 8, "head_sha": head_sha},
        },
    ]

    def request(url, _token, _accept):
        if "/actions/runs?head_sha=" in url:
            return json.dumps({"workflow_runs": [run]}).encode()
        if "/artifacts?per_page=100" in url:
            return json.dumps({"artifacts": artifacts}).encode()
        if url == "archive-8":
            return _archive({"server-log.json": {"schema": "diagnostic"}})
        if url == "archive-7":
            return _archive({
                "protocol-certification-fragment.json": {
                    "schema": MODULE.FRAGMENT_SCHEMA,
                    "producer": "honua-sdk-python",
                    "observations": [{
                        "producer_source_sha": head_sha,
                        "contract_revision": "python-v1",
                        "auth_policy_revision": "auth-v1",
                        "fixture_revision": "fixture-v1",
                        "surface": "ogc-features",
                        "operation": "collections",
                        "canonical_client": "honua-sdk-python",
                        "client_version": "1.0.0",
                        "deployment_target": "synthetic",
                        "client_id": "honua-sdk-python", "runner_lane": "sdk-python",
                        "protocol_version": "1.0", "protocol_profile": "core",
                        "performed_by": "honua-sdk-python",
                        "request_url": "https://candidate.test/collections",
                        "exercised_capabilities": ["positive"],
                        "evidence_uri": "https://github.com/honua-io/honua-sdk-python/actions/runs/8",
                    }],
                }
            })
        raise AssertionError(f"unexpected request: {url}")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = root / "registry.json"
        output = root / "out"
        registry.write_text(
            json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [source]}),
            encoding="utf-8",
        )
        with mock.patch.object(MODULE, "request_bytes", side_effect=request):
            MODULE.fetch(registry, output, "token", {"sdk-python": head_sha})
        manifest = [json.loads(line) for line in (output / ".fetch-manifest.ndjson").read_text().splitlines()]
        assert [(row["artifact_id"], row["producer"]) for row in manifest] == [(7, "honua-sdk-python")]


def test_pins_must_come_from_a_governed_denominator() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        good = _requirements(root / "req.json", {"sdk-js": PINNED_SHA})
        assert MODULE.load_pinned_revisions(good) == {"sdk-js": PINNED_SHA}

        for payload, message in [
            ({"source_revisions": {"sdk-js": {"commit": PINNED_SHA}}}, "governed"),
            ({"schema": MODULE.REQUIREMENTS_SCHEMA}, "source_revisions"),
            (
                {"schema": MODULE.REQUIREMENTS_SCHEMA,
                 "source_revisions": {"sdk-js": {"commit": "trunk"}}},
                "40-character",
            ),
        ]:
            path = root / "bad.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            try:
                MODULE.load_pinned_revisions(path)
            except ValueError as error:
                assert message in str(error)
            else:
                raise AssertionError(f"expected rejection containing {message!r}")


def test_extract_fragments_accepts_only_normalized_envelopes() -> None:
    archive = _archive({
        "nested/protocol-certification-fragment.json": {"schema":MODULE.FRAGMENT_SCHEMA,"producer":{"repository":"honua-io/test"}},
        "nested/unrelated.json": {"schema":"other"},
    })
    assert MODULE.extract_fragments(archive, ["**/protocol-certification-fragment.json"]) == [(
        "nested/protocol-certification-fragment.json",
        {"schema":MODULE.FRAGMENT_SCHEMA,"producer":{"repository":"honua-io/test"}},
    )]


def test_fragment_destination_names_resist_normalization_collisions() -> None:
    first = MODULE.fragment_destination_name("a/b/protocol-certification-fragment.json")
    second = MODULE.fragment_destination_name("a-b/protocol-certification-fragment.json")
    assert first != second


def test_artifact_redirect_does_not_forward_github_credentials() -> None:
    request = urllib.request.Request(
        "https://api.github.com/repos/honua-io/test/actions/artifacts/1/zip",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer secret",
            "User-Agent": "honua-test",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    redirected = MODULE.CredentialStrippingRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://artifactcache.actions.githubusercontent.com/results/archive.zip?sig=value",
    )

    assert redirected is not None
    assert not redirected.has_header("Authorization")
    assert not redirected.has_header("X-GitHub-Api-Version")
    assert not redirected.has_header("Accept")
    assert redirected.get_header("User-agent") == "honua-test"


def test_every_registered_producer_declares_a_denominator_pin() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))

    # A producer without a pin would silently harvest whatever ran most
    # recently -- the defect this registry field exists to make impossible.
    for source in registry["sources"]:
        assert source["source_revision_key"], source["producer"]
        assert "max_artifact_pages" not in source, source["producer"]

    assert all(source.get("implementation_issue") for source in registry["sources"])
    assert not any(
        source["producer"].startswith(MODULE.RESERVED_CLIENT_LANE_PREFIX)
        for source in registry["sources"]
    )
    assert {
        source["producer"]: source["source_revision_key"]
        for source in registry["sources"]
    } == {
        "honua-server-cng": "server",
        "honua-sdk-js": "sdk-js",
        "honua-sdk-python": "sdk-python",
        "honua-sdk-dotnet": "sdk-dotnet",
        "server-protocol-harness": "server-certification",
        "honua-server-client-interop": "server-certification",
        "honua-server-cite": "server-certification",
        "honua-esri-compat": "esri-compat",
        "geospatial-grpc": "geospatial-grpc",
        "geospatial-mcp": "geospatial-mcp",
    }


def test_synthetic_ledger_fragment_and_negative_fixtures_fail_closed() -> None:
    trusted = json.loads((FIXTURES / "trusted-fragment.json").read_text())
    MODULE.validate_fragment_producer(
        trusted, "geospatial-grpc", "a" * 40, "honua-io/geospatial-grpc"
    )
    for filename, message in [
        ("rejected-unassigned-lane.json", "reserved lane"),
        ("rejected-evidence-uri.json", "untrusted evidence_uri"),
    ]:
        payload = json.loads((FIXTURES / filename).read_text())
        with unittest.TestCase().assertRaisesRegex(ValueError, message):
            MODULE.validate_fragment_producer(
                payload, "geospatial-grpc", "a" * 40, "honua-io/geospatial-grpc"
            )


def test_registry_rejects_reserved_unassigned_producer() -> None:
    source = _sdk_js_source(
        producer="canonical-client-unassigned-ogc",
        implementation_issue="https://github.com/honua-io/honua-sdk-js/issues/39",
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "registry.json"
        path.write_text(json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [source]}))
        with unittest.TestCase().assertRaisesRegex(ValueError, "Reserved unassigned"):
            MODULE.load_registry(path)


def test_github_listings_follow_every_page() -> None:
    run = _run(1, PINNED_SHA)
    artifact = _artifact(1, 1, PINNED_SHA)

    def request(url, _token, _accept):
        page = int(url.rsplit("page=", 1)[1])
        if "/artifacts?" in url:
            return json.dumps({"artifacts": [artifact] * (100 if page == 1 else 1)}).encode()
        return json.dumps({"workflow_runs": [run] * (100 if page == 1 else 1)}).encode()

    with mock.patch.object(MODULE, "request_bytes", side_effect=request):
        assert len(MODULE.list_runs_at_revision(
            "honua-io/honua-sdk-js", ".github/workflows/integration.yml", PINNED_SHA, "token"
        )) == 101
        assert len(MODULE.list_run_artifacts("honua-io/honua-sdk-js", 1, "token")) == 101


def test_trusted_run_requires_successful_configured_workflow_and_identity() -> None:
    source = {
        "repository": "honua-io/test",
        "workflow_path": ".github/workflows/certify.yml",
        "trusted_branches": ["trunk"],
        "trusted_events": ["schedule"],
    }
    run = {
        "id": 7,
        "status": "completed",
        "conclusion": "success",
        "event": "schedule",
        "head_branch": "trunk",
        "head_sha": "a" * 40,
        "run_attempt": 2,
        "run_started_at": "2026-08-20T10:00:00Z",
        "path": ".github/workflows/certify.yml",
        "head_repository": {"full_name": "honua-io/test"},
    }
    artifact = {
        "created_at": "2026-08-20T10:01:00Z",
        "workflow_run": {"id": 7, "head_sha": "a" * 40},
    }
    assert MODULE.trusted_run(run, artifact, source)
    assert not MODULE.trusted_run({**run, "conclusion": "failure"}, artifact, source)
    source["accepted_conclusions"] = ["success", "failure"]
    assert MODULE.trusted_run({**run, "conclusion": "failure"}, artifact, source)
    assert not MODULE.trusted_run({**run, "conclusion": "cancelled"}, artifact, source)
    assert not MODULE.trusted_run({**run, "head_branch": "feature"}, artifact, source)
    assert not MODULE.trusted_run(
        run,
        {**artifact, "created_at": "2026-08-20T09:59:59Z"},
        source,
    )
    assert not MODULE.trusted_run(
        run,
        {**artifact, "created_at": run["run_started_at"]},
        source,
    )


def _interop_requirement() -> dict:
    return {
        "capability_key": "serve.ogc-api-features", "surface": "ogc-features",
        "operation": "collections", "maturity": "supported", "canonical_client": "GeoPandas",
        "client_lane": "py-geopandas", "client_version": "1.0.1", "deployment_target": "local-docker",
        "required_tier": "nightly", "licensed": False, "entitlement_policy_revision": None,
        "addressable_by_client": True, "addressability_reason": None,
        "scenario_facets": ["positive"], "contract_revision": "config-v1",
        "auth_policy_revision": "auth-v1", "fixture_revision": "fixture-v1",
        "budget_expectations": None, "test_ids": ["CERT-DISC-01"],
    }


def _interop_raw(**overrides) -> dict:
    raw = {
        "schema_version": "1.0", "run_id": "42", "run_date": "2026-08-22T10:00:00Z",
        "server_commit": PINNED_SHA, "producer_source_sha": PINNED_SHA,
        "image_digest": CANDIDATE["image_digest"],
        "fixture_revision": "fixture-v1", "server_config_revision": "config-v1",
        "auth_policy_revision": "auth-v1", "client_id": "GeoPandas", "runner_lane": "py-geopandas",
        "client_version": "1.0.1", "protocol": "ogc-features", "protocol_version": "1.0",
        "protocol_profile": "core",
        "environment": "local-docker", "results": [{
            "test_case_id": "CERT-DISC-01", "status": "pass", "duration_ms": 10,
            "notes": "GeoPandas discovered the governed collection.",
            "performed_by": "GeoPandas", "request_url": "https://candidate.test/collections",
            "exercised_capabilities": ["positive"],
        }],
    }
    raw.update(overrides)
    return raw


def test_client_interop_receipt_normalizes_to_exact_candidate_fragment() -> None:
    candidate = {**CANDIDATE, "source_sha": PINNED_SHA}
    fragment = MODULE.normalize_client_interop(
        _interop_raw(), [_interop_requirement()], candidate, PINNED_SHA
    )
    assert fragment["producer"] == "honua-server-client-interop"
    assert fragment["candidate"] == candidate
    observation = fragment["observations"][0]
    assert observation["result"] == "pass"
    assert observation["source_sha"] == PINNED_SHA
    assert observation["image_digest"] == candidate["image_digest"]
    assert observation["fixture_revision"] == "fixture-v1"
    assert observation["contract_revision"] == "config-v1"
    assert observation["auth_policy_revision"] == "auth-v1"
    assert observation["evidence_receipt"]["identity"]["candidate_cut_at"] == candidate["cut_at"]


def test_client_interop_normalizer_fails_closed() -> None:
    candidate = {**CANDIDATE, "source_sha": PINNED_SHA}
    for raw, message in [
        (_interop_raw(server_commit=NEWER_SHA), "candidate SHA"),
        (_interop_raw(producer_source_sha=NEWER_SHA), "trusted run SHA"),
        (_interop_raw(results=[{"test_case_id": "unknown", "status": "pass"}]), "0 governed"),
        ({"schema_version": "1.0"}, "missing fields"),
    ]:
        with unittest.TestCase().assertRaisesRegex(ValueError, message):
            MODULE.normalize_client_interop(raw, [_interop_requirement()], candidate, PINNED_SHA)


def test_client_identity_rejects_generic_js_lane_collision() -> None:
    requirement = {**_interop_requirement(), "canonical_client": "OpenLayers", "client_lane": "js"}
    raw = _interop_raw(client_id="js", runner_lane="js")
    with unittest.TestCase().assertRaisesRegex(ValueError, "collides"):
        MODULE.normalize_client_interop(raw, [requirement], {**CANDIDATE, "source_sha": PINNED_SHA}, PINNED_SHA)


def test_client_identity_rejects_missing_protocol_profile() -> None:
    raw = _interop_raw()
    raw.pop("protocol_profile")
    with unittest.TestCase().assertRaisesRegex(ValueError, "protocol_profile"):
        MODULE.normalize_client_interop(raw, [_interop_requirement()], {**CANDIDATE, "source_sha": PINNED_SHA}, PINNED_SHA)


def test_client_identity_rejects_unresolvable_candidate_revision() -> None:
    with unittest.TestCase().assertRaisesRegex(ValueError, "exact resolvable revision"):
        MODULE.normalize_client_interop(_interop_raw(server_commit="unknown"), [_interop_requirement()], {**CANDIDATE, "source_sha": PINNED_SHA}, PINNED_SHA)


def test_client_identity_rejects_geopandas_result_performed_by_httpx() -> None:
    result = {**_interop_raw()["results"][0], "performed_by": "httpx"}
    with unittest.TestCase().assertRaisesRegex(ValueError, "generic HTTP probe"):
        MODULE.normalize_client_interop(_interop_raw(results=[result]), [_interop_requirement()], {**CANDIDATE, "source_sha": PINNED_SHA}, PINNED_SHA)


def test_client_identity_rejects_plain_http_tls_claim() -> None:
    requirement = {**_interop_requirement(), "scenario_facets": ["tls"]}
    result = {
        **_interop_raw()["results"][0],
        "request_url": "http://candidate.test/collections",
        "exercised_capabilities": ["tls"],
    }
    with unittest.TestCase().assertRaisesRegex(ValueError, "claims TLS"):
        MODULE.normalize_client_interop(_interop_raw(results=[result]), [requirement], {**CANDIDATE, "source_sha": PINNED_SHA}, PINNED_SHA)


def test_client_interop_run_rejects_non_trunk_wrong_event_and_conclusion() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    source = next(
        item for item in registry["sources"]
        if item["producer"] == "honua-server-client-interop"
    )
    run = {
        "id": 42, "status": "completed", "conclusion": "success", "event": "schedule",
        "head_branch": "trunk", "head_sha": PINNED_SHA, "run_attempt": 1,
        "run_started_at": "2026-08-22T09:00:00Z", "path": source["workflow_path"],
        "head_repository": {"full_name": source["repository"]},
    }
    artifact = {
        "created_at": "2026-08-22T10:00:00Z",
        "workflow_run": {"id": 42, "head_sha": PINNED_SHA},
    }
    assert MODULE.trusted_run(run, artifact, source)
    assert not MODULE.trusted_run({**run, "head_branch": "feature"}, artifact, source)
    assert not MODULE.trusted_run({**run, "event": "pull_request"}, artifact, source)
    assert not MODULE.trusted_run({**run, "conclusion": "failure"}, artifact, source)


def test_empty_exercised_capabilities_is_a_value_not_an_omission() -> None:
    fragment = MODULE.normalize_client_interop(
        _interop_raw(results=[{
            "test_case_id": "CERT-DISC-01", "status": "skip", "notes": "lane skipped",
            "performed_by": "GeoPandas", "request_url": "https://candidate.test/collections",
            "exercised_capabilities": [],
        }]), [_interop_requirement()], {**CANDIDATE, "source_sha": PINNED_SHA}, PINNED_SHA
    )
    observation = fragment["observations"][0]
    observation.setdefault("client_lane", observation["runner_lane"])
    # validate_fragment_producer's observation checks must accept [] for a skip
    MODULE.validate_fragment_producer(
        {**fragment, "producer": "honua-server-client-interop"},
        "honua-server-client-interop", PINNED_SHA,
    )


def test_client_interop_unregistered_producer_is_rejected() -> None:
    candidate = {**CANDIDATE, "source_sha": PINNED_SHA}
    fragment = MODULE.normalize_client_interop(
        _interop_raw(), [_interop_requirement()], candidate, PINNED_SHA
    )
    with unittest.TestCase().assertRaisesRegex(ValueError, "does not match registry producer"):
        MODULE.validate_fragment_producer(fragment, "unregistered-client-interop")


def test_fragment_producer_must_match_registry() -> None:
    try:
        MODULE.validate_fragment_producer({"producer": "impersonator"}, "honua-server-cng")
    except ValueError as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("producer mismatch was accepted")


def test_fragment_observations_must_match_trusted_run_head() -> None:
    head_sha = "a" * 40
    fragment = {
        "producer": "honua-server-cng",
        "observations": [{"producer_source_sha": "b" * 40}],
    }
    try:
        MODULE.validate_fragment_producer(fragment, "honua-server-cng", head_sha)
    except ValueError as error:
        assert "trusted run head" in str(error)
    else:
        raise AssertionError("producer SHA mismatch was accepted")

    fragment["observations"][0]["producer_source_sha"] = head_sha
    fragment["observations"][0].update({
        "contract_revision": "cng-v1", "auth_policy_revision": "auth-v1",
        "fixture_revision": "fixture-v1", "surface": "cog", "operation": "read",
        "canonical_client": "gdal", "client_version": "3.11",
        "deployment_target": "synthetic",
        "client_id": "gdal", "runner_lane": "cng",
        "protocol_version": "1.0", "protocol_profile": "core",
        "performed_by": "gdal", "request_url": "https://candidate.test/read",
        "exercised_capabilities": ["positive"],
        "evidence_uri": "https://evidence.honua.io/data/sha256/" + "a" * 64,
    })
    MODULE.validate_fragment_producer(fragment, "honua-server-cng", head_sha)


def test_registry_validation_rejects_duplicate_producers() -> None:
    source = {
        "producer": "honua-server-cng",
        "repository": "honua-io/honua-server",
        "workflow_path": ".github/workflows/cng-conformance.yml",
        "artifact_prefix": "cng-certification-",
        "fragment_globs": ["**/protocol-certification-fragment.json"],
        "trusted_branches": ["trunk"],
        "trusted_events": ["schedule"],
        "source_revision_key": "server",
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "registry.json"
        path.write_text(
            json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [source, source]}),
            encoding="utf-8",
        )
        try:
            MODULE.load_registry(path)
        except ValueError as error:
            assert "duplicate" in str(error)
        else:
            raise AssertionError("duplicate registry producer was accepted")


def test_registry_validation_requires_typed_allowlists() -> None:
    source = {
        "producer": "honua-server-cng",
        "repository": "honua-io/honua-server",
        "workflow_path": ".github/workflows/cng-conformance.yml",
        "artifact_prefix": "cng-certification-",
        "fragment_globs": ["**/protocol-certification-fragment.json"],
        "trusted_branches": ["trunk"],
        "trusted_events": ["schedule"],
    }
    for field in ("fragment_globs", "trusted_branches", "trusted_events"):
        malformed = dict(source)
        malformed[field] = "trunk"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(
                json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [malformed]}),
                encoding="utf-8",
            )
            with unittest.TestCase().assertRaisesRegex(ValueError, field):
                MODULE.load_registry(path)


def test_registry_validation_rejects_invalid_artifact_regex() -> None:
    source = {
        "producer": "honua-sdk-python",
        "repository": "honua-io/honua-sdk-python",
        "workflow_path": ".github/workflows/conformance.yml",
        "artifact_prefix": "python-sdk-conformance-",
        "artifact_name_regex": "[",
        "fragment_globs": ["**/protocol-certification-fragment.json"],
        "trusted_branches": ["trunk"],
        "trusted_events": ["workflow_dispatch"],
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "registry.json"
        path.write_text(
            json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [source]}),
            encoding="utf-8",
        )
        with unittest.TestCase().assertRaisesRegex(ValueError, "artifact_name_regex"):
            MODULE.load_registry(path)


def test_registry_validation_rejects_unknown_fields_and_bad_limits() -> None:
    source = {
        "producer": "honua-sdk-python",
        "repository": "honua-io/honua-sdk-python",
        "workflow_path": ".github/workflows/conformance.yml",
        "artifact_prefix": "python-sdk-conformance-",
        "fragment_globs": ["**/protocol-certification-fragment.json"],
        "trusted_branches": ["trunk"],
        "trusted_events": ["workflow_dispatch"],
        "source_revision_key": "sdk-python",
    }
    for mutation, message in [
        ({"artifact_name_regx": ".*"}, "unknown fields"),
        ({"max_artifacts": 0}, "max_artifacts"),
        ({"max_artifacts": 51}, "max_artifacts"),
        ({"max_artifact_pages": 5}, "unknown fields"),
        ({"source_revision_key": ""}, "source_revision_key"),
        ({"max_artifacts": 10, "max_candidates": 9}, "max_candidates"),
        ({"max_candidates": 501}, "max_candidates"),
        ({"required": "yes"}, "required"),
    ]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(
                json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [{**source, **mutation}]}),
                encoding="utf-8",
            )
            with unittest.TestCase().assertRaisesRegex(ValueError, message):
                MODULE.load_registry(path)


def test_missing_token_fails_when_a_producer_is_required() -> None:
    source = {
        "producer": "honua-sdk-python",
        "repository": "honua-io/honua-sdk-python",
        "workflow_path": ".github/workflows/conformance.yml",
        "artifact_prefix": "python-sdk-conformance-",
        "fragment_globs": ["**/protocol-certification-fragment.json"],
        "trusted_branches": ["trunk"],
        "trusted_events": ["workflow_dispatch"],
        "source_revision_key": "sdk-python",
        "required": True,
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = root / "registry.json"
        registry.write_text(
            json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [source]}),
            encoding="utf-8",
        )
        requirements = _requirements(root / "req.json", {"sdk-python": "a" * 40})
        with (
            mock.patch.object(sys, "argv", [
                str(SCRIPT), "--registry", str(registry),
                "--requirements", str(requirements),
                "--output", str(root / "out"),
            ]),
            mock.patch.dict(os.environ, {"HONUA_EVIDENCE_TOKEN": ""}),
        ):
            assert MODULE.main() == 1


class FetchCertificationProducerTests(unittest.TestCase):
    test_select_artifacts = staticmethod(test_select_artifacts_filters_expired_and_keeps_newest)
    test_select_artifacts_regex = staticmethod(test_select_artifacts_applies_full_name_regex)
    test_fetch_counts_only_artifacts_with_fragments = staticmethod(
        test_fetch_counts_only_artifacts_with_fragments
    )
    test_fetch_selects_pinned_run = staticmethod(test_fetch_selects_the_run_at_the_pinned_revision)
    test_fetch_no_fallback = staticmethod(
        test_fetch_never_falls_back_to_a_newer_run_when_the_pin_has_none
    )
    test_fetch_ignores_off_pin_artifacts = staticmethod(
        test_fetch_ignores_artifacts_whose_run_head_is_not_the_pin
    )
    test_pins_from_denominator = staticmethod(test_pins_must_come_from_a_governed_denominator)
    test_extract_fragments = staticmethod(test_extract_fragments_accepts_only_normalized_envelopes)
    test_fragment_destination_names = staticmethod(
        test_fragment_destination_names_resist_normalization_collisions
    )
    test_artifact_redirect_credentials = staticmethod(
        test_artifact_redirect_does_not_forward_github_credentials
    )
    test_producer_pins = staticmethod(test_every_registered_producer_declares_a_denominator_pin)
    test_synthetic_ledger = staticmethod(
        test_synthetic_ledger_fragment_and_negative_fixtures_fail_closed
    )
    test_reserved_producer = staticmethod(test_registry_rejects_reserved_unassigned_producer)
    test_pagination = staticmethod(test_github_listings_follow_every_page)
    test_registry_validation_requires_typed_allowlists = staticmethod(
        test_registry_validation_requires_typed_allowlists
    )
    test_registry_validation_rejects_invalid_artifact_regex = staticmethod(
        test_registry_validation_rejects_invalid_artifact_regex
    )
    test_registry_validation_rejects_unknown_fields_and_bad_limits = staticmethod(
        test_registry_validation_rejects_unknown_fields_and_bad_limits
    )
    test_missing_token_required = staticmethod(test_missing_token_fails_when_a_producer_is_required)
    test_trusted_run = staticmethod(test_trusted_run_requires_successful_configured_workflow_and_identity)
    test_interop_normalizes = staticmethod(
        test_client_interop_receipt_normalizes_to_exact_candidate_fragment
    )
    test_interop_fail_closed = staticmethod(test_client_interop_normalizer_fails_closed)
    test_identity_lane_collision = staticmethod(test_client_identity_rejects_generic_js_lane_collision)
    test_identity_protocol_context = staticmethod(test_client_identity_rejects_missing_protocol_profile)
    test_identity_candidate_revision = staticmethod(test_client_identity_rejects_unresolvable_candidate_revision)
    test_identity_http_probe = staticmethod(test_client_identity_rejects_geopandas_result_performed_by_httpx)
    test_identity_tls_claim = staticmethod(test_client_identity_rejects_plain_http_tls_claim)
    test_interop_run_fail_closed = staticmethod(
        test_client_interop_run_rejects_non_trunk_wrong_event_and_conclusion
    )
    test_interop_unregistered = staticmethod(test_client_interop_unregistered_producer_is_rejected)
    test_fragment_producer = staticmethod(test_fragment_producer_must_match_registry)
    test_fragment_run_head = staticmethod(test_fragment_observations_must_match_trusted_run_head)
    test_registry = staticmethod(test_registry_validation_rejects_duplicate_producers)
