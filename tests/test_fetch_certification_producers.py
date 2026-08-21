from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "fetch-certification-producers.py"
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


def test_extract_fragments_accepts_only_normalized_envelopes() -> None:
    archive = _archive({
        "nested/protocol-certification-fragment.json": {"schema":MODULE.FRAGMENT_SCHEMA,"producer":{"repository":"honua-io/test"}},
        "nested/unrelated.json": {"schema":"other"},
    })
    assert MODULE.extract_fragments(archive, ["**/protocol-certification-fragment.json"]) == [(
        "nested/protocol-certification-fragment.json",
        {"schema":MODULE.FRAGMENT_SCHEMA,"producer":{"repository":"honua-io/test"}},
    )]


def test_list_artifacts_paginates_until_the_last_page() -> None:
    calls = []

    def request(url, _token, _accept):
        calls.append(url)
        rows = [{"id": index} for index in range(100)] if url.endswith("&page=1") else [{"id": 100}]
        return json.dumps({"artifacts": rows}).encode()

    with mock.patch.object(MODULE, "request_bytes", side_effect=request):
        assert len(MODULE.list_artifacts("honua-io/test", "token")) == 101
    assert len(calls) == 2


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
        "path": ".github/workflows/certify.yml",
        "head_repository": {"full_name": "honua-io/test"},
    }
    artifact = {"workflow_run": {"id": 7, "head_sha": "a" * 40}}
    assert MODULE.trusted_run(run, artifact, source)
    assert not MODULE.trusted_run({**run, "conclusion": "failure"}, artifact, source)
    assert not MODULE.trusted_run({**run, "head_branch": "feature"}, artifact, source)


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


class FetchCertificationProducerTests(unittest.TestCase):
    test_select_artifacts = staticmethod(test_select_artifacts_filters_expired_and_keeps_newest)
    test_extract_fragments = staticmethod(test_extract_fragments_accepts_only_normalized_envelopes)
    test_list_artifacts = staticmethod(test_list_artifacts_paginates_until_the_last_page)
    test_trusted_run = staticmethod(test_trusted_run_requires_successful_configured_workflow_and_identity)
    test_fragment_producer = staticmethod(test_fragment_producer_must_match_registry)
    test_fragment_run_head = staticmethod(test_fragment_observations_must_match_trusted_run_head)
    test_registry = staticmethod(test_registry_validation_rejects_duplicate_producers)
