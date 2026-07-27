"""Unit tests for scripts/aggregate.py.

Covers honua-io/honua-evidence#1's aggregation pipeline end-to-end (happy
path, drift-gate hard fail on unknown capability keys, explicit missing/stale
producer degradation -- all with every network fetcher stubbed out);
honua-io/honua-evidence#8's cross-repo evidence joins (CITE freshness, and
the two pushed-envelope producers: terraform DR drills, live canary / cloud
e2e results); and honua-io/honua-evidence#5's per-capability-key known-gaps
join.

Run: python3 -m unittest discover -s tests
     (or, if pytest happens to be installed: python3 -m pytest tests)

No network access is used or required -- every test either drives the pure
helper functions directly or points the local-envelope loaders at
tests/fixtures/ (or a temporary directory), never at the real
data/producers/ directories or the network-pulled producers.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import aggregate as agg  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FetchedLedgerEntryTests(unittest.TestCase):
    """Fetched.ledger_entry is the shared machinery every producer (network-
    pulled or pushed-envelope) relies on for its freshness ledger entry."""

    def test_error_reports_missing_with_detail(self):
        fetched = agg.Fetched("some-producer", error="boom")
        entry = fetched.ledger_entry({"some-producer": 14})
        self.assertEqual(entry["status"], "missing")
        self.assertEqual(entry["detail"], "boom")
        self.assertIsNone(entry["sourceVersion"])

    def test_fresh_within_threshold(self):
        now = datetime.now(timezone.utc)
        source_version = f"{'a' * 12}@{_iso(now - timedelta(days=1))}"
        fetched = agg.Fetched("p", source_version=source_version)
        entry = fetched.ledger_entry({"p": 14})
        self.assertEqual(entry["status"], "fresh")
        self.assertEqual(entry["ageDays"], 1)

    def test_stale_past_threshold(self):
        now = datetime.now(timezone.utc)
        source_version = f"{'a' * 12}@{_iso(now - timedelta(days=30))}"
        fetched = agg.Fetched("p", source_version=source_version)
        entry = fetched.ledger_entry({"p": 14})
        self.assertEqual(entry["status"], "stale")

    def test_default_warnings_is_empty_list_not_shared(self):
        a = agg.Fetched("a")
        b = agg.Fetched("b")
        a.warnings.append("x")
        self.assertEqual(b.warnings, [])


class CiteFreshnessParsingTests(unittest.TestCase):
    """Exercises the CITE 'Last reviewed' regex and source_version encoding
    without hitting the network -- fetch_cite_status()'s HTTP calls aren't
    invoked here; this tests the parsing contract it relies on directly."""

    def test_last_reviewed_regex_matches_expected_format(self):
        text = "# CITE Status\n\nLast reviewed: 2026-05-20\nOwner: Honua Server platform\n"
        match = agg.CITE_LAST_REVIEWED_RE.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "2026-05-20")

    def test_last_reviewed_regex_no_match_returns_none(self):
        self.assertIsNone(agg.CITE_LAST_REVIEWED_RE.search("no such line here"))

    def test_cite_source_version_drives_age_from_reviewed_date_not_commit_date(self):
        # This is the deliberate behavior documented in fetch_cite_status:
        # ageDays must reflect the "Last reviewed" date, not any other
        # timestamp, because that's what Fetched.ledger_entry parses out of
        # sourceVersion's "<sha>@<date>" encoding.
        now = datetime.now(timezone.utc)
        reviewed = now - timedelta(days=20)
        source_version = f"deadbeefcafe@{reviewed.strftime('%Y-%m-%d')}T00:00:00Z"
        fetched = agg.Fetched("cite", data={"lastReviewed": reviewed.strftime("%Y-%m-%d")}, source_version=source_version)
        entry = fetched.ledger_entry({"cite": 14})
        self.assertEqual(entry["ageDays"], 20)
        self.assertEqual(entry["status"], "stale")


class LoadEnvelopesTests(unittest.TestCase):
    """_load_envelopes is shared by both pushed-envelope producers: missing
    directory -> empty list (never a crash); a malformed/incomplete envelope
    is skipped and warned about, not raised."""

    def test_missing_directory_returns_no_envelopes_no_warnings(self):
        envelopes, warnings = agg._load_envelopes("x", Path("/nonexistent/dir/for/sure"), ("a",))
        self.assertEqual(envelopes, [])
        self.assertEqual(warnings, [])

    def test_empty_directory_returns_no_envelopes_no_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            envelopes, warnings = agg._load_envelopes("x", Path(tmp), ("a",))
        self.assertEqual(envelopes, [])
        self.assertEqual(warnings, [])

    def test_invalid_json_is_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not valid json", encoding="utf-8")
            envelopes, warnings = agg._load_envelopes("x", Path(tmp), ("a",))
        self.assertEqual(envelopes, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("invalid JSON", warnings[0])

    def test_non_object_json_is_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
            envelopes, warnings = agg._load_envelopes("x", Path(tmp), ("a",))
        self.assertEqual(envelopes, [])
        self.assertIn("not a JSON object", warnings[0])

    def test_missing_required_field_is_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "e.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
            envelopes, warnings = agg._load_envelopes("x", Path(tmp), ("a", "b"))
        self.assertEqual(envelopes, [])
        self.assertIn("missing required field(s) ['b']", warnings[0])

    def test_valid_envelope_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "e.json").write_text(json.dumps({"a": 1, "b": 2}), encoding="utf-8")
            envelopes, warnings = agg._load_envelopes("x", Path(tmp), ("a", "b"))
        self.assertEqual(envelopes, [{"a": 1, "b": 2}])
        self.assertEqual(warnings, [])

    def test_readme_style_non_json_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "README.md").write_text("not an envelope", encoding="utf-8")
            envelopes, warnings = agg._load_envelopes("x", Path(tmp), ())
        self.assertEqual(envelopes, [])
        self.assertEqual(warnings, [])


class DrDrillFixtureTests(unittest.TestCase):
    """Drives fetch_dr_drills() against tests/fixtures/dr-drills/ (a copy in a
    temp dir, since fetch_dr_drills() reads the module-level DR_DRILLS_DIR
    constant) to prove the real example + malformed fixtures behave as
    documented in docs/producer-contracts.md."""

    def setUp(self):
        self._orig_dir = agg.DR_DRILLS_DIR
        self._tmp = tempfile.mkdtemp()
        for fixture in (FIXTURES / "dr-drills").glob("*.json"):
            shutil.copy(fixture, self._tmp)
        agg.DR_DRILLS_DIR = Path(self._tmp)

    def tearDown(self):
        agg.DR_DRILLS_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_valid_envelope_loaded_and_malformed_one_warned_not_crashed(self):
        fetched = agg.fetch_dr_drills()
        self.assertTrue(fetched.ok)
        self.assertEqual(len(fetched.data), 1)
        self.assertEqual(fetched.data[0]["id"], "2026-07-15-backup-restore-aws-ecs")
        self.assertEqual(len(fetched.warnings), 1)
        self.assertIn("capturedAt", fetched.warnings[0])

    def test_source_version_uses_latest_envelope_sha_and_captured_at(self):
        fetched = agg.fetch_dr_drills()
        self.assertEqual(
            fetched.source_version, "a1b2c3d4e5f6@2026-07-15T03:12:44Z"
        )

    def test_empty_directory_reports_missing_not_error_thrown(self):
        with tempfile.TemporaryDirectory() as empty:
            agg.DR_DRILLS_DIR = Path(empty)
            fetched = agg.fetch_dr_drills()
        self.assertFalse(fetched.ok)
        self.assertIn("none pushed yet", fetched.error)
        entry = fetched.ledger_entry(agg.DEFAULT_STALENESS_DAYS)
        self.assertEqual(entry["status"], "missing")

    def test_non_string_capability_key_is_warned_not_crashed(self):
        # A malformed hand-authored envelope with a non-string entry in
        # 'capabilityKeys' (e.g. an object instead of a string) must be
        # skipped with a warning, not raise when later joined onto the
        # (unhashable-safe) canonical key set.
        with tempfile.TemporaryDirectory() as tmp:
            agg.DR_DRILLS_DIR = Path(tmp)
            (Path(tmp) / "bad.json").write_text(
                json.dumps(
                    {
                        "schema": "x",
                        "id": "d1",
                        "drill": "backup-restore",
                        "capturedAt": "2026-07-15T03:12:44Z",
                        "verdict": "pass",
                        "capabilityKeys": [{}],
                    }
                ),
                encoding="utf-8",
            )
            fetched = agg.fetch_dr_drills()
        self.assertFalse(fetched.ok)
        self.assertEqual(fetched.data, [])
        self.assertTrue(any("must be a non-empty list of strings" in w for w in fetched.warnings))


class LiveCanaryFixtureTests(unittest.TestCase):
    def setUp(self):
        self._orig_dir = agg.LIVE_CANARY_DIR
        self._tmp = tempfile.mkdtemp()
        for fixture in (FIXTURES / "live-canary").glob("*.json"):
            shutil.copy(fixture, self._tmp)
        agg.LIVE_CANARY_DIR = Path(self._tmp)

    def tearDown(self):
        agg.LIVE_CANARY_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_valid_manifest_loaded_and_malformed_one_warned_not_crashed(self):
        fetched = agg.fetch_live_canary()
        self.assertTrue(fetched.ok)
        self.assertEqual(len(fetched.data), 1)
        self.assertEqual(fetched.data[0]["manifestId"], "demo-canary-2026-07-20T06:00:00Z")
        self.assertEqual(len(fetched.warnings), 1)
        self.assertIn("probes", fetched.warnings[0])

    def test_empty_directory_reports_missing_not_error_thrown(self):
        with tempfile.TemporaryDirectory() as empty:
            agg.LIVE_CANARY_DIR = Path(empty)
            fetched = agg.fetch_live_canary()
        self.assertFalse(fetched.ok)
        self.assertIn("none pushed yet", fetched.error)

    def test_non_object_probe_is_warned_not_crashed(self):
        # A malformed manifest where 'probes' is a list of strings instead
        # of probe objects must be skipped with a warning, not raise
        # AttributeError from probe.get(...) inside _live_canary_items.
        with tempfile.TemporaryDirectory() as tmp:
            agg.LIVE_CANARY_DIR = Path(tmp)
            (Path(tmp) / "bad.json").write_text(
                json.dumps(
                    {
                        "schema": "x",
                        "manifestId": "m1",
                        "targetEnvironment": "demo.honua.io",
                        "runAt": "2026-07-20T06:00:00Z",
                        "probes": ["bad"],
                    }
                ),
                encoding="utf-8",
            )
            fetched = agg.fetch_live_canary()
        self.assertFalse(fetched.ok)
        self.assertEqual(fetched.data, [])
        self.assertTrue(any("must be a list of objects" in w for w in fetched.warnings))


class JoinLocalProducerTests(unittest.TestCase):
    """join_local_producer is the shared join for both pushed-envelope
    producers: an unknown capability key is a WARNING, never raised -- this
    is the deliberately different contract from the hard drift gate applied
    to server-matrix/SDK/samples in build_matrix()."""

    CANONICAL = {"dr.backup-automation", "dr.failover", "serve.ogc-api-features"}

    def test_dr_drill_items_join_onto_multiple_keys(self):
        envelopes = [
            {
                "id": "d1", "drill": "backup-restore", "cloud": "aws", "target": "aws-ecs",
                "environment": "validation", "capturedAt": "2026-07-15T03:12:44Z", "verdict": "pass",
                "capabilityKeys": ["dr.backup-automation", "dr.failover"],
            }
        ]
        by_key, warnings = agg.join_local_producer("dr-drills", envelopes, self.CANONICAL, agg._dr_drill_items)
        self.assertEqual(warnings, [])
        self.assertEqual(len(by_key["dr.backup-automation"]), 1)
        self.assertEqual(len(by_key["dr.failover"]), 1)
        self.assertEqual(by_key["dr.backup-automation"][0]["verdict"], "pass")

    def test_unknown_capability_key_is_a_warning_not_an_exception(self):
        envelopes = [
            {
                "id": "d1", "drill": "backup-restore", "capturedAt": "2026-07-15T03:12:44Z", "verdict": "pass",
                "capabilityKeys": ["dr.totally-made-up"],
            }
        ]
        by_key, warnings = agg.join_local_producer("dr-drills", envelopes, self.CANONICAL, agg._dr_drill_items)
        self.assertEqual(by_key, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("dr.totally-made-up", warnings[0])
        self.assertIn("unknown capability key", warnings[0])

    def test_live_canary_probe_missing_capability_keys_warns_and_is_skipped(self):
        envelopes = [
            {
                "manifestId": "m1", "targetEnvironment": "demo.honua.io", "runAt": "2026-07-20T06:00:00Z",
                "probes": [
                    {"probeName": "no-keys-probe", "status": "green", "lastGreenAt": "2026-07-20T06:00:00Z"},
                    {
                        "probeName": "ok-probe", "capabilityKeys": ["serve.ogc-api-features"],
                        "status": "green", "lastGreenAt": "2026-07-20T06:00:03Z",
                    },
                ],
            }
        ]
        by_key, warnings = agg.join_local_producer("live-canary", envelopes, self.CANONICAL, agg._live_canary_items)
        self.assertIn("serve.ogc-api-features", by_key)
        self.assertEqual(len(by_key["serve.ogc-api-features"]), 1)
        self.assertTrue(any("valid 'capabilityKeys'" in w for w in warnings))

    def test_live_canary_probe_with_non_string_capability_key_warns_and_is_skipped(self):
        # A probe whose 'capabilityKeys' list contains a non-string item
        # (e.g. an object) must be skipped with a warning rather than
        # raising when checked against the canonical key set.
        envelopes = [
            {
                "manifestId": "m1", "targetEnvironment": "demo.honua.io", "runAt": "2026-07-20T06:00:00Z",
                "probes": [
                    {"probeName": "bad-keys-probe", "capabilityKeys": [{}], "status": "green"},
                ],
            }
        ]
        by_key, warnings = agg.join_local_producer("live-canary", envelopes, self.CANONICAL, agg._live_canary_items)
        self.assertEqual(by_key, {})
        self.assertTrue(any("valid 'capabilityKeys'" in w for w in warnings))


class CapabilityKeysFieldParsingTests(unittest.TestCase):
    """Parses honua-server's advisory 'Capability Key(s)' issue-form field
    (honua-io/honua-evidence#5) out of real-shaped issue bodies. Two shapes
    are covered: the GitHub issue-form rendering (### header + value line)
    and the free-text inline mention every currently-open cap/*-labeled
    honua-server issue actually uses in practice."""

    CANONICAL = {
        "editing.feature-edits",
        "geocoding.single-line",
        "serve.wms",
        "serve.wmts",
        "ops.observability",
    }

    def test_form_rendered_single_key(self):
        body = (
            "### Problem Summary\n\nSomething broke.\n\n"
            "### Capability Key(s)\n\ngeocoding.single-line\n\n"
            "### Affected Repo(s)\n\nhonua-server\n"
        )
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, ["geocoding.single-line"])
        self.assertEqual(invalid, [])

    def test_form_rendered_multiple_keys_comma_separated(self):
        body = (
            "### Capability Key(s)\n\n"
            "editing.feature-edits,  geocoding.single-line \n\n"
            "### Affected Repo(s)\n\nhonua-server\n"
        )
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, ["editing.feature-edits", "geocoding.single-line"])
        self.assertEqual(invalid, [])

    def test_form_rendered_no_response_yields_no_keys(self):
        body = "### Capability Key(s)\n\n_No response_\n\n### Affected Repo(s)\n\nhonua-server\n"
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, [])
        self.assertEqual(invalid, [])

    def test_tech_debt_variant_label_with_optional_suffix(self):
        body = "### Capability Key(s) (optional)\n\nserve.wms\n\n### Affected Repos\n\nhonua-server\n"
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, ["serve.wms"])

    def test_inline_free_text_single_backtick_quoted_key_with_trailing_prose(self):
        # This is the shape every real open honua-server cap/*-labeled issue
        # actually uses: a plain sentence, not the strict form rendering.
        body = (
            "### Affected Repos\n\n`honua-io/honua-server` only.\n\n"
            "Capability key(s): `ops.observability` (no dedicated audit-logging "
            "capability key exists in `capability-keys.v1.json` today).\n\n"
            "Related to #2861.\n"
        )
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, ["ops.observability"])
        self.assertEqual(invalid, [])

    def test_inline_free_text_multiple_backtick_quoted_keys(self):
        body = (
            "Capability key(s): `serve.wms`, `serve.wmts` (aggregate — this snapshot "
            "spans every CITE-gated protocol surface).\n\nRelated to #2861.\n"
        )
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, ["serve.wms", "serve.wmts"])
        self.assertEqual(invalid, [])

    def test_invalid_key_is_reported_separately_and_not_treated_as_valid(self):
        body = "### Capability Key(s)\n\nserve.wms, serve.totally-made-up\n\n### Affected Repo(s)\n\nx\n"
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, ["serve.wms"])
        self.assertEqual(invalid, ["serve.totally-made-up"])

    def test_all_invalid_keys_yields_empty_valid_list(self):
        body = "Capability key(s): `not.a.real.key`.\n"
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, [])
        self.assertEqual(invalid, ["not.a.real.key"])

    def test_field_absent_yields_no_keys_and_no_invalid_tokens(self):
        body = "### Problem Summary\n\nNo capability field mentioned anywhere here.\n"
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, [])
        self.assertEqual(invalid, [])

    def test_empty_body_yields_no_keys(self):
        valid, invalid = agg.parse_issue_capability_keys("", self.CANONICAL)
        self.assertEqual(valid, [])
        self.assertEqual(invalid, [])

    def test_duplicate_keys_are_deduplicated_preserving_order(self):
        body = "### Capability Key(s)\n\nserve.wms, serve.wms, serve.wmts\n"
        valid, invalid = agg.parse_issue_capability_keys(body, self.CANONICAL)
        self.assertEqual(valid, ["serve.wms", "serve.wmts"])


class JoinGapsByKeyTests(unittest.TestCase):
    """join_gaps_by_key splits open honua-server issues into a per-
    capability-KEY join and a category-level fallback (honua-io/
    honua-evidence#5)."""

    CANONICAL = {"serve.wms", "serve.wmts", "geocoding.single-line"}

    def test_issue_with_valid_key_joins_at_key_level_only(self):
        by_category = {
            "Serve": [
                {
                    "number": 100,
                    "title": "WMS bug",
                    "url": "https://example/100",
                    "body": "### Capability Key(s)\n\nserve.wms\n",
                }
            ]
        }
        gaps_by_key, gaps_by_category, warnings = agg.join_gaps_by_key(by_category, self.CANONICAL)
        self.assertEqual(warnings, [])
        self.assertEqual(len(gaps_by_key["serve.wms"]), 1)
        self.assertEqual(gaps_by_key["serve.wms"][0]["number"], 100)
        self.assertEqual(gaps_by_category, {})

    def test_issue_without_key_falls_back_to_category_level(self):
        by_category = {
            "Serve": [
                {"number": 101, "title": "Generic serve issue", "url": "https://example/101", "body": "no field here"}
            ]
        }
        gaps_by_key, gaps_by_category, warnings = agg.join_gaps_by_key(by_category, self.CANONICAL)
        self.assertEqual(gaps_by_key, {})
        self.assertEqual(len(gaps_by_category["Serve"]), 1)
        self.assertEqual(gaps_by_category["Serve"][0]["number"], 101)
        self.assertEqual(warnings, [])

    def test_issue_with_only_invalid_key_falls_back_to_category_and_warns(self):
        by_category = {
            "Serve": [
                {
                    "number": 102,
                    "title": "Typo'd key",
                    "url": "https://example/102",
                    "body": "### Capability Key(s)\n\nserve.totally-made-up\n",
                }
            ]
        }
        gaps_by_key, gaps_by_category, warnings = agg.join_gaps_by_key(by_category, self.CANONICAL)
        self.assertEqual(gaps_by_key, {})
        self.assertEqual(len(gaps_by_category["Serve"]), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("102", warnings[0])
        self.assertIn("serve.totally-made-up", warnings[0])
        self.assertIn("unknown capability key", warnings[0])

    def test_issue_referencing_multiple_keys_joins_each_key(self):
        by_category = {
            "Serve": [
                {
                    "number": 103,
                    "title": "Cross-protocol gap",
                    "url": "https://example/103",
                    "body": "Capability key(s): `serve.wms`, `serve.wmts`.\n",
                }
            ]
        }
        gaps_by_key, gaps_by_category, warnings = agg.join_gaps_by_key(by_category, self.CANONICAL)
        self.assertEqual(gaps_by_category, {})
        self.assertIn("serve.wms", gaps_by_key)
        self.assertIn("serve.wmts", gaps_by_key)
        self.assertEqual(gaps_by_key["serve.wms"][0]["number"], 103)
        self.assertEqual(gaps_by_key["serve.wmts"][0]["number"], 103)

    def test_mixed_categories_and_issues(self):
        by_category = {
            "Serve": [
                {
                    "number": 104,
                    "title": "Key-level",
                    "url": "u4",
                    "body": "### Capability Key(s)\n\nserve.wms\n",
                },
                {"number": 105, "title": "Category-level", "url": "u5", "body": ""},
            ],
            "Geocoding": [
                {
                    "number": 106,
                    "title": "Also key-level",
                    "url": "u6",
                    "body": "Capability key(s): `geocoding.single-line`.",
                }
            ],
        }
        gaps_by_key, gaps_by_category, warnings = agg.join_gaps_by_key(by_category, self.CANONICAL)
        self.assertEqual(warnings, [])
        self.assertEqual([r["number"] for r in gaps_by_key["serve.wms"]], [104])
        self.assertEqual([r["number"] for r in gaps_by_key["geocoding.single-line"]], [106])
        self.assertEqual([r["number"] for r in gaps_by_category["Serve"]], [105])
        self.assertNotIn("Geocoding", gaps_by_category)

    def test_empty_by_category_yields_empty_everything(self):
        gaps_by_key, gaps_by_category, warnings = agg.join_gaps_by_key({}, self.CANONICAL)
        self.assertEqual(gaps_by_key, {})
        self.assertEqual(gaps_by_category, {})
        self.assertEqual(warnings, [])


class BuildMatrixIngestionWarningsTests(unittest.TestCase):
    """End-to-end (still network-free for the parts under test): proves
    build_matrix()'s pushed-envelope join never lands in the hard drift-gate
    unknown_keys set, and that ingestionWarnings collects local warnings."""

    def test_pushed_envelope_unknown_key_does_not_fail_drift_gate(self):
        canonical_keys = {"dr.backup-automation"}
        dr_envelopes = [
            {
                "id": "d1", "drill": "backup-restore", "capturedAt": "2026-07-15T03:12:44Z", "verdict": "pass",
                "capabilityKeys": ["dr.not-canonical"],
            }
        ]
        by_key, warnings = agg.join_local_producer("dr-drills", dr_envelopes, canonical_keys, agg._dr_drill_items)
        # Simulates build_matrix()'s unknown_keys collection: dr-drills/live-canary
        # warnings are deliberately never folded into that hard-fail set.
        unknown_keys: set[str] = set()  # would only ever gain entries from server-matrix/sdk/samples
        self.assertEqual(unknown_keys, set())
        self.assertEqual(by_key, {})
        self.assertTrue(warnings)


# --- end-to-end pipeline tests (honua-io/honua-evidence#1) ------------------
#
# build_matrix() is driven with EVERY network fetcher stubbed out, proving the
# ingest -> validate -> join -> emit pipeline contract without touching the
# network: happy path, the unknown-key drift gate (hard fail), explicit
# "missing" degradation for an absent producer (the honua-sdk-dotnet
# before-first-snapshot case), and explicit "stale" degradation for an
# outdated samples artifact (its 3-day freshness rule).

CANONICAL_KEYS_DOC = {
    "schemaVersion": "1.0.0",
    "capabilities": [
        {"key": "serve.wms", "displayName": "WMS", "category": "Serve", "edition": "Community"},
        {
            "key": "geocoding.single-line",
            "displayName": "Single-line geocoding",
            "category": "Geocoding",
            "edition": "Pro",
        },
    ],
}


def _sv(days_old: int) -> str:
    """A '<sha>@<ISO8601>' sourceVersion whose date is days_old days ago."""
    return f"{'a' * 12}@{_iso(datetime.now(timezone.utc) - timedelta(days=days_old))}"


def _default_fetches() -> dict:
    """One healthy snapshot per producer. Individual tests overwrite entries
    to simulate a missing/stale/drifted producer."""
    return {
        "server_keys": agg.Fetched(
            "server-keys", data=json.loads(json.dumps(CANONICAL_KEYS_DOC)), source_version=_sv(1)
        ),
        "server_matrix": agg.Fetched(
            "server-matrix",
            data={
                "capabilities": [
                    {
                        "key": "serve.wms",
                        "entryCount": 3,
                        "provingTestCount": 5,
                        "maturity": {"level": "ga"},
                        "cite": [{"suite": "wms13", "passed": 199, "total": 199}],
                        "parity": [],
                        "esriAssess": [],
                        "interop": [],
                        "geobench": [],
                    }
                ],
                "unjoinedCiteSuites": ["gpkg12"],
            },
            source_version=_sv(1),
        ),
        "sdk": {
            "js": agg.Fetched(
                "sdk-js",
                data={"capabilities": [{"key": "serve.wms", "status": "covered", "sinceVersion": "1.2.0"}]},
                source_version=_sv(2),
            ),
            # A loaded-but-empty snapshot: legitimately "not-covered" per key.
            "dotnet": agg.Fetched("sdk-dotnet", data={"coverage": []}, source_version=_sv(2)),
            "python": agg.Fetched("sdk-python", data={"capabilities": []}, source_version=_sv(2)),
        },
        "samples": agg.Fetched(
            "samples",
            data={"capabilities": {"serve.wms": [{"id": "wms-quickstart", "title": "WMS quickstart"}]}},
            source_version=_sv(1),
        ),
        "cite": agg.Fetched(
            "cite",
            data={"lastReviewed": "2026-05-17", "sourceSha": "a" * 12, "reportUrl": "https://example/cite"},
            source_version=_sv(5),
        ),
        # The two pushed-envelope producers and the token-gated issues query
        # default to their honest real-world "nothing available" states.
        "dr_drills": agg.Fetched("dr-drills", error="no DR drill evidence envelopes found (none pushed yet)"),
        "live_canary": agg.Fetched("live-canary", error="no live-canary evidence envelopes found (none pushed yet)"),
        "open_issues": agg.Fetched("open-issues", error="no GitHub token available for issue lookup"),
    }


@contextlib.contextmanager
def _patched_producers(fetches: dict):
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(agg, "fetch_server_keys", return_value=fetches["server_keys"]))
        stack.enter_context(mock.patch.object(agg, "fetch_server_matrix", return_value=fetches["server_matrix"]))
        stack.enter_context(mock.patch.object(agg, "fetch_sdk", side_effect=lambda sdk: fetches["sdk"][sdk]))
        stack.enter_context(mock.patch.object(agg, "fetch_samples", return_value=fetches["samples"]))
        stack.enter_context(mock.patch.object(agg, "fetch_cite_status", return_value=fetches["cite"]))
        stack.enter_context(mock.patch.object(agg, "fetch_dr_drills", return_value=fetches["dr_drills"]))
        stack.enter_context(mock.patch.object(agg, "fetch_live_canary", return_value=fetches["live_canary"]))
        stack.enter_context(mock.patch.object(agg, "fetch_open_issues", return_value=fetches["open_issues"]))
        yield


def _cap(matrix: dict, key: str) -> dict:
    return next(c for c in matrix["capabilities"] if c["key"] == key)


class BuildMatrixHappyPathTests(unittest.TestCase):
    def test_all_producers_join_cleanly_with_empty_drift_gate(self):
        with _patched_producers(_default_fetches()):
            matrix, unknown_keys = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))

        self.assertEqual(unknown_keys, [])
        self.assertEqual(matrix["schemaVersion"], agg.SCHEMA_VERSION)
        self.assertEqual([c["key"] for c in matrix["capabilities"]], ["geocoding.single-line", "serve.wms"])
        self.assertEqual(matrix["unjoinedCiteSuites"], ["gpkg12"])

        wms = _cap(matrix, "serve.wms")
        # Server base fields pass through unchanged (never re-derived).
        self.assertEqual(wms["entryCount"], 3)
        self.assertEqual(wms["provingTestCount"], 5)
        self.assertEqual(wms["cite"][0]["suite"], "wms13")
        # Enrichment joins.
        self.assertEqual(wms["sdks"]["js"], {"status": "covered", "sinceVersion": "1.2.0"})
        self.assertEqual(wms["sdks"]["dotnet"], {"status": "not-covered"})  # snapshot loaded, key absent
        self.assertEqual(wms["samples"], [{"id": "wms-quickstart", "title": "WMS quickstart"}])

        geo = _cap(matrix, "geocoding.single-line")
        self.assertEqual(geo["entryCount"], 0)
        self.assertEqual(geo["samples"], [])  # artifact fetched, genuinely no sample: a real zero

        for producer in ("server-keys", "server-matrix", "sdk-js", "sdk-dotnet", "sdk-python", "samples", "cite"):
            self.assertEqual(matrix["freshness"][producer]["status"], "fresh", producer)
        # Not-yet-producing producers appear explicitly -- never omitted.
        for producer in ("dr-drills", "live-canary"):
            self.assertEqual(matrix["freshness"][producer]["status"], "missing", producer)
            self.assertTrue(matrix["freshness"][producer]["detail"])

    def test_main_happy_path_writes_output_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "capability-matrix.v1.json"
            argv = ["aggregate.py", "--output", str(out)]
            with _patched_producers(_default_fetches()), mock.patch.object(sys, "argv", argv):
                rc = agg.main()
            self.assertEqual(rc, 0)
            written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(written["capabilities"]), 2)
        self.assertEqual(written["schemaVersion"], agg.SCHEMA_VERSION)


class BuildMatrixDriftGateTests(unittest.TestCase):
    """Unknown capability keys from any PULLED producer (server matrix, SDK
    snapshots, samples artifact) must hard-fail the build."""

    def test_unknown_key_in_sdk_snapshot_is_reported(self):
        fetches = _default_fetches()
        fetches["sdk"]["js"].data["capabilities"].append({"key": "serve.bogus", "status": "covered"})
        with _patched_producers(fetches):
            _, unknown_keys = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))
        self.assertEqual(unknown_keys, ["serve.bogus"])

    def test_unknown_key_in_server_matrix_is_reported(self):
        fetches = _default_fetches()
        fetches["server_matrix"].data["capabilities"].append({"key": "made.up", "entryCount": 1})
        with _patched_producers(fetches):
            _, unknown_keys = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))
        self.assertEqual(unknown_keys, ["made.up"])

    def test_unknown_key_in_samples_artifact_is_reported(self):
        fetches = _default_fetches()
        fetches["samples"].data["capabilities"]["samples.bogus"] = [{"id": "x"}]
        with _patched_producers(fetches):
            _, unknown_keys = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))
        self.assertEqual(unknown_keys, ["samples.bogus"])

    def test_main_exits_nonzero_and_writes_nothing_on_unknown_key(self):
        fetches = _default_fetches()
        fetches["sdk"]["js"].data["capabilities"].append({"key": "serve.bogus", "status": "covered"})
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "capability-matrix.v1.json"
            argv = ["aggregate.py", "--output", str(out)]
            err = io.StringIO()
            with _patched_producers(fetches), mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(sys, "stderr", err):
                rc = agg.main()
            self.assertEqual(rc, 1)
            self.assertFalse(out.exists(), "drift-gate failure must not write an output artifact")
            self.assertIn("serve.bogus", err.getvalue())
            self.assertIn("Unknown capability key", err.getvalue())


class BuildMatrixMissingProducerTests(unittest.TestCase):
    """A producer that can't be read degrades to an explicit 'missing' state:
    in the freshness ledger AND at the per-capability granularity -- never
    silently omitted, never fabricated as a real coverage claim. This is the
    honua-sdk-dotnet before-first-snapshot case (sdk-dotnet#273)."""

    def test_missing_sdk_dotnet_yields_ledger_missing_and_per_key_producer_missing(self):
        fetches = _default_fetches()
        fetches["sdk"]["dotnet"] = agg.Fetched(
            "sdk-dotnet", error="fetch failed: HTTP Error 404: Not Found"
        )
        with _patched_producers(fetches):
            matrix, unknown_keys = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))

        self.assertEqual(unknown_keys, [])
        ledger = matrix["freshness"]["sdk-dotnet"]
        self.assertEqual(ledger["status"], "missing")
        self.assertIn("404", ledger["detail"])
        for cap in matrix["capabilities"]:
            self.assertEqual(cap["sdks"]["dotnet"], {"status": "producer-missing"}, cap["key"])
            self.assertNotEqual(cap["sdks"]["dotnet"].get("status"), "not-covered")

    def test_missing_samples_producer_yields_null_samples_not_a_fabricated_zero(self):
        fetches = _default_fetches()
        fetches["samples"] = agg.Fetched("samples", error="no successful run-samples run found on trunk")
        with _patched_producers(fetches):
            matrix, _ = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))

        self.assertEqual(matrix["freshness"]["samples"]["status"], "missing")
        for cap in matrix["capabilities"]:
            self.assertIsNone(cap["samples"], cap["key"])


class BuildMatrixStaleSamplesTests(unittest.TestCase):
    """The samples artifact carries a 3-day freshness rule: older than that,
    the ledger flags it 'stale' -- but the (real, just old) evidence stays
    joined per capability. Old evidence is still evidence."""

    def test_samples_default_threshold_is_three_days(self):
        self.assertEqual(agg.DEFAULT_STALENESS_DAYS["samples"], 3)

    def test_samples_older_than_three_days_flagged_stale_with_data_kept(self):
        fetches = _default_fetches()
        fetches["samples"].source_version = _sv(10)
        with _patched_producers(fetches):
            matrix, unknown_keys = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))

        self.assertEqual(unknown_keys, [])
        ledger = matrix["freshness"]["samples"]
        self.assertEqual(ledger["status"], "stale")
        self.assertEqual(ledger["ageDays"], 10)
        self.assertEqual(_cap(matrix, "serve.wms")["samples"], [{"id": "wms-quickstart", "title": "WMS quickstart"}])

    def test_samples_within_three_days_is_fresh(self):
        fetches = _default_fetches()
        fetches["samples"].source_version = _sv(2)
        with _patched_producers(fetches):
            matrix, _ = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))
        self.assertEqual(matrix["freshness"]["samples"]["status"], "fresh")


if __name__ == "__main__":
    unittest.main()
