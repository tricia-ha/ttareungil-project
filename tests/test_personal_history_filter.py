"""Temporal synthetic checks; no production users, model inference or API calls."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from personal_affine import personalized_minutes
from personalization import (
    PersonalizationStore,
    PersonalizationPolicy,
    _rank_upper_bound,
)


class PersonalHistoryFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.sqlite"
        self.experimental = PersonalizationPolicy(
            history_filter_mode="experimental",
            history_miscoverage=0.05,
            history_bound_method="rank",
        )
        self.store = PersonalizationStore(self.path, policy=self.experimental)
        self.start = datetime.now(timezone.utc) - timedelta(days=30)
        self.token = self.store.create_profile(now=self.start)
        self.index = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, ratio=1.0, base=10.0, context="test", **flags):
        t = self.start + timedelta(hours=3 * self.index)
        self.index += 1
        r = self.store.start_trip(self.token, "1", "2", t, base, context, now=t)
        end = t + timedelta(minutes=base * ratio)
        result = self.store.finish_trip(
            self.token, r["receipt"], "2", end, now=end, **flags
        )
        return result, r["receipt"], end

    def test_consistently_slow_rider_is_not_excluded(self):
        results = [
            self.record(1.3, base=(5.0, 10.0, 20.0, 30.0)[i % 4])[0] for i in range(60)
        ]
        self.assertTrue(all(r["used_for_update"] for r in results))
        self.assertGreater(personalized_minutes(10, results[-1]["profile"]), 10.0)
        self.assertEqual(results[-1]["profile"]["usable_records"], 60)

    def test_very_slow_rider_above_global_factor_cap_is_not_filtered(self):
        results = [self.record(2.5)[0] for _ in range(35)]
        self.assertTrue(all(r["used_for_update"] for r in results))

    def test_single_long_record_does_not_change_factor_and_retry_is_stable(self):
        for _ in range(25):
            self.record(1.3)
        before = self.store.profile(self.token, "test")
        result, receipt, end = self.record(4.0)
        self.assertFalse(result["used_for_update"])
        self.assertEqual(result["exclusion_reason"], "personal_history_long_outlier")
        self.assertEqual(result["profile"]["factor"], before["factor"])
        self.assertEqual(result["auto_filter"]["history_records"], 25)
        for _ in range(3):
            self.record(1.3)
        duplicate = self.store.finish_trip(self.token, receipt, "2", end, now=end)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["auto_filter"], result["auto_filter"])
        row = self.store.db.execute(
            "SELECT actual_minutes,usable,auto_filter_json FROM personal_observations WHERE receipt=?",
            (receipt,),
        ).fetchone()
        self.assertEqual(row[0:2], (40.0, 0))
        self.assertTrue(json.loads(row[2])["excluded"])

    def test_insufficient_history_never_hard_rejects(self):
        for _ in range(5):
            self.record()
        result, _, _ = self.record(4.0)
        self.assertTrue(result["used_for_update"])
        self.assertEqual(result["auto_filter"]["status"], "insufficient_history")

    def test_normal_long_trip_is_retained_without_a_fixed_minute_allowance(self):
        for _ in range(25):
            self.record(1.3)
        self.assertTrue(self.record(1.3, base=60.0)[0]["used_for_update"])
        self.assertFalse(self.record(3.0, base=1.0)[0]["used_for_update"])

    def test_reference_is_scoped_to_person_and_model(self):
        for _ in range(25):
            self.record()
        self.assertEqual(
            self.record(4.0, context="new-model")[0]["auto_filter"]["history_records"],
            0,
        )
        self.token = self.store.create_profile(now=self.start)
        self.assertEqual(self.record(4.0)[0]["auto_filter"]["history_records"], 0)

    def test_future_history_cannot_reject_an_earlier_trip(self):
        self.index = 50
        for _ in range(25):
            self.record()
        self.index = 0
        result, _, _ = self.record(4.0)
        self.assertTrue(result["used_for_update"])
        self.assertEqual(result["auto_filter"]["history_records"], 0)

    def test_manual_exclusions_do_not_contaminate_reference(self):
        for _ in range(25):
            self.record(4.0, leisure=True)
        result, _, _ = self.record()
        self.assertEqual(result["auto_filter"]["history_records"], 0)

    def test_sustained_change_is_not_permanently_locked_out(self):
        for _ in range(60):
            self.record()
        changes = [self.record(3.0)[0] for _ in range(65)]
        self.assertFalse(changes[0]["used_for_update"])
        self.assertTrue(all(r["used_for_update"] for r in changes[-10:]))
        self.assertEqual(changes[-1]["auto_filter"]["history_records"], 124)

    def test_continuous_drift_can_exceed_the_bound_and_is_not_claimed_safe(self):
        results = [self.record(1.0 + i / 100.0)[0] for i in range(60)]
        self.assertTrue(any(not r["used_for_update"] for r in results))
        self.assertFalse(results[-1]["auto_filter"]["real_coverage_validated"])

    def test_old_database_is_migrated_without_reclassifying_history(self):
        self.record(1.3)
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute(
                    "ALTER TABLE personal_observations DROP COLUMN auto_filter_json"
                )
        self.store = PersonalizationStore(self.path, policy=self.experimental)
        self.assertEqual(self.store.profile(self.token, "test")["usable_records"], 1)
        columns = {
            r[1]
            for r in self.store.db.execute("PRAGMA table_info(personal_observations)")
        }
        self.assertIn("auto_filter_json", columns)

    def test_policy_validation(self):
        for kwargs in (
            {"history_miscoverage": 0.0},
            {"history_miscoverage": 0.5},
            {"history_miscoverage": float("nan")},
            {"history_miscoverage": -1.0},
        ):
            with self.assertRaises(ValueError):
                PersonalizationPolicy(**kwargs)

    def test_default_shadow_does_not_exclude_an_unvalidated_candidate(self):
        for _ in range(25):
            self.record(1.3)
        self.store.close()
        self.store = PersonalizationStore(
            self.path,
            policy=PersonalizationPolicy(
                history_filter_mode="shadow",
                history_miscoverage=0.05,
                history_bound_method="rank",
            ),
        )
        result, _, _ = self.record(4.0)
        self.assertEqual(result["auto_filter"]["mode"], "shadow")
        self.assertTrue(result["auto_filter"]["suspected"])
        self.assertFalse(result["auto_filter"]["excluded"])
        self.assertTrue(result["used_for_update"])
        self.assertIsNone(result["exclusion_reason"])
        self.assertEqual(result["profile"]["usable_records"], 26)

    def test_default_restores_old_unvalidated_exclusions_and_keeps_audit(self):
        for _ in range(25):
            self.record(1.3)
        result, receipt, _ = self.record(4.0)
        self.assertFalse(result["used_for_update"])
        self.record(4.0, leisure=True)
        self.store.close()
        self.store = PersonalizationStore(
            self.path, policy=PersonalizationPolicy(history_filter_mode="shadow")
        )
        self.assertEqual(self.store.profile(self.token, "test")["usable_records"], 26)
        row = self.store.db.execute(
            "SELECT usable,reason,auto_filter_json FROM personal_observations WHERE receipt=?",
            (receipt,),
        ).fetchone()
        self.assertEqual(row[:2], (1, None))
        self.assertTrue(json.loads(row[2])["previous_decision"]["excluded"])
        self.assertEqual(
            self.store.db.execute(
                "SELECT COUNT(*) FROM personal_observations WHERE reason='exercise_or_leisure' AND usable=0"
            ).fetchone()[0],
            1,
        )

    def test_default_mode_is_active_with_explicit_nominal_risk(self):
        self.assertEqual(PersonalizationPolicy().history_filter_mode, "active")
        self.assertEqual(PersonalizationPolicy().history_miscoverage, 0.05)
        with self.assertRaises(ValueError):
            PersonalizationPolicy(history_filter_mode="validated")

    def test_explicit_none_risk_level_defers_decision(self):
        self.store.close()
        self.store = PersonalizationStore(
            self.path, policy=PersonalizationPolicy(history_miscoverage=None)
        )
        for _ in range(25):
            self.record(1.3)
        result, _, _ = self.record(4.0)
        self.assertTrue(result["used_for_update"])
        self.assertFalse(result["auto_filter"]["suspected"])
        self.assertEqual(result["auto_filter"]["status"], "risk_level_not_configured")

    def test_finite_sample_rank_determines_data_requirement(self):
        self.assertEqual(_rank_upper_bound(list(range(18)), 0.05), (19, None))
        self.assertEqual(_rank_upper_bound(list(range(19)), 0.05), (19, 18))
        self.assertEqual(_rank_upper_bound(list(range(98)), 0.01), (99, None))
        self.assertEqual(_rank_upper_bound(list(range(99)), 0.01), (99, 98))
        # Corrected quantile, not interpolation or a mean confidence interval.
        self.assertEqual(_rank_upper_bound(list(range(99)), 0.05), (95, 94))

    def test_rank_ties_are_kept_and_smaller_risk_widens_the_bound(self):
        scores = list(range(99))
        self.assertGreater(
            _rank_upper_bound(scores, 0.01)[1], _rank_upper_bound(scores, 0.05)[1]
        )
        for _ in range(25):
            self.record(1.3)
        self.assertTrue(self.record(1.3)[0]["used_for_update"])
        self.assertTrue(self.record(0.7)[0]["used_for_update"])


if __name__ == "__main__":
    unittest.main()
