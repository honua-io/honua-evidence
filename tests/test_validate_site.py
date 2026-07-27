"""Unit tests for scripts/validate-site.py (honua-io/honua-evidence#2).

Covers the CI-encoded acceptance checks for the evidence index site: the
static a11y/link pass (Lighthouse-equivalent, honua-site validator style)
and the card -> L2 -> receipt walk for the editing capability. Also proves
end-to-end that whatever build-site.py renders from a matrix passes the
validator offline -- so a rendering regression that breaks a link, drops an
aria-label, or loses the CNAME fails unit tests before it fails CI.

Run: python3 -m unittest discover -s tests

No network access is used or required: the walker's --online mode is
exercised only through a stubbed _http_status.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Hyphenated filenames cannot be imported with a plain `import` statement.
vs = _load("validate_site", "validate-site.py")
build_site = _load("build_site", "build-site.py")


GOOD_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Test page</title>
<meta name="description" content="A test page." />
<link rel="stylesheet" href="styles.css" />
</head>
<body>
<h1>Heading</h1>
<h2 id="section">Section</h2>
<p><a href="#section">In-page link</a></p>
</body>
</html>
"""


def _run_quiet(validator, *, online: bool = False) -> int:
    """Run the validator with its console output captured (expected-failure
    tests would otherwise spray ::error:: lines into the test log)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return validator.run(online=online, timeout=1.0)


def _minimal_matrix() -> dict:
    """Smallest matrix build-site.py will render, including the receipt-walk
    capability so the walk is exercised against real rendered output."""
    sdks = {
        "js": {"status": "covered", "entrypoints": ["client.query()"]},
        "dotnet": {"status": "producer-missing"},
        "python": {"status": "not-covered"},
    }
    return {
        "schemaVersion": "2.3.0",
        "generatedAt": "2026-07-27T00:00:00Z",
        "capabilities": [
            {
                "key": vs.DEFAULT_WALK_KEY,
                "displayName": "FeatureServer edits",
                "category": "editing",
                "edition": "Community",
                "entryCount": 3,
                "provingTestCount": 12,
                "maturity": {"stable": 3},
                "sdks": sdks,
                "samples": [],
                "openIssues": {"label": "cap/editing", "count": 0, "keyRefs": [], "categoryRefs": []},
            },
            {
                "key": "query.attribute-filters",
                "displayName": "Attribute filters",
                "category": "query",
                "edition": "Community",
                "entryCount": 0,
                "provingTestCount": 0,
                "sdks": {"js": {"status": "not-covered"}, "dotnet": {"status": "not-covered"}, "python": {"status": "not-covered"}},
                "samples": None,
                "openIssues": {"label": "cap/query", "count": 0, "keyRefs": [], "categoryRefs": []},
            },
        ],
        "freshness": {
            "server-matrix": {"status": "fresh", "sourceVersion": "abc123@2026-07-26T00:00:00Z", "fetchedAt": "2026-07-27T00:00:00Z", "detail": ""},
            "samples": {"status": "missing", "sourceVersion": None, "fetchedAt": "2026-07-27T00:00:00Z", "detail": "no artifact"},
        },
        "ingestionWarnings": [],
    }


class _TempSiteCase(unittest.TestCase):
    """Base: a temp dir with the minimal structural files a site needs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="validate-site-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_site(self) -> Path:
        site = self.tmp / "site"
        (site / "capabilities").mkdir(parents=True)
        (site / "data").mkdir()
        (site / "styles.css").write_text("body{}", encoding="utf-8")
        (site / "data" / "capability-matrix.v1.json").write_text("{}", encoding="utf-8")
        (site / "CNAME").write_text("evidence.honua.io\n", encoding="utf-8")
        (site / ".nojekyll").write_text("", encoding="utf-8")
        (site / "index.html").write_text(GOOD_PAGE, encoding="utf-8")
        (site / "freshness.html").write_text(GOOD_PAGE, encoding="utf-8")
        # Pages one level down reference assets with ../ (as build-site.py does).
        (site / "capabilities" / "some-cap.html").write_text(
            GOOD_PAGE.replace('href="styles.css"', 'href="../styles.css"'), encoding="utf-8"
        )
        return site



class StructureChecksTests(_TempSiteCase):
    def _structure_failures(self, site):
        validator = vs.SiteValidator(site, "unused.key")
        validator.check_structure()
        return validator.failures

    def test_complete_site_has_no_structure_failures(self):
        self.assertEqual(self._structure_failures(self.make_site()), [])

    def test_missing_cname_is_a_failure(self):
        site = self.make_site()
        (site / "CNAME").unlink()
        self.assertTrue(any("CNAME" in f for f in self._structure_failures(site)))

    def test_wrong_cname_domain_is_a_failure(self):
        site = self.make_site()
        (site / "CNAME").write_text("wrong.example.com\n", encoding="utf-8")
        self.assertTrue(any("evidence.honua.io" in f for f in self._structure_failures(site)))

    def test_missing_nojekyll_is_a_failure(self):
        site = self.make_site()
        (site / ".nojekyll").unlink()
        self.assertTrue(any(".nojekyll" in f for f in self._structure_failures(site)))

    def test_empty_capabilities_dir_is_a_failure(self):
        site = self.make_site()
        (site / "capabilities" / "some-cap.html").unlink()
        self.assertTrue(any("no L2 pages" in f for f in self._structure_failures(site)))


class A11yChecksTests(_TempSiteCase):
    def _page_failures(self, page_html: str):
        site = self.make_site()
        (site / "capabilities" / "some-cap.html").write_text(page_html, encoding="utf-8")
        validator = vs.SiteValidator(site, "unused.key")
        validator.check_pages()
        return [f for f in validator.failures if "some-cap" in f]

    def test_good_page_passes(self):
        self.assertEqual(self._page_failures(GOOD_PAGE), [])

    def test_missing_lang_fails(self):
        self.assertTrue(any("lang" in f for f in self._page_failures(GOOD_PAGE.replace(' lang="en"', ""))))

    def test_empty_title_fails(self):
        self.assertTrue(any("<title>" in f for f in self._page_failures(GOOD_PAGE.replace("Test page", " "))))

    def test_missing_viewport_fails(self):
        html = GOOD_PAGE.replace('<meta name="viewport" content="width=device-width, initial-scale=1" />', "")
        self.assertTrue(any("viewport" in f for f in self._page_failures(html)))

    def test_missing_description_fails(self):
        html = GOOD_PAGE.replace('<meta name="description" content="A test page." />', "")
        self.assertTrue(any("description" in f for f in self._page_failures(html)))

    def test_two_h1_fails(self):
        html = GOOD_PAGE.replace("<h1>Heading</h1>", "<h1>One</h1><h1>Two</h1>")
        self.assertTrue(any("exactly one <h1>" in f for f in self._page_failures(html)))

    def test_skipped_heading_level_fails(self):
        html = GOOD_PAGE.replace('<h2 id="section">Section</h2>', '<h4 id="section">Deep</h4>')
        self.assertTrue(any("jumps" in f for f in self._page_failures(html)))

    def test_img_without_alt_fails(self):
        html = GOOD_PAGE.replace("<h1>Heading</h1>", '<h1>Heading</h1><img src="styles.css" />')
        self.assertTrue(any("without alt" in f for f in self._page_failures(html)))

    def test_img_with_empty_alt_passes(self):
        # alt="" is valid for decorative images.
        html = GOOD_PAGE.replace("<h1>Heading</h1>", '<h1>Heading</h1><img src="styles.css" alt="" />')
        self.assertEqual(self._page_failures(html), [])

    def test_link_without_text_fails(self):
        html = GOOD_PAGE.replace('<a href="#section">In-page link</a>', '<a href="#section"></a>')
        self.assertTrue(any("no accessible text" in f for f in self._page_failures(html)))

    def test_link_with_aria_label_only_passes(self):
        html = GOOD_PAGE.replace(
            '<a href="#section">In-page link</a>', '<a href="#section" aria-label="Section link"></a>'
        )
        self.assertEqual(self._page_failures(html), [])

    def test_input_without_label_fails(self):
        html = GOOD_PAGE.replace("<h1>Heading</h1>", '<h1>Heading</h1><input type="search" />')
        self.assertTrue(any("no aria-label" in f for f in self._page_failures(html)))

    def test_input_with_aria_label_passes(self):
        html = GOOD_PAGE.replace("<h1>Heading</h1>", '<h1>Heading</h1><input type="search" aria-label="Filter" />')
        self.assertEqual(self._page_failures(html), [])

    def test_input_with_label_for_passes(self):
        html = GOOD_PAGE.replace(
            "<h1>Heading</h1>", '<h1>Heading</h1><label for="q">Query</label><input id="q" type="search" />'
        )
        self.assertEqual(self._page_failures(html), [])

    def test_table_without_caption_fails(self):
        html = GOOD_PAGE.replace(
            "<h1>Heading</h1>",
            '<h1>Heading</h1><table><thead><tr><th scope="col">A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>',
        )
        self.assertTrue(any("without <caption>" in f for f in self._page_failures(html)))

    def test_th_without_scope_fails(self):
        html = GOOD_PAGE.replace(
            "<h1>Heading</h1>",
            "<h1>Heading</h1><table><caption>c</caption><thead><tr><th>A</th></tr></thead></table>",
        )
        self.assertTrue(any("without scope" in f for f in self._page_failures(html)))


class InternalLinkChecksTests(_TempSiteCase):
    def _link_failures(self, site):
        validator = vs.SiteValidator(site, "unused.key")
        validator.check_pages()
        validator.check_internal_links()
        return [f for f in validator.failures if "link" in f or "fragment" in f]

    def test_valid_relative_links_pass(self):
        site = self.make_site()
        page = GOOD_PAGE.replace(
            '<a href="#section">In-page link</a>',
            '<a href="../index.html">Index</a> <a href="../freshness.html#section">Fresh</a>',
        ).replace('href="styles.css"', 'href="../styles.css"')
        (site / "capabilities" / "some-cap.html").write_text(page, encoding="utf-8")
        self.assertEqual(self._link_failures(site), [])

    def test_broken_internal_link_fails(self):
        site = self.make_site()
        page = GOOD_PAGE.replace('<a href="#section">In-page link</a>', '<a href="missing.html">Gone</a>')
        (site / "index.html").write_text(page, encoding="utf-8")
        self.assertTrue(any("broken internal link missing.html" in f for f in self._link_failures(site)))

    def test_missing_fragment_in_target_page_fails(self):
        site = self.make_site()
        page = GOOD_PAGE.replace(
            '<a href="#section">In-page link</a>', '<a href="freshness.html#nope">Fresh</a>'
        )
        (site / "index.html").write_text(page, encoding="utf-8")
        self.assertTrue(any("missing fragment target freshness.html#nope" in f for f in self._link_failures(site)))

    def test_missing_same_page_fragment_fails(self):
        site = self.make_site()
        page = GOOD_PAGE.replace('href="#section"', 'href="#absent"')
        (site / "index.html").write_text(page, encoding="utf-8")
        self.assertTrue(any("missing fragment target #absent" in f for f in self._link_failures(site)))

    def test_external_links_are_not_resolved_locally(self):
        site = self.make_site()
        page = GOOD_PAGE.replace(
            '<a href="#section">In-page link</a>', '<a href="https://example.com/x">Ext</a>'
        )
        (site / "index.html").write_text(page, encoding="utf-8")
        self.assertEqual(self._link_failures(site), [])

    def test_broken_stylesheet_href_fails(self):
        site = self.make_site()
        (site / "styles.css").unlink()
        failures = self._link_failures(site)
        self.assertTrue(any("styles.css" in f for f in failures))


class BuiltSiteEndToEndTests(_TempSiteCase):
    """Render the minimal matrix through build-site.py's real main() and
    assert the validator passes offline -- the rendering contract itself."""

    def _build(self) -> Path:
        matrix_path = self.tmp / "matrix.json"
        matrix_path.write_text(json.dumps(_minimal_matrix()), encoding="utf-8")
        site = self.tmp / "site"
        argv = ["build-site.py", "--input", str(matrix_path), "--output", str(site)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build_site.main(), 0)
        return site

    def test_built_site_passes_offline_validation_including_receipt_walk(self):
        site = self._build()
        validator = vs.SiteValidator(site, vs.DEFAULT_WALK_KEY)
        self.assertEqual(_run_quiet(validator), 0, validator.failures)

    def test_built_site_online_walk_passes_with_stubbed_200s(self):
        site = self._build()
        validator = vs.SiteValidator(site, vs.DEFAULT_WALK_KEY)
        with mock.patch.object(vs, "_http_status", return_value=200) as stub:
            self.assertEqual(_run_quiet(validator, online=True), 0, validator.failures)
        self.assertGreater(stub.call_count, 0, "online walk must actually check external hops")

    def test_dead_receipt_hop_fails_online_walk(self):
        site = self._build()
        validator = vs.SiteValidator(site, vs.DEFAULT_WALK_KEY)
        with mock.patch.object(vs, "_http_status", return_value=404):
            self.assertEqual(_run_quiet(validator, online=True), 1)
        self.assertTrue(any("receipt walk (online)" in f for f in validator.failures))

    def test_unreachable_receipt_hop_fails_online_walk(self):
        site = self._build()
        validator = vs.SiteValidator(site, vs.DEFAULT_WALK_KEY)
        with mock.patch.object(vs, "_http_status", return_value=None):
            self.assertEqual(_run_quiet(validator, online=True), 1)
        self.assertTrue(any("unreachable" in f for f in validator.failures))

    def test_renamed_walk_capability_fails_loudly(self):
        site = self._build()
        validator = vs.SiteValidator(site, "editing.renamed-away")
        self.assertEqual(_run_quiet(validator), 1)
        self.assertTrue(any("renamed" in f for f in validator.failures))

    def test_missing_index_card_link_fails_walk(self):
        site = self._build()
        slug = vs.DEFAULT_WALK_KEY.replace(".", "-")
        index = site / "index.html"
        html = index.read_text(encoding="utf-8").replace(f"capabilities/{slug}.html", "capabilities/other.html")
        index.write_text(html, encoding="utf-8")
        validator = vs.SiteValidator(site, vs.DEFAULT_WALK_KEY)
        self.assertEqual(_run_quiet(validator), 1)
        self.assertTrue(any("no card linking" in f for f in validator.failures))

    def test_l2_page_without_actions_receipt_fails_walk(self):
        site = self._build()
        slug = vs.DEFAULT_WALK_KEY.replace(".", "-")
        l2 = site / "capabilities" / f"{slug}.html"
        html = l2.read_text(encoding="utf-8").replace("/actions/workflows/run-samples.yml", "/no-ci-here")
        l2.write_text(html, encoding="utf-8")
        validator = vs.SiteValidator(site, vs.DEFAULT_WALK_KEY)
        self.assertEqual(_run_quiet(validator), 1)
        self.assertTrue(any("CI-run" in f for f in validator.failures))

    def test_real_committed_matrix_renders_a_validating_site(self):
        """The committed aggregate must always render into a site that
        passes the offline gate (this is what validate.yml enforces)."""
        matrix_path = REPO_ROOT / "data" / "capability-matrix.v1.json"
        site = self.tmp / "real-site"
        argv = ["build-site.py", "--input", str(matrix_path), "--output", str(site)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build_site.main(), 0)
        validator = vs.SiteValidator(site, vs.DEFAULT_WALK_KEY)
        self.assertEqual(_run_quiet(validator), 0, validator.failures[:10])


class HttpStatusTests(unittest.TestCase):
    def test_http_error_code_is_returned_not_raised(self):
        import urllib.error

        err = urllib.error.HTTPError("https://x", 404, "nf", {}, None)
        with mock.patch.object(vs.urllib.request, "urlopen", side_effect=err):
            self.assertEqual(vs._http_status("https://x", 1.0), 404)

    def test_unreachable_returns_none_after_retry(self):
        import urllib.error

        err = urllib.error.URLError("down")
        with mock.patch.object(vs.urllib.request, "urlopen", side_effect=err) as stub:
            self.assertIsNone(vs._http_status("https://x", 1.0))
        self.assertEqual(stub.call_count, 2)

    def test_transient_failure_then_success_returns_status(self):
        import urllib.error

        ok = mock.MagicMock()
        ok.__enter__.return_value.status = 200
        with mock.patch.object(
            vs.urllib.request, "urlopen", side_effect=[urllib.error.URLError("blip"), ok]
        ):
            self.assertEqual(vs._http_status("https://x", 1.0), 200)


if __name__ == "__main__":
    unittest.main()
