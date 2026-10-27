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
