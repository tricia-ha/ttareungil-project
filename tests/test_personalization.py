"""Synthetic data only, temporary databases; not a claim of personalization gains."""

from pathlib import Path
import sys, tempfile, unittest, math
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from personal_affine import personalized_minutes
from personalization import (
    PersonalizationStore,
    PersonalizationPolicy,
    PersonalizedBikeProvider,
    baseline_context,
)
from multimodal import V521BikeProvider, Router, Network, Config
from multimodal_cache import CachedBikeProvider

NOW = datetime.now(timezone.utc) - timedelta(days=2)


class FakeETA:
    def __init__(self):
        self.calls = 0

    def predict(self, **kwargs):
        self.calls += 1
        return {
            "eta": {"p50_min": 10.0, "p80_min": 15.0, "p90_min": 20.0},
            "route": {},
            "model": {},
        }

    def predict_batch(self, rows):
        return [self.predict(**row) for row in rows]


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PersonalizationStore(Path(self.tmp.name) / "personal.sqlite")
        self.token = self.store.create_profile(now=NOW)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, ratio=1.0, index=0, **kwargs):
        start = NOW + timedelta(hours=index)
        receipt = self.store.start_trip(
            self.token,
            "1",
            "2",
            start,
            10.0,
            "test-model",
            applied_factor=kwargs.pop("applied_factor", 1.0),
            now=start,
        )
        end = start + timedelta(minutes=10 * ratio)
        return self.store.finish_trip(
            self.token,
            receipt["receipt"],
            kwargs.pop("end_id", "2"),
            end,
            now=end,
            **kwargs,
        )

    def test_cold_start_exactly_normal_and_no_plaintext_token(self):
        p = self.store.profile(self.token, "test-model")
        self.assertEqual(p["factor"], 1.0)
        self.assertTrue(p["cold_start"])
        self.assertFalse(p["calibrated"])
        self.assertNotEqual(
            self.store.db.execute("SELECT owner FROM personal_profiles").fetchone()[0],
            self.token,
        )

    def test_few_records_are_shrunk_and_many_stable_records_follow_user(self):
        first = personalized_minutes(10, self.record(0.8)["profile"]) / 10
        for i in range(1, 25):
            self.record(0.8, i)
        later = self.store.profile(self.token, "test-model")
        self.assertTrue(0.8 < personalized_minutes(10, later) / 10 < first < 1.0)
        self.assertEqual(later["usable_records"], 25)

    def test_noisy_records_get_less_influence(self):
        for i in range(20):
            self.record(0.8, i)
        stable = (
            personalized_minutes(10, self.store.profile(self.token, "test-model")) / 10
        )
        other = self.store.create_profile(now=NOW)
        self.token = other
        for i in range(20):
            self.record(0.6 if i % 2 else 1.0666666666667, i)
        noisy = self.store.profile(other, "test-model")
        self.assertGreater(personalized_minutes(10, noisy) / 10, stable)
        self.assertLess(noisy["shrinkage_weight"], 20 / 30)

    def test_single_extreme_record_cannot_cause_large_jump(self):
        p = self.record(100.0)["profile"]
        self.assertLess(p["factor"], 1.1)
        self.assertEqual(p["bounded_influence_records"], 1)

    def test_minute_mae_weights_are_not_equal_relative_error_weights(self):
        for i, (base, actual) in enumerate(((1.0, 0.5), (10.0, 12.0))):
            t = NOW + timedelta(hours=i)
            q = self.store.start_trip(
                self.token, "1", "2", t, base, "test-model", now=t
            )
            end = t + timedelta(minutes=actual)
            self.store.finish_trip(self.token, q["receipt"], "2", end, now=end)
        self.assertGreater(self.store.profile(self.token, "test-model")["factor"], 1.0)

    def test_base_not_already_personalized_time_is_used(self):
        self.record(0.9, applied_factor=0.9)
        self.assertLess(
            personalized_minutes(10, self.store.profile(self.token, "test-model")), 10.0
        )

    def test_pause_other_station_and_other_route_do_not_update(self):
        for i, kwargs in enumerate(
            ({"paused": True}, {"end_id": "3"}, {"followed_route": False})
        ):
            r = self.record(2.0, i, **kwargs)
            self.assertFalse(r["used_for_update"])
        p = self.store.profile(self.token, "test-model")
        self.assertEqual(p["usable_records"], 0)
        self.assertEqual(p["factor"], 1.0)

    def test_future_records_and_different_model_are_not_used(self):
        self.record(0.8)
        self.assertEqual(
            self.store.profile(self.token, "test-model", as_of=NOW)["factor"], 1.0
        )
        self.assertEqual(
            self.store.profile(self.token, "different-model")["factor"], 1.0
        )

    def test_reported_exercise_is_stored_without_changing_personal_factor(self):
        before = self.record(0.8)["profile"]
        result = self.record(3.0, 1, leisure=True)
        self.assertFalse(result["used_for_update"])
        self.assertEqual(result["exclusion_reason"], "exercise_or_leisure")
        self.assertEqual(result["profile"]["usable_records"], 1)
        self.assertEqual(result["profile"]["factor"], before["factor"])
        saved = self.store.db.execute(
            "SELECT actual_minutes,usable,reason FROM personal_observations WHERE usable=0"
        ).fetchone()
        self.assertEqual(saved, (30.0, 0, "exercise_or_leisure"))

    def test_receipt_ownership_duplicate_and_deletion(self):
        trip = self.store.start_trip(
            self.token, "001", "002", NOW, 10.0, "test-model", now=NOW
        )
        other = self.store.create_profile(now=NOW)
        end = NOW + timedelta(minutes=8)
        with self.assertRaises(ValueError):
            self.store.finish_trip(other, trip["receipt"], "2", end, now=end)
        first = self.store.finish_trip(self.token, trip["receipt"], "2.0", end, now=end)
        again = self.store.finish_trip(self.token, trip["receipt"], "002", end, now=end)
        self.assertFalse(first["duplicate"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["profile"]["usable_records"], 1)
        self.store.delete_observation(self.token, trip["receipt"])
        self.assertEqual(self.store.profile(self.token, "test-model")["factor"], 1.0)
        self.record(0.8)
        self.store.delete_profile(self.token)
        self.assertEqual(
            self.store.db.execute(
                "SELECT COUNT(*) FROM personal_observations"
            ).fetchone()[0],
            0,
        )

    def test_invalid_inputs(self):
        for base in (0.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.store.start_trip(self.token, "1", "2", NOW, base, "test", now=NOW)
        with self.assertRaises(ValueError):
            self.store.start_trip(
                self.token, "1", "2", NOW + timedelta(hours=1), 10, "test", now=NOW
            )
        with self.assertRaises(ValueError):
            self.store.profile("unknown", "test")

    def test_same_person_cannot_start_duplicate_active_rides(self):
        q = self.store.start_trip(self.token, "1", "2", NOW, 10, "test", now=NOW)
        with self.assertRaises(ValueError):
            self.store.start_trip(self.token, "1", "2", NOW, 10, "test", now=NOW)
        self.store.delete_observation(self.token, q["receipt"])
        self.store.start_trip(self.token, "1", "2", NOW, 10, "test", now=NOW)


class RoutingPersonalizationTests(unittest.TestCase):
    def test_later_inventory_is_checked_at_personalized_arrival(self):
        start = datetime.fromisoformat("2026-10-26T08:00:00+09:00")
        net = Network(
            {k: {} for k in ("O", "B", "S", "C", "D", "Z")},
            walks=[
                {"from": "B", "to": "S", "minutes": 1},
                {"from": "C", "to": "D", "minutes": 1},
            ],
            bikes=[
                {"from": "O", "to": "B", "base": 10},
                {"from": "D", "to": "Z", "base": 2},
            ],
            transit=[
                {
                    "from": "S",
                    "to": "C",
                    "mode": "subway",
                    "trip_id": "T",
                    "departure": (start + timedelta(minutes=9)).isoformat(),
                    "arrival": (start + timedelta(minutes=12)).isoformat(),
                }
            ],
        )

        class Base:
            eta_quantile = "p50"

            def __call__(self, e, departure):
                return {
                    "minutes": e["base"],
                    "base_eta": {"p50_min": e["base"]},
                    "user_factor": 1.0,
                    "p_empty": 0.0,
                    "inventory_prediction": {"checked_for": departure},
                }

        result = Router(
            net,
            PersonalizedBikeProvider(Base(), {"factor": 0.8}),
            Config(top_k=1, horizon_minutes=30),
        ).search("O", "Z", start)
        legs = [leg for leg in result["routes"][0]["legs"] if leg["mode"] == "bicycle"]
        self.assertEqual(len(legs), 2)
        self.assertEqual(
            legs[1]["details"]["inventory_prediction"]["checked_for"],
            (start + timedelta(minutes=13)).isoformat(),
        )

    def test_scheduled_runtime_cold_start_matches_existing_route_then_uses_profile(
        self,
    ):
        import scheduled_runtime as runtime
        from scheduled_runtime import ScheduledConfig

        start = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(
            microsecond=0
        )
        when = start.isoformat()
        table = Network(
            {
                "S": {"latitude": 37.5, "longitude": 127.0, "kind": "stop"},
                "T": {"latitude": 37.51, "longitude": 127.0, "kind": "stop"},
            },
            transit=[
                {
                    "from": "S",
                    "to": "T",
                    "departure": (start + timedelta(minutes=10)).isoformat(),
                    "arrival": (start + timedelta(minutes=20)).isoformat(),
                    "mode": "bus",
                    "trip_id": "T",
                }
            ],
            metadata={"valid_departure": when, "valid_horizon_minutes": 180},
        )

        class Client:
            def __init__(self, *a, **k):
                pass

            def get(self, *a):
                return (
                    {
                        "status": "OK",
                        "route": {
                            "properties": {"totalTime": 60, "totalDistance": 80},
                            "legs": [],
                        },
                    },
                    when,
                )

            def stats(self):
                return {"new_api_calls": 0, "cache_hits": 0}

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            catalog = path / "stations.csv"
            catalog.write_text(
                "station_id,station_name,latitude,longitude\n1,A,37.5001,127\n2,B,37.5099,127\n",
                encoding="utf-8",
            )
            personal_path = path / "personal.sqlite"
            with PersonalizationStore(personal_path) as store:
                token = store.create_profile()
            model = FakeETA()

            def run(token_arg):
                return runtime.run_scheduled_optimization(
                    table.nodes["S"],
                    table.nodes["T"],
                    when,
                    eta_service=model,
                    timetable=table,
                    use_kma=False,
                    use_inventory=False,
                    personalization_id=token_arg,
                    personalization_path=personal_path,
                    config=ScheduledConfig(
                        top_k=1,
                        require_known_inventory=False,
                        max_empty_probability=None,
                    ),
                    output_dir=path / "out",
                    progress=None,
                )

            with (
                patch.object(runtime, "STATION_CATALOG", catalog),
                patch.object(runtime, "RUNTIME_CACHE_DIR", path / "cache"),
                patch.object(runtime, "KakaoClient", Client),
                patch.object(runtime, "_artifact_identity", return_value=[]),
                patch("transit_geometry.attach_transit_geometry"),
            ):
                ordinary = run(None)
                cold = run(token)
                self.assertEqual(
                    ordinary["routes"][0]["total_minutes"],
                    cold["routes"][0]["total_minutes"],
                )
                self.assertEqual(cold["personalization"]["factor"], 1.0)
                self.assertEqual(cold["bicycle_checkpoint"]["misses"], 0)
                with PersonalizationStore(personal_path) as store:
                    context = baseline_context()
                    for i in range(15):
                        t = NOW + timedelta(hours=i)
                        q = store.start_trip(token, "1", "2", t, 10, context, now=t)
                        end = t + timedelta(minutes=8)
                        store.finish_trip(token, q["receipt"], "2", end, now=end)
                trained = run(token)
                self.assertLess(
                    trained["routes"][0]["total_minutes"],
                    ordinary["routes"][0]["total_minutes"],
                )
                # Changed arrival times can legitimately introduce a new time
                # bucket; identical base requests must still be cache hits.
                self.assertGreater(trained["bicycle_checkpoint"]["hits"], 0)
                self.assertNotIn(token, str(trained))

    def test_two_people_share_base_predictions_not_personalized_minutes(self):
        model = FakeETA()
        edge = {"from": "O", "to": "Z", "eta_from": "1", "eta_to": "2"}
        with tempfile.TemporaryDirectory() as folder:
            cache = CachedBikeProvider(
                V521BikeProvider(model),
                Path(folder) / "eta.sqlite",
                {"user_factor": 1.0},
            )
            try:
                a = PersonalizedBikeProvider(cache, {"factor": 0.8})(
                    edge, NOW.isoformat()
                )
                b = PersonalizedBikeProvider(cache, {"factor": 1.2})(
                    edge, NOW.isoformat()
                )
                self.assertEqual(a["minutes"], 8.0)
                self.assertEqual(b["minutes"], 12.0)
                self.assertEqual(a["base_eta"]["p50_min"], 10.0)
                self.assertEqual(model.calls, 1)
                self.assertEqual(
                    cache._values[next(iter(cache._values))]["minutes"], 10.0
                )
            finally:
                cache.close()

    def test_corrected_time_changes_train_connection_and_winning_route(self):
        start = datetime.fromisoformat("2026-10-26T08:00:00+09:00")
        net = Network(
            {k: {} for k in ("O", "B", "S", "Z")},
            walks=[{"from": "B", "to": "S", "minutes": 1}],
            bikes=[{"from": "O", "to": "B", "eta_from": "1", "eta_to": "2"}],
            transit=[
                {
                    "from": "O",
                    "to": "Z",
                    "mode": "bus",
                    "trip_id": "bus",
                    "departure": start.isoformat(),
                    "arrival": (start + timedelta(minutes=20)).isoformat(),
                },
                {
                    "from": "S",
                    "to": "Z",
                    "mode": "subway",
                    "trip_id": "train",
                    "departure": (start + timedelta(minutes=9)).isoformat(),
                    "arrival": (start + timedelta(minutes=15)).isoformat(),
                },
            ],
        )
        with tempfile.TemporaryDirectory() as folder:
            cache = CachedBikeProvider(
                V521BikeProvider(FakeETA()), Path(folder) / "eta.sqlite", {}
            )
            try:
                ordinary = Router(
                    net, cache, Config(top_k=1, horizon_minutes=30)
                ).search("O", "Z", start)
                fast = Router(
                    net,
                    PersonalizedBikeProvider(cache, {"factor": 0.8}),
                    Config(top_k=1, horizon_minutes=30),
                ).search("O", "Z", start)
                self.assertEqual(ordinary["routes"][0]["bike_rentals"], 0)
                self.assertEqual(fast["routes"][0]["bike_rentals"], 1)
                self.assertEqual(fast["routes"][0]["total_minutes"], 15)
            finally:
                cache.close()

    def test_p80_and_double_scaling_are_rejected(self):
        with self.assertRaises(ValueError):
            PersonalizedBikeProvider(
                V521BikeProvider(FakeETA(), eta_quantile="p80"), {"factor": 0.8}
            )
        p = PersonalizedBikeProvider(
            V521BikeProvider(FakeETA(), user_factor=0.9), {"factor": 0.8}
        )
        with self.assertRaises(ValueError):
            p({"eta_from": "1", "eta_to": "2"}, NOW.isoformat())


if __name__ == "__main__":
    unittest.main()
