"""Proven dead ends, expired rental windows and exhausted journey horizons."""

import sys, unittest
from pathlib import Path

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).parent),
]
from test_walking_budget import BudgetWalking, START
from multimodal import Network, Router, Config
from scheduled_runtime import ScheduledConfig, build_scheduled_network
from candidate_edges import DeadEndGuard


def point(lat, kind=None):
    p = dict(latitude=lat, longitude=127.0)
    if kind:
        p["kind"] = kind
    return p


def ride(a, b, trip="B"):
    return dict(
        **{"from": a, "to": b},
        mode="bus",
        trip_id=trip,
        departure="2026-10-30T08:35:00+09:00",
        arrival="2026-10-30T08:45:00+09:00",
    )


def future_case():
    o, d = point(37.5), point(37.53)
    stations = [
        dict(point(37.507), station_id="1", station_name="1"),
        dict(point(37.523), station_id="2", station_name="2"),
    ]
    table = Network(
        nodes={"S": point(37.493, "stop"), "T": point(37.537, "stop")},
        transit=[ride("S", "T")],
    )
    return o, d, stations, table


def dead_end_case():
    o, d = point(37.5), point(37.57)
    nodes = {
        "D": point(37.507, "stop"),
        "S": point(37.493, "stop"),
        "X": point(37.56, "stop"),
        "T": point(37.57, "stop"),
    }
    return (
        o,
        d,
        [],
        Network(nodes=nodes, transit=[ride("X", "D", "XD"), ride("S", "T", "ST")]),
    )


class QueryWasteTests(unittest.TestCase):
    def test_expired_rentals_skip_exclusive_station_queries(self):
        o, d, s, t = future_case()
        w = BudgetWalking(20)
        n = build_scheduled_network(
            t, s, o, d, w, ScheduledConfig(bicycle_available=False, max_kakao_calls=20)
        )
        self.assertFalse(n.bike_policy)
        self.assertIn("bike:1", n.nodes)
        self.assertFalse(
            any(a.startswith("bike:") or b.startswith("bike:") for a, b in w.queries)
        )
        r = Router(n, None, Config(top_k=3), walking_provider=w).search(
            "origin", "destination", START
        )
        self.assertTrue(r["routes"])
        self.assertEqual(w.client.calls, 2)

    def test_expired_station_is_kept_as_a_necessary_walking_relay(self):
        o, d = point(37.5), point(37.55)
        stations = [dict(point(37.506), station_id="1", station_name="relay")]
        t = Network(
            nodes={"S": point(37.512, "stop"), "T": point(37.55, "stop")},
            transit=[ride("S", "T")],
        )
        w = BudgetWalking(20)
        n = build_scheduled_network(
            t, stations, o, d, w, ScheduledConfig(bicycle_available=False)
        )
        r = Router(n, None, Config(), walking_provider=w).search(
            "origin", "destination", START
        )
        self.assertTrue(r["routes"])
        legs = r["routes"][0]["legs"]
        self.assertTrue(any(e["to"] == "bike:1" for e in legs))
        self.assertTrue(any(e["mode"] == "bus" for e in legs))
        self.assertFalse(any(e["mode"] == "bicycle" for e in legs))

    def test_expired_bicycle_only_search_uses_no_walking_calls(self):
        o, d, s, t = future_case()
        w = BudgetWalking(20)
        n = build_scheduled_network(
            t,
            s,
            o,
            d,
            w,
            ScheduledConfig(search_mode="bicycle", bicycle_available=False),
        )
        self.assertFalse(n.bike_policy)
        self.assertEqual(w.client.calls, 0)

    def test_terminal_without_any_exit_is_not_queried(self):
        o, d, s, t = dead_end_case()
        w = BudgetWalking(20)
        n = build_scheduled_network(
            t, s, o, d, w, ScheduledConfig(search_mode="transit")
        )
        self.assertNotIn(("origin", "D"), w.queries)
        r = Router(n, None, Config(required_mode="transit"), walking_provider=w).search(
            "origin", "destination", START
        )
        self.assertTrue(r["routes"])
        self.assertEqual(w.client.calls, 1)

    def test_terminal_with_walking_transfer_is_kept(self):
        o, d, s, t = dead_end_case()
        t.nodes["relay"] = point(37.504, "stop")
        t.transit.append(ride("relay", "T", "relay-T"))
        w = BudgetWalking(20)
        n = build_scheduled_network(
            t, s, o, d, w, ScheduledConfig(search_mode="transit")
        )
        self.assertFalse(DeadEndGuard(n).is_dead("D"))
        self.assertIn(("origin", "D"), w.queries)

    def test_unknown_bicycle_and_walking_exits_remain_candidates(self):
        n = Network(
            nodes={
                "origin": point(37.5),
                "destination": point(37.6),
                "B": point(37.501, "bike"),
                "S": point(37.502, "stop"),
            },
            bike_policy={"max_distance_m": 7000},
            walk_policy={"max_distance_m": 900},
        )
        guard = DeadEndGuard(n)
        self.assertFalse(guard.is_dead("B"))
        self.assertFalse(guard.is_dead("S"))
        self.assertFalse(guard.is_dead("destination"))

    def test_custom_destination_name_is_never_pruned(self):
        n = Network(
            nodes={"origin": point(37.5), "Z": point(37.505)},
            walk_policy={"max_distance_m": 900},
        )
        w = BudgetWalking(20)
        r = Router(n, None, Config(top_k=1), walking_provider=w).search(
            "origin", "Z", START
        )
        self.assertTrue(r["routes"])

    def test_horizon_end_skips_new_queries_but_preserves_known_route(self):
        n = Network(
            nodes={
                "origin": point(37.5),
                "A": point(37.51),
                "destination": point(37.515),
            },
            walks=[
                {"from": "origin", "to": "A", "minutes": 1},
                {"from": "origin", "to": "destination", "minutes": 0.5},
            ],
            walk_policy={"max_distance_m": 900},
        )
        w = BudgetWalking(20)
        r = Router(
            n, None, Config(horizon_minutes=1, top_k=3), walking_provider=w
        ).search("origin", "destination", START)
        self.assertEqual(r["routes"][0]["total_minutes"], 0.5)
        self.assertEqual(w.client.calls, 0)

    def test_arrival_on_vehicle_at_horizon_can_alight_at_destination(self):
        e = dict(
            **{"from": "origin", "to": "destination"},
            mode="bus",
            trip_id="B",
            departure=START,
            arrival="2026-10-30T08:31:00+09:00",
        )
        n = Network(
            nodes={"origin": point(37.5), "destination": point(37.515)}, transit=[e]
        )
        r = Router(n, None, Config(horizon_minutes=1, required_mode="transit")).search(
            "origin", "destination", START
        )
        self.assertTrue(r["routes"])
        self.assertEqual(r["routes"][0]["total_minutes"], 1)


if __name__ == "__main__":
    unittest.main()
