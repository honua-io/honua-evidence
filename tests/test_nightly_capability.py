"""The Phase-B join must retain and age diagnostic nightly observations."""
import copy
import unittest
from unittest import mock
from test_aggregate import agg, _default_fetches, _patched_producers, _cap


class NightlyCapabilityTests(unittest.TestCase):
    def matrix(self, anchor="2026-11-22T12:00:00Z", state="fresh", run_date="2026-11-21T12:00:00Z", limit=14):
        fetches = _default_fetches()
        source = fetches["server_matrix"].data
        source.update({"nightlyEvidence": {"collectedAt": anchor,
                       "scope": "diagnostic; not candidate release proof", "producers": [{"runId": 123}]},
                       "evidenceFreshness": {"anchor": anchor, "maxAgeDays": limit}})
        stamp = {"state": state, "runDate": run_date, "ageDays": 1}
        row = source["capabilities"][0]
        row["cite"] = [{"suite": "WMS", "passRate": 100, "freshness": copy.deepcopy(stamp)}]
        row["interop"] = [{"clientLane": "qgis", "freshness": copy.deepcopy(stamp), "evidence": {"runId": 123}}]
        row["geobenchResults"] = [{"scenario": "feature_query_load", "metrics": {"okCount": 123},
                                  "freshness": copy.deepcopy(stamp)}]
        original = copy.deepcopy(source)
        with _patched_producers(fetches), mock.patch.object(agg, "now_iso", return_value="2026-11-23T12:00:00Z"):
            matrix, unknown = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))
        self.assertEqual(unknown, [])
        self.assertEqual(source, original, "do not mutate the fetched producer")
        return matrix

    def test_observations_and_provenance_survive_with_wall_clock_age(self):
        matrix = self.matrix()
        row = _cap(matrix, "serve.wms")
        self.assertEqual(matrix["nightlyEvidence"]["producers"], [{"runId": 123}])
        self.assertEqual(matrix["evidenceFreshness"]["anchor"], "2026-11-22T12:00:00Z")
        self.assertEqual(row["geobenchResults"][0]["metrics"]["okCount"], 123)
        for kind in ("cite", "interop", "geobenchResults"):
            self.assertEqual(row[kind][0]["freshness"]["ageDays"], 2)
            self.assertEqual(row[kind][0]["freshness"]["state"], "fresh")

    def test_stale_collection_cannot_be_refreshed_by_fetching_again(self):
        row = _cap(self.matrix("2026-10-01T12:00:00Z"), "serve.wms")
        self.assertEqual(row["cite"][0]["passRate"], 0)
        for kind in ("cite", "interop", "geobenchResults"):
            self.assertEqual(row[kind][0]["freshness"]["state"], "stale")

    def test_future_or_malformed_collection_fails_closed(self):
        for anchor in ("2026-11-24T12:00:00Z", "garbage", "2026-11-22T12:00:00"):
            with self.subTest(anchor=anchor):
                row = _cap(self.matrix(anchor), "serve.wms")
                self.assertEqual(row["cite"][0]["passRate"], 0)
                self.assertNotEqual(row["interop"][0]["freshness"]["state"], "fresh")

    def test_failed_row_is_never_upgraded_by_aggregation(self):
        row = _cap(self.matrix(state="stale"), "serve.wms")
        self.assertEqual(row["cite"][0]["passRate"], 0)
        self.assertEqual(row["interop"][0]["freshness"]["state"], "stale")

    def test_legacy_producer_has_no_invented_nightly_claim(self):
        with _patched_producers(_default_fetches()):
            matrix, _ = agg.build_matrix(staleness=dict(agg.DEFAULT_STALENESS_DAYS))
        self.assertNotIn("nightlyEvidence", matrix)

    def test_old_observation_stays_stale_inside_new_collection(self):
        row = _cap(self.matrix(run_date="2026-10-01T12:00:00Z"), "serve.wms")
        self.assertEqual(row["cite"][0]["passRate"], 0)
        self.assertEqual(row["interop"][0]["freshness"]["state"], "stale")

    def test_invalid_observation_or_limit_fails_closed(self):
        for date in (None, "bad", "2026-11-23T10:00:00Z"):
            with self.subTest(date=date):
                row = _cap(self.matrix(run_date=date), "serve.wms")
                self.assertEqual(row["cite"][0]["passRate"], 0)
        for limit in (True, 0, -1, 30, float("nan")):
            with self.subTest(limit=limit):
                row = _cap(self.matrix(limit=limit), "serve.wms")
                self.assertEqual(row["cite"][0]["passRate"], 0)

    def test_fourteen_day_boundary_is_inclusive(self):
        row = _cap(self.matrix(run_date="2026-11-09T12:00:00Z"), "serve.wms")
        self.assertEqual(row["cite"][0]["passRate"], 100)
        row = _cap(self.matrix(run_date="2026-11-09T11:59:59Z"), "serve.wms")
        self.assertEqual(row["cite"][0]["passRate"], 0)
