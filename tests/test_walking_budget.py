"""Directed walking budget, graph completeness and early completion regressions."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kakao_routes import CallLimit
from scheduled_runtime import ScheduledConfig, build_scheduled_network
from candidate_edges import LazyConnections
from multimodal import Config, Network, Router
from multimodal_data import distance

START = "2026-10-30T08:30:00+09:00"


class BudgetWalking:
    def __init__(self, maximum=12, cache=None):
        self.client = SimpleNamespace(calls=0, max_calls=maximum)
        self.values = dict(cache or {})
        self.queries = []

    def cached(self, a, b, start, end):
        key = (start, end)
        return (key in self.values, self.values.get(key))

    def __call__(self, a, b, start, end):
        hit, edge = self.cached(a, b, start, end)
        if hit:
            return edge
        if self.client.calls >= self.client.max_calls:
            raise CallLimit("test budget")
        self.client.calls += 1
        self.queries.append((start, end))
        edge = dict(**{"from": start, "to": end}, minutes=1.0)
        self.values[start, end] = edge
        return edge


def fixtures():
    origin = dict(latitude=37.5, longitude=127.0)
    destination = dict(latitude=37.53, longitude=127.0)
    stations = [
        dict(
            station_id=str(i),
            station_name=str(i),
            latitude=37.5005 + i * 0.0002,
            longitude=127.0,
        )
        for i in range(12)
    ] + [
        dict(
            station_id=str(i + 20),
            station_name=str(i + 20),
            latitude=37.5295 - i * 0.0002,
            longitude=127.0,
        )
        for i in range(12)
    ]
    nodes = {
        "S": dict(kind="stop", latitude=37.5004, longitude=127.0),
        "T": dict(kind="stop", latitude=37.5296, longitude=127.0),
    }
    transit = [
        dict(
            **{"from": "S", "to": "T"},
            mode="bus",
            trip_id="bus1",
            departure="2026-10-30T08:35:00+09:00",
            arrival="2026-10-30T08:45:00+09:00",
        )
    ]
    return origin, destination, stations, Network(nodes=nodes, transit=transit)


class WalkingBudgetTests(unittest.TestCase):
    def test_bus_and_rail_receive_endpoint_seeds(self):
        o, d, stations, table = fixtures()
        table.nodes.update(
            {
                "subway:A": dict(kind="stop", latitude=37.5006, longitude=127.0),
                "subway:B": dict(kind="stop", latitude=37.5294, longitude=127.0),
            }
        )
        table.transit.append(
            dict(
                table.transit[0],
                **{
                    "from": "subway:A",
                    "to": "subway:B",
                    "mode": "subway",
                    "trip_id": "rail1",
                },
            )
        )
        for mode in ("integrated", "transit"):
            walking = BudgetWalking()
            build_scheduled_network(
                table, stations, o, d, walking, ScheduledConfig(search_mode=mode)
            )
            queries = set(walking.queries)
            self.assertIn(("origin", "subway:A"), queries)
            self.assertIn(("subway:B", "destination"), queries)
            self.assertIn(("origin", "S"), queries)
            self.assertIn(("T", "destination"), queries)
            self.assertLessEqual(walking.client.calls, 6)

    def test_all_modes_match_exhaustively_materialized_reference(self):
        o, d, stations, table = fixtures()
        stations = [stations[0], stations[12]]
        for mode in ("integrated", "transit", "bicycle"):
            walking = BudgetWalking(1000)
            network = build_scheduled_network(
                table, stations, o, d, walking, ScheduledConfig(search_mode=mode)
            )
            walks, bikes = [], []
            for a, source in network.nodes.items():
                for b, target in network.nodes.items():
                    if a == b:
                        continue
                    if (
                        a != "destination"
                        and b != "origin"
                        and not source.get("kind") == target.get("kind") == "bike"
                        and distance(source, target) <= 900
                    ):
                        walks.append(dict(**{"from": a, "to": b}, minutes=1))
                    if source.get("kind") == target.get("kind") == "bike":
                        if mode == "bicycle" and (
                            source["station_id"]
                            not in network.bike_policy["origin_access_ids"]
                            or target["station_id"]
                            not in network.bike_policy["destination_access_ids"]
                        ):
                            continue
                        if distance(source, target) <= 7000:
                            bikes.append(dict(**{"from": a, "to": b}))
            reference = Network(
                nodes=network.nodes, walks=walks, bikes=bikes, transit=network.transit
            )
            config = Config(
                required_mode=None if mode == "integrated" else mode,
                top_k=1,
                horizon_minutes=20,
                max_seconds=5,
            )
            predict = lambda edge, departure: dict(minutes=5, p_empty=0.0)
            expected = Router(reference, predict, config).search(
                "origin", "destination", START
            )
            actual = Router(network, predict, config, walking_provider=walking).search(
                "origin", "destination", START
            )
            self.assertTrue(actual["routes"], mode)
            self.assertEqual(
                actual["routes"][0]["total_minutes"],
                expected["routes"][0]["total_minutes"],
                mode,
            )

    def build(self, mode, walking):
        o, d, stations, table = fixtures()
        return build_scheduled_network(
            table,
            stations,
            o,
            d,
            walking,
            ScheduledConfig(search_mode=mode, max_kakao_calls=12),
        )

    def test_seed_budget_balances_endpoints_and_modes(self):
        for mode in ("bicycle", "transit", "integrated"):
            with self.subTest(mode=mode):
                walking = BudgetWalking()
                network = self.build(mode, walking)
                self.assertEqual(walking.client.calls, 2 if mode == "transit" else 4)
                self.assertTrue(any(a == "origin" for a, b in walking.queries))
                self.assertTrue(any(b == "destination" for a, b in walking.queries))
                if mode == "integrated":
                    self.assertTrue(
                        any(
                            a.startswith("bike:") or b.startswith("bike:")
                            for a, b in walking.queries
                        )
                    )
                    self.assertTrue(
                        any(
                            a in ("S", "T") or b in ("S", "T")
                            for a, b in walking.queries
                        )
                    )
                self.assertFalse(network.metadata["connector_limit_reached"])

    def test_cached_distant_rank_endpoint_survives_zero_new_budget(self):
        edge = dict(**{"from": "origin", "to": "bike:11"}, minutes=2)
        walking = BudgetWalking(0, {("origin", "bike:11"): edge})
        network = self.build("bicycle", walking)
        self.assertIn(edge, network.walks)
        self.assertEqual(walking.client.calls, 0)

    def test_bicycle_is_evaluated_before_all_twelve_calls_are_spent(self):
        walking = BudgetWalking()
        network = self.build("bicycle", walking)
        calls_at_prediction = []

        def predict(edge, departure):
            calls_at_prediction.append(walking.client.calls)
            return dict(minutes=5.0, p_empty=0.0)

        result = Router(
            network,
            predict,
            Config(
                required_mode="bicycle",
                top_k=1,
                horizon_minutes=15,
                max_seconds=5,
                bike_expansion_batch_size=2,
            ),
            walking_provider=walking,
        ).search("origin", "destination", START)
        self.assertTrue(result["routes"])
        self.assertLess(calls_at_prediction[0], 12)
        self.assertLessEqual(walking.client.calls, 12)

    def test_goal_walk_is_paid_before_nearer_optional_stops(self):
        nodes = {
            "origin": dict(latitude=37.49, longitude=127.0),
            "destination": dict(latitude=37.505, longitude=127.0),
            "A": dict(kind="stop", latitude=37.5, longitude=127.0),
            "near": dict(kind="stop", latitude=37.5001, longitude=127.0),
        }
        walking = BudgetWalking(1)
        lazy = LazyConnections(
            Network(nodes=nodes, walk_policy={"max_distance_m": 900}), walking
        )
        self.assertEqual(next(lazy.iter_walks_from("A"))["to"], "destination")
        self.assertEqual(walking.queries, [("A", "destination")])

    def test_cache_hit_and_negative_hit_survive_exhausted_calls(self):
        nodes = {
            "origin": dict(latitude=37.49, longitude=127.0),
            "destination": dict(latitude=37.505, longitude=127.0),
            "A": dict(kind="stop", latitude=37.5, longitude=127.0),
            "B": dict(kind="stop", latitude=37.5001, longitude=127.0),
        }
        edge = dict(**{"from": "A", "to": "destination"}, minutes=2)
        walking = BudgetWalking(0, {("A", "destination"): edge, ("A", "B"): None})
        lazy = LazyConnections(
            Network(nodes=nodes, walk_policy={"max_distance_m": 900}), walking
        )
        self.assertEqual(lazy.walks_from("A"), [edge])
        self.assertFalse(lazy.network.metadata.get("connector_limit_reached", False))
        self.assertEqual(walking.queries, [])

    def test_no_station_pair_within_radius_needs_no_access_calls(self):
        o, d, stations, table = fixtures()
        d = dict(latitude=37.7, longitude=127.0)
        walking = BudgetWalking()
        network = build_scheduled_network(
            table, stations, o, d, walking, ScheduledConfig(search_mode="bicycle")
        )
        self.assertEqual(walking.client.calls, 0)
        self.assertEqual(
            network.metadata["bicycle_feasibility"],
            "no_station_pair_within_configured_distance",
        )
        self.assertFalse(network.walk_policy)

    def test_full_walk_candidate_set_is_preserved_after_reordering(self):
        walking = BudgetWalking(100)
        network = self.build("integrated", walking)
        lazy = LazyConnections(network, walking)
        for start, source in network.nodes.items():
            if start == "destination":
                continue
            expected = {
                end
                for _, end in lazy.walk_index.near(source, 900)
                if end not in (start, "origin")
                and (start, end) not in lazy._existing_walks
                and not (source.get("kind") == network.nodes[end].get("kind") == "bike")
            }
            self.assertEqual({e["to"] for e in lazy.walks_from(start)}, expected)


if __name__ == "__main__":
    unittest.main()
