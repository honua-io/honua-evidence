"""Native campaign receipt binding and explicitly reviewed local intake."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest
from test_aggregate_certification import (module as aggregate, requirement, observation, fragment,
    as_receipt_v2, rebind_receipt, CANDIDATE, REQUIREMENTS_SOURCE_SHA)

spec = importlib.util.spec_from_file_location('native_intake', Path(__file__).resolve().parents[1] / 'scripts/intake-native-campaign.py')
intake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intake)


def native_fixture(tmp_path):
    req = requirement('ArcGIS Pro/arcpy')
    req.update(client_lane='desktop-arcgis', client_driver='arcpy', release_bucket='must-fix-before-cut', client_version='3.4.x')
    obs = as_receipt_v2(observation(req, candidate_cut_at=CANDIDATE['cut_at']), req)
    obs.update(client_driver='arcpy', client_version_observed='3.4.2')
    obs['evidence_receipt']['identity'].update(client_driver='arcpy', client_version_observed='3.4.2')
    rebind_receipt(obs)
    document = fragment('honua-esri-compat', [obs])
    catalog = {'schema': aggregate.REQUIREMENTS_SCHEMA, 'revision': 'rev-1', 'complete': True,
               'source_revisions': {'esri-compat': {'repository': 'honua-io/honua-esri-compat', 'commit': 'c'*40}},
               'requirements': [req]}
    source = tmp_path/'input'; source.mkdir()
    path = source/'fragment.json'; path.write_text(json.dumps(document), encoding='utf-8')
    blob = source/'data'/'sha256'/obs['evidence_digest'][7:]; blob.parent.mkdir(parents=True)
    blob.write_bytes(aggregate._receipt_bytes(obs['evidence_receipt']))
    catalog_path = tmp_path/'requirements.json'; catalog_path.write_text(json.dumps(catalog), encoding='utf-8')
    return req, obs, document, path, catalog_path, blob


def test_native_receipt_is_bound_and_context_is_preserved(tmp_path):
    req, obs, doc, path, _, _ = native_fixture(tmp_path)
    ledger = aggregate.build_ledger('rev-1', REQUIREMENTS_SOURCE_SHA, True, [req], [(path, doc)], CANDIDATE)
    cell = ledger['cells'][0]
    assert cell['result'] == 'pass' and cell['client_driver'] == 'arcpy'
    assert cell['client_version_observed'] == '3.4.2' and cell['release_bucket'] == 'must-fix-before-cut'


@pytest.mark.parametrize('fault', ['receipt-driver', 'receipt-version', 'source', 'cut', 'wrong-driver', 'wrong-version', 'missing-version'])
def test_native_receipt_rejects_unbound_or_wrong_identity(tmp_path, fault):
    req, obs, doc, path, _, _ = native_fixture(tmp_path)
    if fault == 'receipt-driver': obs['evidence_receipt']['identity']['client_driver'] = 'pyqgis'
    elif fault == 'receipt-version': obs['evidence_receipt']['identity']['client_version_observed'] = '3.4.3'
    elif fault == 'source': obs['evidence_receipt']['identity']['producer_source_sha'] = 'd'*40
    elif fault == 'cut': obs['evidence_receipt']['identity']['candidate_cut_at'] = '2026-08-20T09:00:01Z'
    elif fault == 'wrong-driver': obs['client_driver'] = 'pyqgis'
    elif fault == 'wrong-version': obs['client_version_observed'] = '3.5.0'
    else: obs.pop('client_version_observed')
    rebind_receipt(obs)
    with pytest.raises(ValueError):
        aggregate.build_ledger('rev-1', REQUIREMENTS_SOURCE_SHA, True, [req], [(path, doc)], CANDIDATE)


def test_reviewed_intake_requires_source_pin_fragment_hash_and_receipt_blob(tmp_path):
    _, _, _, path, catalog, blob = native_fixture(tmp_path)
    pin = hashlib.sha256(path.read_bytes()).hexdigest()
    def run(dest, **changes):
        args = dict(driver='arcpy', source_key='esri-compat', fragment_sha256=pin,
                    candidate=CANDIDATE, requirements_source_revision=REQUIREMENTS_SOURCE_SHA)
        args.update(changes)
        return intake.intake(path, catalog, dest, **args)
    result = run(tmp_path/'staged')
    assert result['observations'] == 1
    assert (tmp_path/'staged'/'fragments'/'protocol-certification-fragment.json').read_bytes() == path.read_bytes()
    with pytest.raises(ValueError, match='fragment SHA256'):
        run(tmp_path/'bad-hash', fragment_sha256='f'*64)
    with pytest.raises(ValueError, match='source pin'):
        run(tmp_path/'bad-source', source_key='missing')
    blob.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='Receipt bytes'):
        run(tmp_path/'bad-blob')
    assert not (tmp_path/'bad-blob').exists()
