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


def test_fetch_counts_only_artifacts_with_fragments() -> None:
    head_sha = "a" * 40
    source = {
        "producer": "honua-sdk-python",
        "repository": "honua-io/honua-sdk-python",
        "workflow_path": ".github/workflows/conformance.yml",
        "artifact_prefix": "python-sdk-conformance-",
        "artifact_name_regex": r"^python-sdk-conformance-[0-9]+-[0-9]+$",
        "fragment_globs": ["**/protocol-certification-fragment.json"],
        "trusted_branches": ["trunk"],
        "trusted_events": ["workflow_dispatch"],
        "max_artifacts": 1,
        "required": True,
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
            "workflow_run": {"id": 7, "head_sha": head_sha},
        },
    ]

    def request(url, _token, _accept):
        if "/actions/runs/" in url:
            run_id = int(url.rsplit("/", 1)[1])
            return json.dumps({
                "id": run_id, "status": "completed", "conclusion": "success",
                "event": "workflow_dispatch", "head_branch": "trunk", "head_sha": head_sha,
                "run_attempt": 1, "run_started_at": "2026-08-20T10:00:00Z",
                "path": ".github/workflows/conformance.yml",
                "head_repository": {"full_name": "honua-io/honua-sdk-python"},
            }).encode()
        if url == "archive-8":
            return _archive({"server-log.json": {"schema": "diagnostic"}})
        if url == "archive-7":
            return _archive({
                "protocol-certification-fragment.json": {
                    "schema": MODULE.FRAGMENT_SCHEMA,
                    "producer": "honua-sdk-python",
                    "observations": [{"producer_source_sha": head_sha}],
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
        with (
            mock.patch.object(MODULE, "list_artifacts", return_value=artifacts),
            mock.patch.object(MODULE, "request_bytes", side_effect=request),
        ):
            MODULE.fetch(registry, output, "token")
        manifest = [json.loads(line) for line in (output / ".fetch-manifest.ndjson").read_text().splitlines()]
        assert [(row["artifact_id"], row["producer"]) for row in manifest] == [(7, "honua-sdk-python")]


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


def test_list_artifacts_paginates_until_the_last_page() -> None:
    calls = []

    def request(url, _token, _accept):
        calls.append(url)
        rows = [{"id": index} for index in range(100)] if url.endswith("&page=1") else [{"id": 100}]
        return json.dumps({"artifacts": rows}).encode()

    with mock.patch.object(MODULE, "request_bytes", side_effect=request):
        assert len(MODULE.list_artifacts("honua-io/test", "token")) == 101
    assert len(calls) == 2


def test_list_artifacts_stops_at_the_page_bound() -> None:
    calls = []

    def request(url, _token, _accept):
        calls.append(url)
        return json.dumps({"artifacts": [{"id": index} for index in range(100)]}).encode()

    with mock.patch.object(MODULE, "request_bytes", side_effect=request):
        assert len(MODULE.list_artifacts("honua-io/test", "token", max_pages=2)) == 200
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
    }
    for mutation, message in [
        ({"artifact_name_regx": ".*"}, "unknown fields"),
        ({"max_artifacts": 0}, "max_artifacts"),
        ({"max_artifacts": 51}, "max_artifacts"),
        ({"max_artifact_pages": 0}, "max_artifact_pages"),
        ({"max_artifact_pages": 21}, "max_artifact_pages"),
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
        "required": True,
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        registry = root / "registry.json"
        registry.write_text(
            json.dumps({"schema": MODULE.REGISTRY_SCHEMA, "sources": [source]}),
            encoding="utf-8",
        )
        with (
            mock.patch.object(sys, "argv", [
                str(SCRIPT), "--registry", str(registry), "--output", str(root / "out")
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
    test_extract_fragments = staticmethod(test_extract_fragments_accepts_only_normalized_envelopes)
    test_fragment_destination_names = staticmethod(
        test_fragment_destination_names_resist_normalization_collisions
    )
    test_artifact_redirect_credentials = staticmethod(
        test_artifact_redirect_does_not_forward_github_credentials
    )
    test_list_artifacts = staticmethod(test_list_artifacts_paginates_until_the_last_page)
    test_list_artifacts_page_bound = staticmethod(test_list_artifacts_stops_at_the_page_bound)
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
    test_fragment_producer = staticmethod(test_fragment_producer_must_match_registry)
    test_fragment_run_head = staticmethod(test_fragment_observations_must_match_trusted_run_head)
    test_registry = staticmethod(test_registry_validation_rejects_duplicate_producers)
