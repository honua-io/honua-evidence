from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path

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
