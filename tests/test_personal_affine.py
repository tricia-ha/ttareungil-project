from pathlib import Path
import math, sys, unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from personal_affine import fit_affine, personalized_minutes
from personalization import PersonalizationPolicy, PersonalizedBikeProvider


class AffineTests(unittest.TestCase):
    def test_cold_start_and_positive_monotonic_extrapolation(self):
        policy = PersonalizationPolicy()
        cold = fit_affine([], policy)
        self.assertEqual(personalized_minutes(3.0, cold), 3.0)
        rows = [(4 + i % 37, 0.85 * (4 + i % 37) - 2) for i in range(300)]
        fit = fit_affine(rows, policy)
        self.assertAlmostEqual(fit["factor"], 0.85, delta=0.04)
        self.assertAlmostEqual(fit["offset_min"], -2, delta=0.5)
        values = [
            personalized_minutes(x, fit) for x in [0.01, 0.1, 1, 2, 4, 10, 40, 100]
        ]
        self.assertTrue(all(math.isfinite(v) and v > 0 for v in values))
        self.assertEqual(values, sorted(values))

    def test_constant_duration_does_not_invent_a_speed_difference(self):
        p = PersonalizationPolicy()
        one = fit_affine([(10, 7)], p)
        many = fit_affine([(10, 7)] * 100, p)
        self.assertEqual(one["factor"], 1.0)
        self.assertEqual(many["factor"], 1.0)
        self.assertLess(abs(one["offset_min"]), abs(many["offset_min"]))
        self.assertLess(abs(many["offset_min"] + 3), 0.3)

    def test_outlier_influence_is_bounded(self):
        p = PersonalizationPolicy()
        ordinary = [(float(i), float(i) - 2) for i in range(5, 35)]
        clean = fit_affine(ordinary, p)
        dirty = fit_affine(ordinary + [(10.0, 1000.0)], p)
        self.assertLess(
            abs(personalized_minutes(15, dirty) - personalized_minutes(15, clean)), 1.0
        )

    def test_provider_applies_offset_after_base_cache_only(self):
        class Base:
            eta_quantile = "p50"

            def __call__(self, *args):
                return {
                    "minutes": 10.0,
                    "base_eta": {"p50_min": 10.0, "p80_min": 15.0},
                    "user_factor": 1.0,
                }

        result = PersonalizedBikeProvider(Base(), {"factor": 0.9, "offset_min": -2.0})(
            None, None
        )
        self.assertEqual(result["minutes"], 7.0)
        self.assertEqual(result["base_eta"]["p50_min"], 10.0)
        self.assertEqual(result["base_eta"]["p80_min"], 15.0)
        self.assertEqual(result["user_offset_min"], -2.0)


if __name__ == "__main__":
    unittest.main()
