#!/usr/bin/env python3
"""Validate the built static evidence site (honua-io/honua-evidence#2).

Encodes the two issue-#2 acceptance criteria that were previously only
checked by hand, as a repeatable CI gate over ``site/`` (run
scripts/build-site.py first):

1. Receipt walk (card -> L2 -> raw artifact): starting from the index card
   for the editing capability (canonical key ``editing.featureserver-edits``
   -- the issue text's "editing.feature-edits" predates the canonical
   vocabulary), assert the index row links to the L2 page, the L2 page has a
   populated "Raw receipts" section, and (with ``--online``) that every
   external receipt hop on that page answers < 400 -- i.e. every hop lands
   on a real artifact, not a dead link.

2. Lighthouse-equivalent static a11y + link pass, mirroring honua-site's
   validator style (validate-internal-links.mjs et al. -- honua-site gates
   with stdlib-style static validators, not a hosted Lighthouse run):
   - every internal href/src resolves to a file in the built site, and
     fragment links resolve to an id/name in the target page;
   - every page has <html lang>, a non-empty <title>, a meta viewport, a
     meta description, and exactly one <h1>, with no skipped heading levels;
   - every <img> has an alt attribute, every <a> has accessible text (or an
     aria-label), form controls carry an aria-label or an associated
     <label>, and data tables have a <caption> and <th scope>;
   - the Pages domain files (CNAME = evidence.honua.io, .nojekyll) are
     present so a deploy can never silently drop the custom domain.

Zero-dependency: Python standard library only. Offline by default --
``--online`` additionally HTTP-checks the external receipt-walk hops and is
meant for CI (validate.yml), where a dead upstream artifact should fail the
gate rather than ship as a broken "receipt".

Exit code 0 = pass, 1 = one or more failures (each printed as
``::error::``-style lines so GitHub Actions annotates them).
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE_DIR = REPO_ROOT / "site"

# The issue-#2 acceptance walk names "editing.feature-edits"; the canonical
# vocabulary (honua-server capability-keys.v1.json) spells it
# "editing.featureserver-edits". If the canonical key is ever renamed, this
# validator fails loudly rather than silently walking nothing.
DEFAULT_WALK_KEY = "editing.featureserver-edits"

EXPECTED_CNAME = "evidence.honua.io"

# Tags that never need closing and never carry link-relevant text.
_VOID_TAGS = {"meta", "link", "img", "br", "hr", "input", "source", "wbr"}


class PageScan(HTMLParser):
    """Single-pass structural scan of one rendered page.

    Collects exactly what the checks below need -- links with their visible
    text, ids, headings, images, form controls, tables -- without any
    third-party HTML library.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang: str | None = None
        self.title_text = ""
        self.has_viewport = False
        self.has_description = False
        self.headings: list[int] = []  # e.g. [1, 2, 2, 3]
        self.ids: set[str] = set()
        self.links: list[dict[str, str]] = []  # {href, text, aria_label}
        self.srcs: list[str] = []
        self.imgs_missing_alt = 0
        self.unlabeled_controls: list[str] = []
        self.tables = 0
        self.tables_with_caption = 0
        self.th_total = 0
        self.th_with_scope = 0
        self._label_for: set[str] = set()
        self._pending_controls: list[tuple[str, str]] = []  # (tag desc, id)
        self._open_a: dict[str, str] | None = None
        self._in_title = False
        self._in_caption_table_depth = 0
        self._table_depth = 0

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _attr(attrs: list[tuple[str, str | None]], name: str) -> str | None:
        for k, v in attrs:
            if k == name:
                return v if v is not None else ""
        return None

    # -- parser hooks ----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        get = lambda name: self._attr(attrs, name)  # noqa: E731
        el_id = get("id")
        if el_id:
            self.ids.add(el_id)
        name_attr = get("name")
        if name_attr and tag == "a":
            self.ids.add(name_attr)

        if tag == "html":
            self.lang = get("lang")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            if (get("name") or "").lower() == "viewport":
                self.has_viewport = True
            if (get("name") or "").lower() == "description" and (get("content") or "").strip():
                self.has_description = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.headings.append(int(tag[1]))
        elif tag == "a":
            href = get("href")
            self._open_a = {"href": href or "", "text": "", "aria_label": get("aria-label") or ""}
        elif tag == "img":
            src = get("src")
            if src:
                self.srcs.append(src)
            if get("alt") is None:
                self.imgs_missing_alt += 1
        elif tag == "label":
            for_id = get("for")
            if for_id:
                self._label_for.add(for_id)
        elif tag in {"input", "select", "textarea"}:
            if tag == "input" and (get("type") or "").lower() in {"hidden", "submit", "button"}:
                pass
            elif get("aria-label") or get("aria-labelledby") or get("title"):
                pass
            else:
                self._pending_controls.append((f"<{tag}>", el_id or ""))
        elif tag == "table":
            self.tables += 1
            self._table_depth += 1
        elif tag == "caption" and self._table_depth > 0:
            self.tables_with_caption += 1
        elif tag == "th":
            self.th_total += 1
            if get("scope"):
                self.th_with_scope += 1
        elif tag == "script" or tag == "link":
            src = get("src") if tag == "script" else get("href")
            if tag == "link" and (get("rel") or "") not in {"stylesheet", "icon"}:
                src = None
            if src:
                self.srcs.append(src)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._open_a is not None:
            self.links.append(self._open_a)
            self._open_a = None
        elif tag == "title":
            self._in_title = False
        elif tag == "table" and self._table_depth > 0:
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_text += data
        if self._open_a is not None:
            self._open_a["text"] += data

    # -- results ---------------------------------------------------------
    def finish(self) -> None:
        self.close()
        # A control is labeled if a <label for=...> anywhere on the page
        # points at it (labels may appear after the control in source).
        self.unlabeled_controls = [
            desc for desc, el_id in self._pending_controls if not (el_id and el_id in self._label_for)
        ]


def is_external(href: str) -> bool:
    return href.startswith(("http://", "https://", "//", "mailto:", "tel:", "data:", "javascript:"))


def scan_page(path: Path) -> PageScan:
    scan = PageScan()
    scan.feed(path.read_text(encoding="utf-8"))
    scan.finish()
    return scan


class SiteValidator:
    def __init__(self, site_dir: Path, walk_key: str) -> None:
        self.site_dir = site_dir
        self.walk_key = walk_key
        self.failures: list[str] = []
        self.pages: dict[Path, PageScan] = {}

    def fail(self, message: str) -> None:
        self.failures.append(message)

    # -- structure ---------------------------------------------------------
    def check_structure(self) -> None:
        for rel in ("index.html", "freshness.html", "styles.css", "data/capability-matrix.v1.json", ".nojekyll"):
            if not (self.site_dir / rel).exists():
                self.fail(f"site structure: missing {rel}")
        cname = self.site_dir / "CNAME"
        if not cname.exists():
            self.fail("site structure: missing CNAME (custom domain evidence.honua.io would drop on deploy)")
        elif cname.read_text(encoding="utf-8").strip() != EXPECTED_CNAME:
            self.fail(f"site structure: CNAME is {cname.read_text(encoding='utf-8').strip()!r}, expected {EXPECTED_CNAME!r}")
        capabilities_dir = self.site_dir / "capabilities"
        if not capabilities_dir.exists() or not list(capabilities_dir.glob("*.html")):
            self.fail("site structure: capabilities/ has no L2 pages")

    # -- per-page a11y ------------------------------------------------------
    def check_pages(self) -> None:
        for path in sorted(self.site_dir.rglob("*.html")):
            rel = path.relative_to(self.site_dir)
            scan = scan_page(path)
            self.pages[path] = scan
            if not scan.lang:
                self.fail(f"{rel}: <html> missing lang attribute")
            if not scan.title_text.strip():
                self.fail(f"{rel}: empty or missing <title>")
            if not scan.has_viewport:
                self.fail(f"{rel}: missing meta viewport")
            if not scan.has_description:
                self.fail(f"{rel}: missing meta description")
            h1s = [h for h in scan.headings if h == 1]
            if len(h1s) != 1:
                self.fail(f"{rel}: expected exactly one <h1>, found {len(h1s)}")
            prev = 0
            for level in scan.headings:
                if prev and level > prev + 1:
                    self.fail(f"{rel}: heading level jumps from h{prev} to h{level}")
                prev = level
            if scan.imgs_missing_alt:
                self.fail(f"{rel}: {scan.imgs_missing_alt} <img> without alt")
            for desc in scan.unlabeled_controls:
                self.fail(f"{rel}: {desc} has no aria-label, aria-labelledby, title, or <label for>")
            for link in scan.links:
                if not link["text"].strip() and not link["aria_label"].strip():
                    self.fail(f"{rel}: link to {link['href'] or '(no href)'} has no accessible text")
            if scan.tables and scan.tables_with_caption < scan.tables:
                self.fail(f"{rel}: {scan.tables - scan.tables_with_caption} table(s) without <caption>")
            if scan.th_total and scan.th_with_scope < scan.th_total:
                self.fail(f"{rel}: {scan.th_total - scan.th_with_scope} <th> without scope")

    # -- internal links -----------------------------------------------------
    def _resolve(self, page: Path, target: str) -> Path | None:
        raw = unquote(target)
        if raw.startswith("/"):
            candidate = (self.site_dir / raw.lstrip("/")).resolve()
        else:
            candidate = (page.parent / raw).resolve()
        try:
            candidate.relative_to(self.site_dir.resolve())
        except ValueError:
            return None
        return candidate

    def check_internal_links(self) -> None:
        for path, scan in self.pages.items():
            rel = path.relative_to(self.site_dir)
            refs = [link["href"] for link in scan.links] + scan.srcs
            for href in refs:
                href = href.strip()
                if not href or is_external(href):
                    continue
                split = urlsplit(href)
                target_path, fragment = split.path, split.fragment
                if not target_path:
                    # pure fragment link -- must exist on this page
                    if fragment and fragment not in scan.ids:
                        self.fail(f"{rel}: missing fragment target #{fragment}")
                    continue
                candidate = self._resolve(path, target_path)
                if candidate is None or not candidate.exists():
                    self.fail(f"{rel}: broken internal link {href}")
                    continue
                if fragment and candidate.suffix == ".html":
                    target_scan = self.pages.get(candidate)
                    if target_scan is None and candidate.exists():
                        target_scan = scan_page(candidate)
                    if target_scan is not None and fragment not in target_scan.ids:
                        self.fail(f"{rel}: missing fragment target {href}")

    # -- receipt walk (issue #2 acceptance criterion) -------------------------
    def check_receipt_walk(self, online: bool, timeout: float) -> None:
        slug = self.walk_key.replace(".", "-")
        l2_rel = Path("capabilities") / f"{slug}.html"
        l2_path = self.site_dir / l2_rel

        index_path = self.site_dir / "index.html"
        index_scan = self.pages.get(index_path)
        if index_scan is None:
            return  # structure check already failed
        card_hrefs = {link["href"] for link in index_scan.links}
        if f"capabilities/{slug}.html" not in card_hrefs:
            self.fail(f"receipt walk: index.html has no card linking to capabilities/{slug}.html ({self.walk_key})")
        if not l2_path.exists():
            self.fail(f"receipt walk: L2 page {l2_rel} does not exist -- was {self.walk_key} renamed in the canonical vocabulary?")
            return

        l2_scan = self.pages.get(l2_path) or scan_page(l2_path)
        page_text = l2_path.read_text(encoding="utf-8")
        if "Raw receipts" not in page_text:
            self.fail(f"receipt walk: {l2_rel} has no 'Raw receipts' section")

        external = [link["href"] for link in l2_scan.links if link["href"].startswith(("http://", "https://"))]
        if len(external) < 3:
            self.fail(f"receipt walk: {l2_rel} has only {len(external)} external receipt link(s); expected the raw-receipts chain")
        if not any("/actions" in href for href in external):
            self.fail(f"receipt walk: {l2_rel} has no CI-run (Actions) receipt link -- the card -> L2 -> CI-run chain is broken")

        if online:
            for url in sorted(set(external)):
                status = _http_status(url, timeout)
                if status is None or status >= 400:
                    self.fail(f"receipt walk (online): {url} -> {status if status is not None else 'unreachable'}")

    def run(self, online: bool, timeout: float) -> int:
        if not self.site_dir.exists():
            print(f"::error::{self.site_dir} does not exist; run scripts/build-site.py first.")
            return 1
        self.check_structure()
        self.check_pages()
        self.check_internal_links()
        self.check_receipt_walk(online, timeout)
        if self.failures:
            for failure in self.failures:
                print(f"::error::{failure}")
            print(f"validate-site: {len(self.failures)} failure(s) across {len(self.pages)} page(s).")
            return 1
        mode = "online receipt-walk" if online else "offline"
        print(f"validate-site: OK -- {len(self.pages)} pages, links + a11y + receipt walk ({mode}).")
        return 0


def _http_status(url: str, timeout: float) -> int | None:
    """Return the HTTP status for ``url`` (following redirects), or None if
    unreachable. GETs with a real User-Agent; retries once on transient
    failure so a single network blip does not fail the gate."""
    request = urllib.request.Request(url, headers={"User-Agent": "honua-evidence-validate-site/1.0"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https URLs from our own pages)
                return response.status
        except urllib.error.HTTPError as err:
            return err.code
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=DEFAULT_SITE_DIR, help="built site directory (default: site/)")
    parser.add_argument("--walk-key", default=DEFAULT_WALK_KEY, help="capability key for the end-to-end receipt walk")
    parser.add_argument("--online", action="store_true", help="also HTTP-check the external receipt-walk hops")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout for --online checks")
    args = parser.parse_args()
    return SiteValidator(args.site, args.walk_key).run(online=args.online, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
