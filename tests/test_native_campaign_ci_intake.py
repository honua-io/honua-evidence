"""No network tests for authenticated, candidate-bound native CI intake."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import zipfile
import pytest
from test_native_campaign_intake import native_fixture
from test_aggregate_certification import CANDIDATE, REQUIREMENTS_SOURCE_SHA

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_ci_intake', ROOT/'scripts/intake-native-campaign-ci.py')
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)
REGISTRY = ROOT/'config/protocol-certification-producers.v1.json'


def setup_artifact(tmp_path, monkeypatch):
    _, _, _, fragment, requirements, blob = native_fixture(tmp_path)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('arcpy/protocol-certification-fragment.json', fragment.read_bytes())
        archive.writestr('arcpy/data/sha256/' + blob.name, blob.read_bytes())
    content = stream.getvalue()
    run = {'id': 101, 'head_sha': 'c'*40, 'head_branch': 'trunk', 'event': 'workflow_dispatch',
           'status': 'completed', 'conclusion': 'success', 'run_attempt': 1,
           'run_started_at': '2026-08-20T09:00:00Z', 'path': '.github/workflows/esri-sdk-certification.yml',
           'head_repository': {'full_name': 'honua-io/honua-esri-compat'}}
    artifact = {'id': 202, 'name': 'esri-protocol-certification-101', 'expired': False,
                'archive_download_url': 'https://api.github.com/unused', 'created_at': '2026-08-20T10:00:00Z',
                'workflow_run': {'id': 101, 'head_sha': 'c'*40},
                'digest': 'sha256:' + hashlib.sha256(content).hexdigest()}
    calls = []
    def request(url, token, accept):
        calls.append(url)
        if '/honua-release/contents/' in url: return requirements.read_bytes()
        if url.endswith('/runs/101'): return json.dumps(run).encode()
        if url.endswith('/artifacts/202/zip'): return content
        raise AssertionError(url)
    monkeypatch.setattr(ci.fetch, 'request_bytes', request)
    monkeypatch.setattr(ci.fetch, 'list_run_artifacts', lambda *args: [artifact])
    def invoke(dest, **overrides):
        args = dict(driver='arcpy', run_id=101, artifact_id=202,
                    fragment_member='arcpy/protocol-certification-fragment.json', candidate=CANDIDATE,
                    requirements_source_revision=REQUIREMENTS_SOURCE_SHA, token='read-token')
        args.update(overrides)
        return ci.intake_ci(REGISTRY, requirements, dest, **args)
    return invoke, run, artifact, calls, requirements


def test_registered_ci_run_and_archive_proof_bound_to_candidate(tmp_path, monkeypatch):
    invoke, _, _, calls, _ = setup_artifact(tmp_path, monkeypatch)
    output = tmp_path/'staged'
    result = invoke(output)
    assert result['workflow_proof']['run_id'] == 101
    assert result['workflow_proof']['candidate'] == CANDIDATE
    assert (output/'workflow-proof.json').is_file()
    assert len(list((output/'data/sha256').iterdir())) == 1
    assert any(url.endswith('/artifacts/202/zip') for url in calls)


@pytest.mark.parametrize('fault', ['wrong-source', 'wrong-workflow', 'wrong-run', 'wrong-digest',
                                    'foreign-repo', 'failed', 'wrong-candidate', 'untrusted-path'])
def test_untrusted_run_or_candidate_never_stages(tmp_path, monkeypatch, fault):
    invoke, run, artifact, _, _ = setup_artifact(tmp_path, monkeypatch)
    changes = {}
    if fault == 'wrong-source': run['head_sha'] = 'd'*40
    elif fault == 'wrong-workflow': run['path'] = '.github/workflows/unreviewed.yml'
    elif fault == 'wrong-run': artifact['workflow_run']['id'] = 999
    elif fault == 'wrong-digest': artifact['digest'] = 'sha256:' + '0'*64
    elif fault == 'foreign-repo': run['head_repository']['full_name'] = 'attacker/fork'
    elif fault == 'failed': run['conclusion'] = 'failure'
    elif fault == 'wrong-candidate': changes['candidate'] = dict(CANDIDATE, source_sha='f'*40)
    else: changes['fragment_member'] = '../protocol-certification-fragment.json'
    with pytest.raises(ValueError): invoke(tmp_path/'out', **changes)
    assert not (tmp_path/'out').exists()


def test_unregistered_driver_and_unsigned_local_catalog_rejected(tmp_path, monkeypatch):
    invoke, _, _, _, requirements = setup_artifact(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='registered'):
        invoke(tmp_path/'py', driver='pyqgis')
    requirements.write_text('{}', encoding='utf-8')
    # Simulate GitHub returning different reviewed bytes than the local file.
    prior = ci.fetch.request_bytes
    monkeypatch.setattr(ci.fetch, 'request_bytes', lambda url, token, accept:
        b'{"different":true}' if '/honua-release/contents/' in url else prior(url, token, accept))
    with pytest.raises(ValueError, match='Requirements bytes differ'):
        invoke(tmp_path/'bad-catalog')
