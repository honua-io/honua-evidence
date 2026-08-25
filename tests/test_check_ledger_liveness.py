"""Unit tests for scripts/check-ledger-liveness.py (honua-io/honua-evidence#17).

The watchdog's whole value is that it is loud when -- and only when -- the
aggregator has actually stopped. These tests pin both halves: the ledger-age
verdict, and the parked-run detection that is the leading indicator of the
2026-08-16 `github-pages` deadlock.

No network, no token, no GitHub: the module is pure by design and every test
drives it with a synthetic `gh run list --json ...` payload and a fixed `now`.

Run: python3 -m unittest discover -s tests
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = REPO_ROOT / "scripts" / "check-ledger-liveness.py"

# Hyphenated filename, so import it by path rather than by module name.
_spec = importlib.util.spec_from_file_location("check_ledger_liveness", _SCRIPT)
liveness = importlib.util.module_from_spec(_spec)
sys.modules["check_ledger_liveness"] = liveness
_spec.loader.exec_module(liveness)

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(status: str, age_minutes: float, run_id: int = 1, event: str = "schedule") -> dict:
    return {"databaseId": run_id, "status": status, "conclusion": "",
            "createdAt": _iso(NOW - timedelta(minutes=age_minutes)),
            "event": event, "url": f"https://github.com/o/r/actions/runs/{run_id}"}


class EvaluateLedgerTests(unittest.TestCase):
    def test_recent_generated_at_is_fresh(self):
        verdict = liveness.evaluate_ledger(_iso(NOW - timedelta(hours=2)), NOW)
        self.assertEqual(verdict["status"], "fresh")
        self.assertAlmostEqual(verdict["ageHours"], 2.0, places=2)

    def test_beyond_threshold_is_stale(self):
        verdict = liveness.evaluate_ledger(_iso(NOW - timedelta(hours=31)), NOW)
        self.assertEqual(verdict["status"], "stale")
        self.assertIn("31.0h", verdict["detail"])

    def test_threshold_boundary_is_still_fresh(self):
        """Exactly at maxAgeHours must not flap the alarm."""
        verdict = liveness.evaluate_ledger(
            _iso(NOW - timedelta(hours=liveness.DEFAULT_MAX_AGE_HOURS)), NOW)
        self.assertEqual(verdict["status"], "fresh")

    def test_default_threshold_trips_before_honua_release_36h_gate(self):
        """The producer must speak before honua-release#84's consumer check."""
        self.assertLess(liveness.DEFAULT_MAX_AGE_HOURS, 36)

    def test_real_outage_window_would_have_been_caught(self):
        """The 2026-08-16 freeze: generatedAt stuck at 05:45:56Z."""
        frozen = "2026-08-16T05:45:56Z"
        # 24h into the 42h outage -- long before gate-evidence noticed anything.
        verdict = liveness.evaluate_ledger(
            frozen, datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(verdict["status"], "stale")

    def test_missing_generated_at_is_unreadable_not_fresh(self):
        verdict = liveness.evaluate_ledger(None, NOW)
        self.assertEqual(verdict["status"], "unreadable")
        self.assertIsNone(verdict["ageHours"])

    def test_unparseable_generated_at_is_unreadable(self):
        verdict = liveness.evaluate_ledger("not-a-timestamp", NOW)
        self.assertEqual(verdict["status"], "unreadable")


class ReadGeneratedAtTests(unittest.TestCase):
    def test_reads_committed_matrix_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            path.write_text(json.dumps({"generatedAt": "2026-08-18T04:12:35Z"}), encoding="utf-8")
            self.assertEqual(liveness.read_generated_at(path), "2026-08-18T04:12:35Z")

    def test_missing_file_returns_none(self):
        self.assertIsNone(liveness.read_generated_at(Path("/nonexistent/m.json")))

    def test_the_real_committed_matrix_carries_generated_at(self):
        """Guards the contract honua-release#84's gate reads."""
        self.assertIsNotNone(liveness.read_generated_at(liveness.DEFAULT_MATRIX))


class SelectStuckRunsTests(unittest.TestCase):
    def test_waiting_run_past_threshold_is_stuck_and_cancellable(self):
        stuck = liveness.select_stuck_runs([_run("waiting", 90)], NOW)
        self.assertEqual(len(stuck), 1)
        self.assertTrue(stuck[0]["cancellable"])
        self.assertEqual(stuck[0]["ageMinutes"], 90.0)

    def test_waiting_run_inside_threshold_is_not_stuck(self):
        self.assertEqual(liveness.select_stuck_runs([_run("waiting", 10)], NOW), [])

    def test_queued_run_is_reported_but_not_cancellable(self):
        """Ordinary backpressure gets an alarm, never a cancel."""
        stuck = liveness.select_stuck_runs([_run("queued", 120)], NOW)
        self.assertEqual(len(stuck), 1)
        self.assertFalse(stuck[0]["cancellable"])

    def test_in_progress_and_completed_runs_are_ignored(self):
        runs = [_run("in_progress", 600, 1), _run("completed", 600, 2)]
        self.assertEqual(liveness.select_stuck_runs(runs, NOW), [])

    def test_malformed_entries_do_not_crash_the_watchdog(self):
        runs = ["nonsense", {}, {"status": "waiting"},
                {"status": "waiting", "createdAt": "bogus"}, _run("waiting", 90, 7)]
        stuck = liveness.select_stuck_runs(runs, NOW)
        self.assertEqual([s["id"] for s in stuck], [7])


class BuildReportTests(unittest.TestCase):
    def _report(self, ledger_status: str, stuck: list) -> tuple[bool, str]:
        ledger = liveness.evaluate_ledger(
            _iso(NOW - timedelta(hours=2 if ledger_status == "fresh" else 40)), NOW)
        return liveness.build_report(ledger, stuck, liveness.DEFAULT_STUCK_AFTER_MINUTES)

    def test_fresh_ledger_and_no_parked_runs_is_healthy(self):
        stalled, report = self._report("fresh", [])
        self.assertFalse(stalled)
        self.assertIn("healthy", report)

    def test_stale_ledger_alone_is_stalled(self):
        stalled, report = self._report("stale", [])
        self.assertTrue(stalled)
        self.assertIn("STALLED", report)

    def test_parked_run_alone_is_stalled_even_while_the_ledger_is_still_fresh(self):
        """The leading indicator: catch the deadlock in its first hour, not
        after the ledger has aged out 30h later."""
        stuck = liveness.select_stuck_runs([_run("waiting", 90)], NOW)
        stalled, report = self._report("fresh", stuck)
        self.assertTrue(stalled)
        self.assertIn("pending_deployments", report)

    def test_report_names_the_originating_issue(self):
        stalled, report = self._report("stale", [])
        self.assertIn("honua-io/honua-evidence#17", report)


class MainTests(unittest.TestCase):
    def _matrix(self, tmp: str, generated_at: str) -> Path:
        path = Path(tmp) / "capability-matrix.v1.json"
        path.write_text(json.dumps({"generatedAt": generated_at}), encoding="utf-8")
        return path

    def _runs(self, tmp: str, runs: list) -> Path:
        path = Path(tmp) / "runs.json"
        path.write_text(json.dumps(runs), encoding="utf-8")
        return path

    def _outputs(self, path: Path) -> dict:
        parsed, lines = {}, path.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            key, _, value = lines[index].partition("=")
            if value == "" and "<<__HONUA_EOF__" in lines[index]:
                key = lines[index].split("<<")[0]
                index += 1
                block = []
                while index < len(lines) and lines[index] != "__HONUA_EOF__":
                    block.append(lines[index])
                    index += 1
                parsed[key] = "\n".join(block)
            else:
                parsed[key] = value
            index += 1
        return parsed

    def test_exit_zero_and_outputs_when_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            out.touch()
            code = liveness.main([
                "--matrix", str(self._matrix(tmp, _iso(NOW - timedelta(hours=1)))),
                "--runs", str(self._runs(tmp, [_run("completed", 5)])),
                "--now", _iso(NOW), "--github-output", str(out)])
            self.assertEqual(code, 0)
            outputs = self._outputs(out)
            self.assertEqual(outputs["stalled"], "false")
            self.assertEqual(outputs["cancel_run_ids"], "")

    def test_exit_one_and_cancel_ids_when_a_run_is_parked(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            out.touch()
            code = liveness.main([
                "--matrix", str(self._matrix(tmp, _iso(NOW - timedelta(hours=1)))),
                "--runs", str(self._runs(tmp, [_run("waiting", 200, 31933013020),
                                               _run("queued", 200, 42)])),
                "--now", _iso(NOW), "--github-output", str(out)])
            self.assertEqual(code, 1)
            outputs = self._outputs(out)
            self.assertEqual(outputs["stalled"], "true")
            # The queued run is reported but must NOT be cancelled.
            self.assertEqual(outputs["cancel_run_ids"], "31933013020")

    def test_unreadable_matrix_fails_loudly_rather_than_passing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.txt"
            out.touch()
            code = liveness.main([
                "--matrix", str(Path(tmp) / "absent.json"),
                "--now", _iso(NOW), "--github-output", str(out)])
            self.assertEqual(code, 1)
            self.assertEqual(self._outputs(out)["ledger_status"], "unreadable")


class WorkflowWiringTests(unittest.TestCase):
    """The structural half of the #17 fix lives in YAML, so assert it here --
    a silent revert to one environment-bound job under one non-cancelling
    group is exactly the regression that cost 42 hours."""

    def setUp(self):
        self.text = (REPO_ROOT / ".github" / "workflows" / "aggregate.yml").read_text(encoding="utf-8")
        self.validate_text = (REPO_ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")

    def test_aggregate_job_is_not_bound_to_an_environment(self):
        aggregate_job = self.text.split("  aggregate:", 1)[1].split("\n  deploy:", 1)[0]
        self.assertNotIn("environment:", aggregate_job)

    def test_pages_group_cancels_in_progress(self):
        deploy_job = self.text.split("\n  deploy:", 1)[1]
        self.assertIn("group: aggregate-pages", deploy_job)
        self.assertIn("cancel-in-progress: true", deploy_job)

    def test_ledger_group_is_separate_from_the_pages_group(self):
        aggregate_job = self.text.split("  aggregate:", 1)[1].split("\n  deploy:", 1)[0]
        self.assertIn("group: aggregate-ledger", aggregate_job)

    def test_both_jobs_carry_a_timeout(self):
        self.assertEqual(self.text.count("timeout-minutes:"), 2)

    def test_capability_ledger_commits_before_certification_aggregation(self):
        capability_commit = self.text.index("Commit refreshed capability ledger if changed")
        certification = self.text.index("Aggregate protocol certification observations")
        self.assertLess(capability_commit, certification)

    def test_missing_trunk_certification_requirements_do_not_block_deploy(self):
        self.assertIn('"$REQUIREMENTS_REVISION" == "trunk"', self.text)
        self.assertIn('"$REQUIREMENTS_HTTP_STATUS" == "404"', self.text)
        self.assertIn("awaiting its requirements catalog on honua-release trunk", self.text)
        self.assertIn('"$REQUIREMENTS_HTTP_STATUS" != "200"', self.text)

    def test_pr_gate_executes_aggregator_against_committed_fragments(self):
        self.assertIn("Validate committed protocol certification fragments", self.validate_text)
        self.assertIn("--requirements .validate-cache/protocol-certification-requirements.v1.json", self.validate_text)
        self.assertIn("data/producers/protocol-certification", self.validate_text)

    def test_watchdog_does_not_share_a_group_with_what_it_watches(self):
        watchdog = (REPO_ROOT / ".github" / "workflows" / "ledger-liveness.yml").read_text(
            encoding="utf-8")
        self.assertIn("group: ledger-liveness", watchdog)
        self.assertNotIn("group: aggregate-pages", watchdog)
        self.assertNotIn("group: aggregate-ledger", watchdog)


if __name__ == "__main__":
    unittest.main()
