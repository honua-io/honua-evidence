"""Unit tests for scripts/generate-brief.py (honua-io/honua-evidence#4).

Covers the per-prospect evidence-brief generator: capability-list inputs
(``--caps`` intake list, ``?caps=`` shareable URL, honua-caps.v1 JSON from
honua-esri-assess's EsriFootprint crosswalk), the BUYER-SHAREABLE front
matter contract, the honesty mechanics (gap disclosures on every card with
no flag to remove them; missing/stale producers restated, never papered
over), the buyer-shareable output guard (private repo names and personal
email addresses can never be written -- fail closed, exit 3), and a real
end-to-end generation run against the committed
data/capability-matrix.v1.json.

Run: python3 -m unittest discover -s tests

No network access is used or required: the generator is offline by design
(it only reads the already-aggregated matrix JSON).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_MATRIX = REPO_ROOT / "data" / "capability-matrix.v1.json"
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Hyphenated filenames cannot be imported with a plain `import` statement.
gb = _load("generate_brief", "generate-brief.py")


def synthetic_matrix() -> dict:
    """A minimal but structurally complete matrix: one fully-evidenced
    Community capability, one Enterprise capability with degradation markers
    (noSurface, producer-missing SDK snapshot, category-level open issue),
    and a ledger with fresh, stale, and missing producers."""
    return {
        "schemaVersion": "2.3.0",
        "generatedAt": "2026-07-27T00:00:00Z",
        "unjoinedCiteSuites": ["GML 3.2"],
        "freshness": {
            "server-matrix": {"fetchedAt": "x", "sourceVersion": "aaa@2026-07-27T00:00:00Z", "ageDays": 0, "status": "fresh"},
            "sdk-js": {"fetchedAt": "x", "sourceVersion": "bbb@2026-07-27T00:00:00Z", "ageDays": 0, "status": "fresh"},
            "sdk-dotnet": {"fetchedAt": "x", "sourceVersion": None, "status": "missing", "detail": "fetch failed"},
            "sdk-python": {"fetchedAt": "x", "sourceVersion": "ccc@2026-07-27T00:00:00Z", "ageDays": 0, "status": "fresh"},
            "samples": {"fetchedAt": "x", "sourceVersion": "ddd@2026-07-27T00:00:00Z", "ageDays": 0, "status": "fresh"},
            "cite": {"fetchedAt": "x", "sourceVersion": "eee@2026-05-20T00:00:00Z", "ageDays": 68, "status": "stale"},
            "dr-drills": {"fetchedAt": "x", "sourceVersion": None, "status": "missing", "detail": "none pushed yet"},
            "live-canary": {"fetchedAt": "x", "sourceVersion": None, "status": "missing", "detail": "none pushed yet"},
        },
        "capabilities": [
            {
                "key": "serve.example",
                "displayName": "Example Serving",
                "category": "Serve",
                "edition": "Community",
                "entryCount": 10,
                "provingTestCount": 100,
                "maturity": {"implemented": 10},
                "noSurface": None,
                "cite": [{"suite": "Suite X 1.0", "profile": "default", "passed": 5, "total": 5, "passRate": 100.0}],
                "parity": [],
                "esriAssess": [],
                "interop": [{"clientLane": "js", "protocol": "example"}],
                "geobench": ["workload-a"],
                "dr": [],
                "liveCanary": [],
                "sdks": {
                    "js": {"status": "covered", "sinceVersion": "1.0.0"},
                    "dotnet": {"status": "producer-missing"},
                    "python": {"status": "not-covered"},
                },
                "samples": [
                    {
                        "id": "s1",
                        "title": "Example walkthrough",
                        "url": "https://samples.honua.io/s1",
                        "sdks": ["js"],
                        "edition": "community",
                        "lastRun": {"outcome": "pass", "serverVersion": "1.0.0", "at": "2026-07-27T00:00:00Z"},
                    }
                ],
                "openIssues": {
                    "count": 1,
                    "refs": [{"number": 42, "title": "Widen coverage", "url": "https://example.test/i/42"}],
                    "categoryLevel": True,
                    "keyRefs": [],
                    "categoryRefs": [{"number": 42, "title": "Widen coverage", "url": "https://example.test/i/42"}],
                    "label": "cap/serve",
                },
            },
            {
                "key": "editing.enterprise-thing",
                "displayName": "Enterprise Thing",
                "category": "Editing",
                "edition": "Enterprise",
                "entryCount": 0,
                "provingTestCount": 0,
                "maturity": {},
                "noSurface": {"capability": "editing.enterprise-thing", "reasonCode": "cross-cutting-gate", "reason": "gate applied elsewhere"},
                "cite": [],
                "parity": [],
                "esriAssess": [],
                "interop": [],
                "geobench": [],
                "dr": [],
                "liveCanary": [],
                "sdks": {},
                "samples": [],
                "openIssues": {"count": 0, "refs": [], "categoryLevel": False, "keyRefs": [], "categoryRefs": [], "label": "cap/editing"},
            },
        ],
    }


def render(matrix=None, keys=("serve.example",), **kwargs) -> str:
    matrix = matrix or synthetic_matrix()
    caps = gb.resolve_capabilities(matrix, list(keys))
    defaults = {"prospect": "Test Prospect", "units": None, "unmapped": [], "generated_at": "2026-07-27T12:00:00Z"}
    defaults.update(kwargs)
    return gb.render_brief(matrix, caps, **defaults)


class CapsInputTests(unittest.TestCase):
    def test_parse_caps_url_extracts_keys_and_units(self):
        keys, units = gb.parse_caps_url("https://honua.io/capabilities.html?caps=serve.wms,serve.wmts&units=4")
        self.assertEqual(keys, ["serve.wms", "serve.wmts"])
        self.assertEqual(units, 4)

    def test_parse_caps_url_without_units(self):
        keys, units = gb.parse_caps_url("https://honua.io/capabilities.html?caps=serve.wms")
        self.assertEqual(keys, ["serve.wms"])
        self.assertIsNone(units)

    def test_parse_caps_url_without_caps_fails(self):
        with self.assertRaises(gb.BriefInputError):
            gb.parse_caps_url("https://honua.io/capabilities.html")

    def test_caps_file_honua_caps_v1(self):
        payload = {
            "schemaVersion": "honua-caps.v1",
            "capabilities": [{"key": "serve.example", "assessKeys": ["a"], "matchedInventoryCount": 3, "tier": "go"}],
            "unmapped": [{"assessKey": "utility-network", "matchedInventoryCount": 1, "tier": "no-go", "reason": "not-supported"}],
            "unitsEstimate": 7,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "honua-caps.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            keys, units, unmapped = gb.load_caps_file(path)
        self.assertEqual(keys, ["serve.example"])
        self.assertEqual(units, 7)
        self.assertEqual(unmapped[0]["assessKey"], "utility-network")

    def test_caps_file_rejects_non_caps_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "junk.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(gb.BriefInputError):
                gb.load_caps_file(path)

    def test_unknown_key_fails_loudly(self):
        with self.assertRaises(gb.BriefInputError) as ctx:
            gb.resolve_capabilities(synthetic_matrix(), ["serve.example", "made.up-key"])
        self.assertIn("made.up-key", str(ctx.exception))
        self.assertIn("fabricat", str(ctx.exception))

    def test_duplicate_keys_deduped_preserving_order(self):
        self.assertEqual(gb.dedupe(["b", "a", "b", "a"]), ["b", "a"])


class EditionEstimateTests(unittest.TestCase):
    def test_highest_edition_wins_with_drivers(self):
        matrix = synthetic_matrix()
        caps = gb.resolve_capabilities(matrix, ["serve.example", "editing.enterprise-thing"])
        edition, drivers = gb.edition_estimate(caps)
        self.assertEqual(edition, "Enterprise")
        self.assertEqual(drivers, ["editing.enterprise-thing"])

    def test_all_community(self):
        matrix = synthetic_matrix()
        caps = gb.resolve_capabilities(matrix, ["serve.example"])
        self.assertEqual(gb.edition_estimate(caps), ("Community", ["serve.example"]))


class BriefContentTests(unittest.TestCase):
    def test_front_matter_carries_classification_and_matrix_version(self):
        text = render()
        head = text.split("\n\n", 1)[0]
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("classification: BUYER-SHAREABLE", head)
        self.assertIn("matrixSchemaVersion: 2.3.0", head)
        self.assertIn("matrixGeneratedAt: 2026-07-27T00:00:00Z", head)
        self.assertIn("generatedAt: 2026-07-27T12:00:00Z", head)
        self.assertIn("sourceOfTruth:", head)
        self.assertIn("https://evidence.honua.io/", head)
        self.assertIn("prospect: Test Prospect", head)
        self.assertIn("human-in-the-loop", head)

    def test_gap_section_present_on_every_card_even_without_gaps(self):
        text = render(keys=("serve.example", "editing.enterprise-thing"))
        self.assertEqual(text.count("Known gaps (disclosed by default; this section cannot be removed):"), 2)
        self.assertIn("[Widen coverage](https://example.test/i/42) (category-level)", text)
        self.assertIn("No dedicated route surface", text)

    def test_no_cli_flag_can_remove_gaps(self):
        # The honesty mechanic: assert no option on either subcommand even
        # smells like gap suppression.
        parser = gb.build_parser()
        subparsers = [
            choice
            for action in parser._actions
            if isinstance(action, type(parser._subparsers._group_actions[0]))
            for choice in action.choices.values()
        ]
        pattern = ("gap", "hide", "omit", "exclude", "skip", "strip", "clean")
        for sub in subparsers:
            for action in sub._actions:
                for opt in action.option_strings:
                    for needle in pattern:
                        self.assertNotIn(needle, opt.lower(), f"suspicious gap-suppressing option {opt}")

    def test_missing_and_stale_producers_disclosed(self):
        text = render()
        # Ledger table + degraded callouts.
        self.assertIn("## Evidence freshness at generation time", text)
        self.assertIn("`dr-drills`: **missing**", text)
        self.assertIn("`cite`: **stale**", text)
        # Per-card restatement: stale CITE, producer-missing SDK lane, missing
        # DR/canary producers.
        self.assertIn("cite snapshot stale at generation time (68 days old)", text)
        self.assertIn("dotnet: coverage snapshot unavailable at generation time (not a coverage claim)", text)
        self.assertIn("dr-drills produced no evidence at generation time", text)
        self.assertIn("live-canary produced no evidence at generation time", text)

    def test_never_produced_producer_is_disclosed_as_not_built_not_as_missing(self):
        """honua-io/honua-release#89: a producer with no ledger row because it
        has never produced anything is disclosed as "not built yet", not as a
        snapshot that went missing -- and never silently dropped, because a
        buyer-shareable brief must not let an absent lane read as coverage."""
        matrix = synthetic_matrix()
        del matrix["freshness"]["dr-drills"]
        matrix["awaitingFirstEnvelope"] = ["dr-drills"]
        text = render(matrix=matrix)
        self.assertIn("Producers not built yet", text)
        self.assertIn("`dr-drills`: **not built yet**", text)
        self.assertIn("dr-drills has never produced evidence; this lane is not built yet", text)
        self.assertNotIn("dr-drills produced no evidence at generation time", text)
        self.assertNotIn("dr-drills snapshot absent from the freshness ledger", text)

    def test_producer_absent_from_ledger_and_not_awaiting_is_still_flagged(self):
        """The awaitingFirstEnvelope escape hatch applies ONLY to producers the
        matrix explicitly declares. Anything else missing from the ledger is
        still called out."""
        matrix = synthetic_matrix()
        del matrix["freshness"]["dr-drills"]
        text = render(matrix=matrix)
        self.assertIn("dr-drills snapshot absent from the freshness ledger at generation time", text)

    def test_l2_links_and_receipts_pointer_present(self):
        text = render(keys=("serve.example",))
        self.assertIn("https://evidence.honua.io/capabilities/serve-example.html", text)

    def test_units_and_unmapped_footprint_keys_disclosed(self):
        unmapped = [{"assessKey": "utility-network", "matchedInventoryCount": 2, "tier": "no-go", "reason": "not-supported"}]
        text = render(units=7, unmapped=unmapped)
        self.assertIn("Serving-unit estimate: 7", text)
        self.assertIn("`utility-network`", text)
        self.assertIn("not-supported", text)

    def test_unjoined_cite_suites_disclosed(self):
        self.assertIn("GML 3.2", render())

    def test_public_contact_only(self):
        text = render()
        self.assertIn("info@honua.io", text)
        self.assertIn("security@honua.io", text)


class OutputGuardTests(unittest.TestCase):
    def test_scan_forbidden_is_case_insensitive(self):
        hits = gb.scan_forbidden("see Honua-Sales and MIKE@honua.io for details")
        self.assertIn("honua-sales", hits)
        self.assertIn("mike@honua.io", hits)

    def test_guard_blocks_output_and_writes_nothing(self):
        matrix = synthetic_matrix()
        matrix["capabilities"][0]["openIssues"]["refs"][0]["title"] = "sync with honua-sales templates"
        with tempfile.TemporaryDirectory() as tmp:
            matrix_path = Path(tmp) / "matrix.json"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            out_path = Path(tmp) / "brief.md"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = gb.main(
                    ["brief", "--prospect", "P", "--caps", "serve.example",
                     "--matrix", str(matrix_path), "--output", str(out_path)]
                )
            self.assertEqual(code, 3)
            self.assertFalse(out_path.exists(), "guard hit must write nothing (fail closed)")
            self.assertIn("buyer-shareable output guard", stderr.getvalue())

    def test_unknown_key_exit_code(self):
        matrix = synthetic_matrix()
        with tempfile.TemporaryDirectory() as tmp:
            matrix_path = Path(tmp) / "matrix.json"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = gb.main(
                    ["brief", "--prospect", "P", "--caps", "nope.nope", "--matrix", str(matrix_path), "--output", "-"]
                )
            self.assertEqual(code, 2)
            self.assertIn("nope.nope", stderr.getvalue())


class CommittedMatrixTests(unittest.TestCase):
    """End-to-end runs against the real committed matrix: the brief for the
    FULL capability set must be free of internal-only strings (this is the
    public-repo / buyer-shareable enforcement test from issue #4), and the
    CLI happy path must write a real file."""

    @classmethod
    def setUpClass(cls):
        cls.matrix = json.loads(COMMITTED_MATRIX.read_text(encoding="utf-8"))

    def test_full_brief_over_all_capabilities_has_no_internal_leaks(self):
        keys = [cap["key"] for cap in self.matrix["capabilities"]]
        caps = gb.resolve_capabilities(self.matrix, keys)
        text = gb.render_brief(
            self.matrix, caps, prospect="Leak Scan", units=None, unmapped=[], generated_at=gb.utc_now_iso()
        )
        self.assertEqual(gb.scan_forbidden(text), [], "brief rendered from the committed matrix leaked internal-only terms")
        lowered = text.lower()
        self.assertNotIn("mike@honua.io", lowered)
        self.assertIn("info@honua.io", lowered)

    def test_cli_end_to_end_writes_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "acme-brief.md"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gb.main(
                    ["brief", "--prospect", "Acme County",
                     "--caps", "serve.ogc-api-features, editing.featureserver-edits",
                     "--matrix", str(COMMITTED_MATRIX), "--output", str(out_path)]
                )
            self.assertEqual(code, 0)
            text = out_path.read_text(encoding="utf-8")
        self.assertIn("classification: BUYER-SHAREABLE", text)
        self.assertIn("# Honua Evidence Brief -- Acme County", text)
        self.assertIn("capabilities/serve-ogc-api-features.html", text)
        self.assertIn("Known gaps (disclosed by default; this section cannot be removed):", text)
        # The committed matrix has missing pushed-envelope producers; the
        # brief must say so rather than claiming coverage.
        self.assertIn("dr-drills", text)

    def test_caps_url_source_end_to_end(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = gb.main(
                ["brief", "--prospect", "URL Prospect",
                 "--caps-url", "https://honua.io/capabilities.html?caps=serve.ogc-api-features&units=3",
                 "--matrix", str(COMMITTED_MATRIX), "--output", "-"]
            )
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertIn("Serving-unit estimate: 3", text)
        self.assertIn("`serve.ogc-api-features`", text)


class ProofCountsTests(unittest.TestCase):
    def test_block_shape_markers_counts_and_disclosures(self):
        matrix = synthetic_matrix()
        text = gb.render_proof_counts(matrix, "2026-07-27T12:00:00Z")
        self.assertIn("<!-- BEGIN GENERATED: honua-evidence proof-counts", text)
        self.assertIn("<!-- END GENERATED: honua-evidence proof-counts -->", text)
        self.assertIn("| Suite X 1.0 | default | 5 / 5 | 100.0% | `serve.example` |", text)
        self.assertIn("ledger status: stale", text)
        self.assertIn("GML 3.2", text)
        self.assertIn("schemaVersion 2.3.0", text)

    def test_committed_matrix_proof_counts_cli(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = gb.main(["proof-counts", "--matrix", str(COMMITTED_MATRIX), "--output", "-"])
        self.assertEqual(code, 0)
        text = stdout.getvalue()
        self.assertEqual(gb.scan_forbidden(text), [])
        self.assertIn("OGC API Features 1.0", text)
        self.assertIn("Source of truth: <https://evidence.honua.io/data/capability-matrix.v1.json>", text)


if __name__ == "__main__":
    unittest.main()
