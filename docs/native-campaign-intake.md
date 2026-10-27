# Reviewed native campaign intake

`scripts/intake-native-campaign.py` stages one locally reviewed native campaign
fragment and its content-addressed receipt files. It does not register a producer
or authenticate an Actions workflow. An operator must independently review the
artifact and provide its SHA256, the exact candidate and the requirements source
revision. The catalog's `source_revisions` entry must already identify the native
producer repository and exact commit. A lane whose producer does not yet have a
governed source pin cannot enter through this path.

```sh
python scripts/intake-native-campaign.py \
  --fragment /reviewed/pyqgis/protocol-certification-fragment.json \
  --requirements /reviewed/protocol-certification-requirements.v1.json \
  --out /staging/native-pyqgis --driver pyqgis --source-key client-compat \
  --fragment-sha256 "$REVIEWED_FRAGMENT_SHA256" \
  --requirements-source-revision "$REQUIREMENTS_SOURCE_SHA" \
  --candidate-source-sha "$CANDIDATE_SHA" --candidate-image-digest "$CANDIDATE_DIGEST" \
  --candidate-cut-at "$CANDIDATE_CUT"
```

The fragment hash, producer and governed commit, candidate, receipt bytes, and
full consumer join are checked before publication. The output contains a
`fragments/` directory for aggregation, `data/sha256/` receipt blobs for evidence
publication, and `intake.json` recording the review inputs. Staging alone does
not issue certification credit; the regular release gate remains authoritative.

## Candidate-bound GitHub Actions intake

`scripts/intake-native-campaign-ci.py` is a separate opt-in path for a portable
export inside a GitHub Actions artifact. It accepts exact run and artifact IDs,
reads the repository-owned producer registry, and fetches the requirements
catalog from the specified honua-release commit. It verifies the catalog bytes,
registered workflow, branch and event, successful run, producer source pin,
GitHub artifact archive digest, exact candidate, and every content-addressed
receipt before staging the fragment and `workflow-proof.json`. The API read
token must have access to the producer repository's Actions artifacts.

```sh
python scripts/intake-native-campaign-ci.py \
  --registry config/protocol-certification-producers.v1.json \
  --requirements /reviewed/protocol-certification-requirements.v1.json \
  --out /staging/native-ci --driver arcpy --run-id 123 --artifact-id 456 \
  --fragment-member arcpy/protocol-certification-fragment.json \
  --requirements-source-revision "$REQUIREMENTS_SOURCE_SHA" \
  --candidate-source-sha "$CANDIDATE_SHA" --candidate-image-digest "$CANDIDATE_DIGEST" \
  --candidate-cut-at "$CANDIDATE_CUT"
```

The command relies on GitHub API authenticity and the already reviewed registry;
it does not accept a local workflow claim. A local reviewed intake has
`provenance_status: local-review-unverified` and is not workflow-authenticated.
Neither path changes the producer registry, publishes evidence, or creates a
release claim. A producer without both its final governed source pin and its
reviewed registered workflow remains unavailable through CI intake. The current
Esri registry entry identifies the existing SDK workflow; a future native workflow
must be reviewed in that registry before it can be used here. PyQGIS likewise
requires a reviewed source pin and registry entry.

`workflow-proof.json` records what the command verified; it is not a signed
attestation. A detached or hand-written copy is not proof of a GitHub run. Run
this intake against GitHub within the trusted CI job each time the staged files
are to be used, and apply the existing release gate afterward.
