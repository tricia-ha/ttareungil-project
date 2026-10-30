"""Regression cases for cache accounting, partial searches and mixed transfers."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import tempfile
import unittest
from multimodal import Network, Router, Config, V521BikeProvider
from multimodal_cache import CachedBikeProvider


def at(minute):
    return f"2026-10-16T08:{minute:02d}:00+09:00"


def ride(a, b, start, end, trip, mode="bus"):
    return {
        "from": a,
        "to": b,
        "departure": at(start),
        "arrival": at(end),
        "trip_id": trip,
        "mode": mode,
    }


class EfficiencyTests(unittest.TestCase):
    def test_budget_keeps_transit_search_alive(self):
        n = Network(
            {key: {} for key in ["O", "X", "Y", "Z"]},
            walks=[
                {"from": "X", "to": "Z", "minutes": 20},
                {"from": "Y", "to": "Z", "minutes": 20},
            ],
            bikes=[{"from": "O", "to": "X"}, {"from": "O", "to": "Y"}],
            transit=[ride("O", "Z", 5, 12, "bus")],
        )
        calls = []

        def predict(e, t):
            calls.append((e, t))
            return {"minutes": 2}

        result = Router(n, predict, Config(top_k=1, max_bike_predictions=1)).search(
            "O", "Z", at(0)
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["routes"][0]["arrival"], at(12))
        self.assertEqual(result["status"], "partial")
        self.assertGreater(result["skipped_bike_requests"], 0)

    def test_cached_eta_is_available_after_new_prediction_budget_is_used(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []

            def predict(edge, t):
                calls.append((edge["to"], t))
                return {"minutes": 1 if edge["to"] == "Z" and t == at(5) else 20}

            cached = CachedBikeProvider(predict, Path(folder) / "cache.sqlite", {})
            z = {"from": "O", "to": "Z"}
            cached(z, at(5))  # Persist a later departure before the search starts.
            before = len(calls)
            n = Network({"O": {}, "Z": {}}, bikes=[z])
            result = Router(n, cached, Config(top_k=1, max_bike_predictions=1)).search(
                "O", "Z", at(0)
            )
            self.assertEqual(result["routes"][0]["arrival"], at(6))
            self.assertEqual(result["bike_predictions"], 1)
            self.assertEqual(len(calls) - before, 1)
            cached.close()

    def test_disk_hits_do_not_consume_new_prediction_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cache.sqlite"
            edge = {"from": "O", "to": "Z"}
            cached = CachedBikeProvider(lambda *_: {"minutes": 3}, path, {})
            cached(edge, at(0))
            cached.close()

            def forbidden(*args):
                raise AssertionError("must use persisted ETA")

            cached = CachedBikeProvider(forbidden, path, {})
            result = Router(
                Network({"O": {}, "Z": {}}, bikes=[edge]),
                cached,
                Config(top_k=1, max_bike_predictions=1),
            ).search("O", "Z", at(0))
            self.assertEqual(result["routes"][0]["arrival"], at(3))
            self.assertEqual(result["bike_predictions"], 0)
            cached.close()

    def test_two_bicycle_rides_between_transit_legs_remain_possible(self):
        n = Network(
            {key: {} for key in "OABCDEFGZ"},
            walks=[
                {"from": "B", "to": "C", "minutes": 1},
                {"from": "E", "to": "F", "minutes": 1},
            ],
            bikes=[{"from": "A", "to": "B"}, {"from": "D", "to": "E"}],
            transit=[
                ride("O", "A", 0, 2, "bus1"),
                ride("C", "D", 6, 8, "train", "subway"),
                ride("F", "Z", 12, 15, "bus2"),
            ],
        )
        result = Router(n, lambda *_: {"minutes": 2}, Config(top_k=1)).search(
            "O", "Z", at(0)
        )
        route = result["routes"][0]
        self.assertEqual(route["total_minutes"], 15)
        self.assertEqual(route["bike_rentals"], 2)
        self.assertEqual(route["transit_boardings"], 3)

    def test_batch_only_computes_misses_and_keeps_exact_inventory_times(self):
        class Service:
            def __init__(self):
                self.batches = []

            def predict_batch(self, requests):
                self.batches.append(requests)
                return [
                    dict(
                        eta={"p50_min": 2, "p80_min": 3, "p90_min": 4},
                        route={},
                        model={},
                    )
                    for request in requests
                ]

        class Provider(V521BikeProvider):
            def __init__(self, service):
                super().__init__(service)
                self.times = []

            def precheck_inventory(self, e, t):
                self.times.append(t)
                return {"p_empty": 0.01 if t == at(1) else 0.2}

        with tempfile.TemporaryDirectory() as folder:
            service = Service()
            provider = Provider(service)
            cached = CachedBikeProvider(provider, Path(folder) / "cache.sqlite", {})
            a = {"from": "O", "to": "A", "eta_from": "1", "eta_to": "2"}
            b = {"from": "O", "to": "B", "eta_from": "1", "eta_to": "3"}
            self.assertEqual(cached.prefetch([a, a, b], at(1)), 2)
            self.assertEqual(cached.prefetch([a, b], at(2)), 0)
            self.assertEqual(len(service.batches), 1)
            self.assertEqual(len(service.batches[0]), 2)
            self.assertEqual(cached(a, at(1))["p_empty"], 0.01)
            self.assertEqual(cached(a, at(4))["p_empty"], 0.2)
            self.assertEqual(provider.times, [at(1), at(4)])
            self.assertEqual(cached.misses, 2)
            cached.close()

    def test_train_stays_aboard_over_dwell_time(self):
        n = Network(
            {"O": {}, "A": {}, "Z": {}},
            transit=[
                ride("O", "A", 0, 3, "T", "subway"),
                ride("A", "Z", 4, 7, "T", "subway"),
                ride("A", "Z", 2, 6, "earlier", "subway"),
            ],
        )
        result = Router(n, lambda *_: None, Config(top_k=1)).search("O", "Z", at(0))
        self.assertEqual(result["routes"][0]["arrival"], at(7))
        self.assertEqual(result["routes"][0]["transit_boardings"], 1)

    def test_wait_states_are_not_needed_at_plain_transit_stops(self):
        n = Network({"O": {}, "Z": {}}, transit=[ride("O", "Z", 50, 55, "late")])
        result = Router(n, lambda *_: None, Config(top_k=1, max_expansions=5)).search(
            "O", "Z", at(0)
        )
        self.assertEqual(result["routes"][0]["arrival"], at(55))
        self.assertTrue(result["search_complete"])
        self.assertLessEqual(result["expanded_states"], 4)

    def test_waiting_for_inventory_is_kept_even_if_empty_now(self):
        class Provider:
            def precheck_inventory(self, e, t):
                return {"p_empty": 1.0 if t < at(2) else 0.0}

            def __call__(self, e, t):
                return dict(minutes=1, **self.precheck_inventory(e, t))

        n = Network({"O": {}, "Z": {}}, bikes=[{"from": "O", "to": "Z"}])
        result = Router(
            n,
            Provider(),
            Config(top_k=1, require_known_inventory=True, max_empty_probability=0.1),
        ).search("O", "Z", at(0))
        self.assertEqual(result["routes"][0]["arrival"], at(3))

    def test_bad_batch_falls_back_to_individual_edges(self):
        class Provider:
            def predict_eta_batch(self, edges, t):
                raise ValueError("one bad batch item")

            def predict_eta(self, e, t):
                return {"minutes": 2}

        with tempfile.TemporaryDirectory() as folder:
            cache = CachedBikeProvider(Provider(), Path(folder) / "cache.sqlite", {})
            n = Network({"O": {}, "Z": {}}, bikes=[{"from": "O", "to": "Z"}])
            result = Router(n, cache, Config(top_k=1)).search("O", "Z", at(0))
            self.assertEqual(result["routes"][0]["arrival"], at(2))
            self.assertEqual(result["prediction_failures"], 0)
            self.assertGreater(result["bike_batch_fallbacks"], 0)
            cache.close()


if __name__ == "__main__":
    unittest.main()
