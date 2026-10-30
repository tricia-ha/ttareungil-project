"""Student-t single-observation bound; no real users/models/API calls."""

from pathlib import Path
from datetime import datetime, timedelta, timezone
import math
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from personalization import (
    PersonalizationPolicy,
    PersonalizationStore,
    _student_t_upper_bound,
)


class StudentTBoundTests(unittest.TestCase):
    def test_formula_matches_known_t_critical_and_not_mean_ci(self):
        upper, d = _student_t_upper_bound([-1.0, 0.0, 1.0], 0.05)
        self.assertAlmostEqual(d["t_critical"], 2.9199855803537256, places=10)
        self.assertEqual(d["degrees_of_freedom"], 2)
        self.assertAlmostEqual(d["sample_sd_log_ratio"], 1.0)
        self.assertAlmostEqual(d["mean_standard_error"], 1 / math.sqrt(3))
        self.assertAlmostEqual(upper, 2.9199855803537256 * math.sqrt(4 / 3), places=10)
        self.assertGreater(upper, d["t_critical"] * d["mean_standard_error"])

    def test_less_than_two_records_or_no_variation_defers_decision(self):
        for scores in ([], [1.0], [1.0] * 25):
            self.assertIsNone(_student_t_upper_bound(scores, 0.05)[0])
        self.assertEqual(
            _student_t_upper_bound([1.0] * 25, 0.05)[1]["status"], "degenerate_variance"
        )

    def test_larger_sample_does_not_collapse_prediction_uncertainty_to_zero(self):
        small, dsmall = _student_t_upper_bound([-1.0, 0.0, 1.0], 0.05)
        large, dlarge = _student_t_upper_bound([-1.0, 0.0, 1.0] * 1000, 0.05)
        self.assertLess(dlarge["t_critical"], dsmall["t_critical"])
        self.assertLess(large, small)
        self.assertGreater(large, 1.0)
        self.assertGreater(
            dlarge["prediction_standard_error"], dlarge["sample_sd_log_ratio"]
        )

    def test_smaller_tail_probability_widens_upper_bound(self):
        self.assertGreater(
            _student_t_upper_bound([-1.0, 0.0, 1.0] * 10, 0.01)[0],
            _student_t_upper_bound([-1.0, 0.0, 1.0] * 10, 0.05)[0],
        )

    def test_invalid_method_rejected_and_default_is_student_t(self):
        self.assertEqual(PersonalizationPolicy().history_bound_method, "student_t")
        with self.assertRaises(ValueError):
            PersonalizationPolicy(history_bound_method="mean_ci")

    def _run_sequence(self, folder, mode="experimental", constant=False):
        policy = (
            None
            if mode is None
            else PersonalizationPolicy(
                history_bound_method="student_t",
                history_miscoverage=0.05,
                history_filter_mode=mode,
            )
        )
        with PersonalizationStore(Path(folder) / "test.sqlite", policy=policy) as store:
            start = datetime.now(timezone.utc) - timedelta(days=10)
            token = store.create_profile(now=start)
            results = []
            for i in range(26):
                t = start + timedelta(hours=i)
                ratio = 1.3 if constant else ((1.2, 1.3, 1.4)[i % 3] if i < 25 else 4.0)
                r = store.start_trip(token, "1", "2", t, 10.0, "test", now=t)
                end = t + timedelta(minutes=10 * ratio)
                results.append(
                    store.finish_trip(token, r["receipt"], "2", end, now=end)
                )
            return results

    def test_store_excludes_one_long_record_and_keeps_previous_factor(self):
        with tempfile.TemporaryDirectory() as folder:
            r = self._run_sequence(folder)
        self.assertFalse(r[-1]["used_for_update"])
        self.assertTrue(r[-1]["auto_filter"]["requires_independent_normal_log_scores"])
        self.assertEqual(r[-1]["auto_filter"]["bound_method"], "student_t")
        self.assertEqual(r[-1]["profile"]["factor"], r[-2]["profile"]["factor"])
        self.assertGreater(r[-1]["auto_filter"]["upper_minutes"], 13.0)

    def test_constant_slow_rider_is_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            r = self._run_sequence(folder, constant=True)
        self.assertTrue(all(v["used_for_update"] for v in r))
        self.assertEqual(r[-1]["auto_filter"]["status"], "degenerate_variance")

    def test_service_defaults_enforce_t_bound_without_env_configuration(self):
        with patch.dict(
            "os.environ",
            {
                "PERSONALIZATION_HISTORY_ALPHA": "",
                "PERSONALIZATION_HISTORY_METHOD": "",
                "PERSONALIZATION_HISTORY_MODE": "",
            },
        ):
            with tempfile.TemporaryDirectory() as folder:
                r = self._run_sequence(folder, mode=None)
                self.assertTrue(all(v["used_for_update"] for v in r[:-1]))
                self.assertFalse(r[-1]["used_for_update"])
                self.assertEqual(r[-1]["auto_filter"]["mode"], "active")
                self.assertEqual(
                    r[-1]["auto_filter"]["nominal_upper_tail_probability"], 0.05
                )
                self.assertEqual(r[-1]["auto_filter"]["bound_method"], "student_t")
                with PersonalizationStore(Path(folder) / "test.sqlite") as store:
                    row = store.db.execute(
                        "SELECT usable,reason FROM personal_observations ORDER BY returned_ts DESC LIMIT 1"
                    ).fetchone()
                    self.assertEqual(row, (0, "personal_history_long_outlier"))
                    self.assertEqual(
                        store.db.execute(
                            "SELECT COUNT(*) FROM personal_observations"
                        ).fetchone()[0],
                        26,
                    )

    def test_active_keeps_consistently_slow_rider_with_variation(self):
        with tempfile.TemporaryDirectory() as folder:
            with PersonalizationStore(
                Path(folder) / "slow.sqlite", policy=PersonalizationPolicy()
            ) as store:
                start = datetime.now(timezone.utc) - timedelta(days=10)
                token = store.create_profile(now=start)
                for i in range(30):
                    t = start + timedelta(hours=i)
                    ratio = (1.8, 2.0, 2.2)[i % 3]
                    receipt = store.start_trip(token, "1", "2", t, 10.0, "test", now=t)[
                        "receipt"
                    ]
                    end = t + timedelta(minutes=10 * ratio)
                    result = store.finish_trip(token, receipt, "2", end, now=end)
                    self.assertTrue(result["used_for_update"], result["auto_filter"])
                self.assertEqual(result["profile"]["usable_records"], 30)

    def test_service_env_can_explicitly_choose_shadow(self):
        with patch.dict(
            "os.environ",
            {
                "PERSONALIZATION_HISTORY_ALPHA": ".01",
                "PERSONALIZATION_HISTORY_METHOD": "student_t",
                "PERSONALIZATION_HISTORY_MODE": "shadow",
            },
        ):
            with tempfile.TemporaryDirectory() as folder:
                r = self._run_sequence(folder, mode=None)
                self.assertTrue(r[-1]["auto_filter"]["suspected"])
                self.assertTrue(r[-1]["used_for_update"])
                self.assertEqual(
                    r[-1]["auto_filter"]["nominal_upper_tail_probability"], 0.01
                )

    def test_t_shadow_records_suspicion_without_exclusion(self):
        with tempfile.TemporaryDirectory() as folder:
            r = self._run_sequence(folder, mode="shadow")
        self.assertTrue(r[-1]["auto_filter"]["suspected"])
        self.assertFalse(r[-1]["auto_filter"]["excluded"])
        self.assertTrue(r[-1]["used_for_update"])


if __name__ == "__main__":
    unittest.main()
