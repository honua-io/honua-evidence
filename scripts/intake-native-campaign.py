#!/usr/bin/env python3
"""Review and stage one native campaign fragment against governed source pins.

This local intake does not authenticate a producer workflow or register a lane.
The operator supplies the reviewed fragment SHA256; receipt blobs are verified
against their content addresses and the ordinary ledger consumer validates the
entire fragment before anything is published.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tempfile


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HERE = Path(__file__).resolve().parent
aggregate = _load('native_intake_aggregate', HERE / 'aggregate-certification.py')
fetch = _load('native_intake_fetch', HERE / 'fetch-certification-producers.py')
PRODUCERS = {'arcpy': ('honua-esri-compat', 'honua-io/honua-esri-compat'),
             'pyqgis': ('honua-client-compat', 'honua-io/honua-client-compat')}


def intake(fragment_path, requirements_path, destination, *, driver, source_key,
           fragment_sha256, candidate, requirements_source_revision):
    fragment_path, requirements_path, destination = map(Path, (fragment_path, requirements_path, destination))
    if driver not in PRODUCERS or not re.fullmatch(r'[0-9a-f]{64}', fragment_sha256 or ''):
        raise ValueError('Known native driver and reviewed exact fragment SHA256 required')
    if not re.fullmatch(r'[0-9a-f]{40}', requirements_source_revision or ''):
        raise ValueError('Exact requirements source revision required')
    if fragment_path.is_symlink() or not fragment_path.is_file():
        raise ValueError('Regular fragment file required')
    raw = fragment_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != fragment_sha256:
        raise ValueError('Reviewed fragment SHA256 differs')
    fragment = aggregate._read_json(fragment_path)
    catalog = aggregate._read_json(requirements_path)
    revision, complete, requirements = aggregate.load_requirements(requirements_path)
    producer, repository = PRODUCERS[driver]
    source = catalog.get('source_revisions', {}).get(source_key)
    if not isinstance(source, dict) or source.get('repository') != repository or not re.fullmatch(r'[0-9a-f]{40}', source.get('commit', '')):
        raise ValueError('Governed source pin must name the native producer repository and full commit')
    if fragment.get('schema') != aggregate.FRAGMENT_SCHEMA or fragment.get('producer') != producer:
        raise ValueError('Native producer or fragment schema differs')
    if fragment.get('candidate') != candidate:
        raise ValueError('Fragment candidate differs from reviewed candidate')
    if fragment.get('operation_scope', {}).get('complete') is not True:
        raise ValueError('Complete declared fragment scope required')
    observations = fragment.get('observations')
    if not isinstance(observations, list) or not observations:
        raise ValueError('At least one native observation required')
    fetch.validate_fragment_producer(fragment, producer, source['commit'], repository)
    blobs = {}
    for observation in observations:
        if observation.get('client_driver') != driver or observation.get('result') not in ('pass', 'fail'):
            raise ValueError('Native intake requires executed observations for the reviewed driver')
        if observation.get('evidence_receipt', {}).get('schema') != aggregate.RECEIPT_SCHEMA_V2:
            raise ValueError('Native intake requires producer-owned v2 receipts')
        digest = observation.get('evidence_digest', '')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('Content-addressed receipt digest required')
        path = fragment_path.parent / 'data' / 'sha256' / digest[7:]
        if path.is_symlink() or not path.is_file():
            raise ValueError('Receipt content-addressed file missing')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest[7:] or data != aggregate._receipt_bytes(observation.get('evidence_receipt')):
            raise ValueError('Receipt bytes do not match content address and observation')
        blobs[digest[7:]] = data
    aggregate.build_ledger(revision, requirements_source_revision, complete, requirements,
                           [(fragment_path, fragment)], candidate)
    if destination.exists():
        raise ValueError('Destination must not already exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-intake-', dir=destination.parent) as temp:
        root = Path(temp) / 'staged'
        (root / 'fragments').mkdir(parents=True)
        (root / 'data' / 'sha256').mkdir(parents=True)
        (root / 'fragments' / 'protocol-certification-fragment.json').write_bytes(raw)
        for digest, data in blobs.items():
            (root / 'data' / 'sha256' / digest).write_bytes(data)
        (root / 'intake.json').write_text(json.dumps({'schema': 'honua.native-reviewed-intake/v1',
            'driver': driver, 'producer': producer, 'provenance_status': 'local-review-unverified',
            'source_key': source_key, 'source_commit': source['commit'],
            'requirements_source_revision': requirements_source_revision, 'fragment_sha256': fragment_sha256},
            sort_keys=True, indent=2) + '\n', encoding='utf-8')
        root.rename(destination)
    return {'observations': len(observations), 'receipt_blobs': len(blobs), 'destination': str(destination)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('fragment', 'requirements', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('driver', 'source-key', 'fragment-sha256', 'requirements-source-revision',
                 'candidate-source-sha', 'candidate-image-digest', 'candidate-cut-at'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    print(json.dumps(intake(args.fragment, args.requirements, args.out, driver=args.driver,
        source_key=args.source_key, fragment_sha256=args.fragment_sha256,
        requirements_source_revision=args.requirements_source_revision,
        candidate={'source_sha': args.candidate_source_sha, 'image_digest': args.candidate_image_digest,
                   'cut_at': args.candidate_cut_at}), sort_keys=True))


if __name__ == '__main__':
    main()
