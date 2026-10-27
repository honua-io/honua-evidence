#!/usr/bin/env python3
"""Stage one registered native CI artifact after GitHub and ledger validation.

This is opt-in: it does not register a producer or publish certification credit.
Only existing reviewed registry workflow policy and governed source pins are used.
"""
from __future__ import annotations
import argparse
import hashlib
import fnmatch
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


fetch = _module('native_ci_fetch', 'fetch-certification-producers.py')
local = _module('native_ci_local_intake', 'intake-native-campaign.py')
MAX_ARCHIVE = 100 * 1024 * 1024


def _member(name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('Archive member must be a safe relative POSIX path')
    parts = PurePosixPath(name).parts
    if not parts or name.startswith('/') or any(part in ('.', '..') or ':' in part for part in parts):
        raise ValueError('Archive member must be a safe relative POSIX path')
    return name


def intake_ci(registry_path, requirements_path, destination, *, driver, run_id, artifact_id,
              fragment_member, candidate, requirements_source_revision, token):
    if driver not in local.PRODUCERS or type(run_id) is not int or run_id <= 0 or type(artifact_id) is not int or artifact_id <= 0:
        raise ValueError('Known driver and positive exact GitHub run/artifact IDs required')
    if not isinstance(token, str) or not token.strip():
        raise ValueError('GitHub read token required')
    member = _member(fragment_member)
    governed_registry = Path(__file__).resolve().parents[1] / 'config/protocol-certification-producers.v1.json'
    if Path(registry_path).resolve() != governed_registry.resolve():
        raise ValueError('CI intake requires the repository-owned producer registry')
    if not isinstance(requirements_source_revision, str) or not re.fullmatch(r'[0-9a-f]{40}', requirements_source_revision):
        raise ValueError('Exact requirements source revision required')
    catalog_url = ('https://api.github.com/repos/honua-io/honua-release/contents/'
                   'certification/protocol-certification-requirements.v1.json?ref=' + requirements_source_revision)
    catalog_bytes = fetch.request_bytes(catalog_url, token, 'application/vnd.github.raw+json')
    if Path(requirements_path).read_bytes() != catalog_bytes:
        raise ValueError('Requirements bytes differ from the pinned honua-release revision')
    producer, repository = local.PRODUCERS[driver]
    registry = fetch.load_registry(Path(registry_path))
    matches = [source for source in registry['sources'] if source['producer'] == producer and source['repository'] == repository]
    if len(matches) != 1:
        raise ValueError('Exactly one registered native producer workflow is required')
    source = matches[0]
    pins = fetch.load_pinned_revisions(Path(requirements_path))
    source_key = source['source_revision_key']
    if source_key not in pins:
        raise ValueError('Registered native producer has no governed source pin')
    base = 'https://api.github.com/repos/' + repository + '/actions'
    run = json.loads(fetch.request_bytes(f'{base}/runs/{run_id}', token, 'application/vnd.github+json'))
    artifacts = fetch.list_run_artifacts(repository, run_id, token)
    matches = [artifact for artifact in artifacts if artifact.get('id') == artifact_id]
    if len(matches) != 1:
        raise ValueError('Artifact is absent or ambiguous in the exact GitHub run')
    artifact = matches[0]
    candidates = fetch.select_artifacts([artifact], source['artifact_prefix'], 1, source.get('artifact_name_regex'))
    if (run.get('id') != run_id or run.get('head_sha') != pins[source_key] or len(candidates) != 1
            or not fetch.trusted_run(run, artifact, source)):
        raise ValueError('Artifact is not from the registered trusted workflow at the governed source pin')
    # Construct the API endpoint rather than forwarding credentials to an artifact-provided URL.
    archive = fetch.request_bytes(f'{base}/artifacts/{artifact_id}/zip', token, 'application/vnd.github+json')
    digest = 'sha256:' + hashlib.sha256(archive).hexdigest()
    if len(archive) > MAX_ARCHIVE or artifact.get('digest') != digest:
        raise ValueError('GitHub artifact archive digest differs or archive is too large')
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        infos = package.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or sum(info.file_size for info in infos) > MAX_ARCHIVE:
            raise ValueError('Archive has duplicate members or exceeds size limit')
        if member not in names or not any(fnmatch.fnmatch(member, pattern)
                or fnmatch.fnmatch('x/' + member, pattern) for pattern in source['fragment_globs']):
            raise ValueError('Requested fragment is not covered by the registered artifact policy')
        raw = package.read(member)
        document = json.loads(raw, object_pairs_hook=local.aggregate._unique_json_object)
        observations = document.get('observations')
        if not isinstance(observations, list):
            raise ValueError('Fragment observations required')
        parent = str(PurePosixPath(member).parent)
        prefix = '' if parent == '.' else parent + '/'
        blobs = {}
        for observation in observations:
            pin = observation.get('evidence_digest', '') if isinstance(observation, dict) else ''
            if not isinstance(pin, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', pin):
                raise ValueError('Content-addressed native receipt required')
            blob_name = _member(prefix + 'data/sha256/' + pin[7:])
            if blob_name not in names:
                raise ValueError('Artifact is missing a receipt blob')
            blobs[pin[7:]] = package.read(blob_name)
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Destination must not already exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-ci-', dir=destination.parent) as scratch:
        scratch = Path(scratch)
        fragment_path = scratch / 'input' / 'protocol-certification-fragment.json'
        fragment_path.parent.mkdir()
        fragment_path.write_bytes(raw)
        blob_dir = fragment_path.parent / 'data' / 'sha256'; blob_dir.mkdir(parents=True)
        for pin, value in blobs.items():
            (blob_dir / pin).write_bytes(value)
        staged = scratch / 'staged'
        result = local.intake(fragment_path, requirements_path, staged, driver=driver, source_key=source_key,
            fragment_sha256=hashlib.sha256(raw).hexdigest(), candidate=candidate,
            requirements_source_revision=requirements_source_revision)
        proof = {'schema': 'honua.native-campaign-github-artifact-proof/v1', 'repository': repository,
                 'workflow_path': source['workflow_path'], 'head_sha': run['head_sha'],
                 'head_branch': run['head_branch'], 'event': run['event'], 'run_id': run_id,
                 'run_attempt': run['run_attempt'], 'artifact_id': artifact_id, 'artifact_name': artifact['name'],
                 'archive_sha256': digest, 'fragment_member': member,
                 'fragment_sha256': hashlib.sha256(raw).hexdigest(), 'candidate': candidate,
                 'source_revision_key': source_key, 'requirements_source_revision': requirements_source_revision}
        (staged / 'workflow-proof.json').write_text(json.dumps(proof, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        staged.rename(destination)
    result['destination'] = str(destination)
    result['workflow_proof'] = proof
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('registry', 'requirements', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('driver', 'fragment-member', 'requirements-source-revision', 'candidate-source-sha',
                 'candidate-image-digest', 'candidate-cut-at'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--artifact-id', type=int, required=True)
    parser.add_argument('--token-env', default='HONUA_EVIDENCE_TOKEN')
    args = parser.parse_args()
    value = intake_ci(args.registry, args.requirements, args.out, driver=args.driver, run_id=args.run_id,
        artifact_id=args.artifact_id, fragment_member=args.fragment_member,
        requirements_source_revision=args.requirements_source_revision,
        candidate={'source_sha': args.candidate_source_sha, 'image_digest': args.candidate_image_digest,
                   'cut_at': args.candidate_cut_at}, token=os.environ.get(args.token_env, ''))
    print(json.dumps(value, sort_keys=True))


if __name__ == '__main__':
    main()
