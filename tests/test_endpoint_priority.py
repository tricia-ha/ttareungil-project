"""Candidate order changes must not become hidden candidate pruning."""

from pathlib import Path
import sys, unittest

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).parent),
]
from endpoint_priority import EndpointPriority
from scheduled_runtime import ScheduledConfig, build_scheduled_network
from multimodal import Network, Router, Config
from test_walking_budget import BudgetWalking, START


def scenario():
    nodes = {
        "origin": dict(latitude=37.5, longitude=127, kind="endpoint"),
        "destination": dict(latitude=37.54, longitude=127, kind="endpoint"),
        "slow_a": dict(latitude=37.5001, longitude=127, kind="stop"),
        "slow_b": dict(latitude=37.5399, longitude=127, kind="stop"),
        "fast_a": dict(latitude=37.502, longitude=127, kind="stop"),
        "fast_b": dict(latitude=37.538, longitude=127, kind="stop"),
    }
    transit = [
        dict(
            **{"from": "slow_a", "to": "slow_b"},
            mode="subway",
            trip_id="slow",
            departure="2026-10-30T08:35:00+09:00",
            arrival="2026-10-30T09:15:00+09:00",
        ),
        dict(
            **{"from": "fast_a", "to": "fast_b"},
            mode="bus",
            trip_id="fast",
            departure="2026-10-30T08:35:00+09:00",
            arrival="2026-10-30T08:40:00+09:00",
        ),
    ]
    groups = [
        [("origin", "slow_a")],
        [("slow_b", "destination")],
        [("origin", "fast_a")],
        [("fast_b", "destination")],
    ]
    return nodes, transit, groups


class EndpointPriorityTests(unittest.TestCase):
    def test_farther_faster_bus_is_ranked_before_nearer_slow_subway(self):
        nodes, transit, groups = scenario()
        rank = EndpointPriority(nodes, transit, [], groups, 7000)
        ordered = list(rank.ordered(groups))
        self.assertEqual(ordered[:2], [("origin", "fast_a"), ("fast_b", "destination")])
        self.assertEqual(set(ordered), {pair for group in groups for pair in group})
        self.assertLess(rank.score(ordered[0])[1], rank.score(("origin", "slow_a"))[1])

    def test_unknown_transfer_is_retained_and_sides_alternate(self):
        nodes, transit, groups = scenario()
        nodes["unknown"] = dict(latitude=37.503, longitude=127, kind="stop")
        groups[0].append(("origin", "unknown"))
        ordered = list(
            EndpointPriority(nodes, transit, [], groups, 7000).ordered(groups)
        )
        self.assertIn(("origin", "unknown"), ordered)
        self.assertEqual(len(ordered), sum(map(len, groups)))
        self.assertEqual(
            [a == "origin" for a, b in ordered[:4]], [True, False, True, False]
        )

    def test_waiting_changes_priority_even_when_vehicle_is_fast(self):
        nodes, transit, groups = scenario()
        transit[1].update(
            departure="2026-10-30T09:20:00+09:00", arrival="2026-10-30T09:25:00+09:00"
        )
        ordered = list(
            EndpointPriority(nodes, transit, [], groups, 7000, START).ordered(groups)
        )
        self.assertEqual(ordered[:2], [("origin", "slow_a"), ("slow_b", "destination")])

    def test_known_road_walk_overrides_straight_line_estimate(self):
        nodes, transit, groups = scenario()
        walks = [
            {"from": "origin", "to": "fast_a", "minutes": 9, "source": "kakao_walk"}
        ]
        rank = EndpointPriority(nodes, transit, walks, groups, 7000, START)
        self.assertEqual(rank.access_costs["origin", "fast_a"], 9)
        self.assertEqual(
            list(rank.ordered(groups))[:2],
            [("origin", "slow_a"), ("slow_b", "destination")],
        )

    def test_cached_impossible_access_cannot_be_the_preferred_pair(self):
        nodes, transit, groups = scenario()
        for edge in transit:
            edge["mode"] = "bus"
        table = Network(
            nodes={
                k: v for k, v in nodes.items() if k not in ("origin", "destination")
            },
            transit=transit,
        )
        walking = BudgetWalking(6, {("origin", "fast_a"): None})
        build_scheduled_network(
            table,
            [],
            nodes["origin"],
            nodes["destination"],
            walking,
            ScheduledConfig(search_mode="transit"),
            departure=START,
        )
        self.assertNotIn(("origin", "fast_a"), walking.queries)
        self.assertEqual(
            walking.queries[:2], [("origin", "slow_a"), ("slow_b", "destination")]
        )

    def test_no_endpoint_calls_when_origin_is_provably_isolated(self):
        nodes, transit, groups = scenario()
        table = Network(
            nodes={
                k: v for k, v in nodes.items() if k not in ("origin", "destination")
            },
            transit=transit,
        )
        walking = BudgetWalking(6)
        graph = build_scheduled_network(
            table,
            [],
            dict(latitude=37.4, longitude=127),
            nodes["destination"],
            walking,
            ScheduledConfig(search_mode="transit"),
            departure=START,
        )
        self.assertEqual(walking.client.calls, 0)
        self.assertTrue(graph.metadata["endpoint_origin_unreachable"])

    def test_known_quick_connection_is_secured_under_small_call_budget(self):
        nodes, transit, groups = scenario()
        # Put both services into one mode group: the nearest service must not
        # hide a faster stop farther from both endpoints.
        for edge in transit:
            edge["mode"] = "bus"
        table = Network(
            nodes={
                k: v for k, v in nodes.items() if k not in ("origin", "destination")
            },
            transit=transit,
        )
        walking = BudgetWalking(6)
        graph = build_scheduled_network(
            table,
            [],
            nodes["origin"],
            nodes["destination"],
            walking,
            ScheduledConfig(search_mode="transit"),
        )
        self.assertEqual(
            walking.queries[:2], [("origin", "fast_a"), ("fast_b", "destination")]
        )
        self.assertEqual(walking.client.calls, 2)
        result = Router(
            Network(nodes=graph.nodes, walks=graph.walks, transit=graph.transit),
            None,
            Config(required_mode="transit", top_k=1, horizon_minutes=60),
        ).search("origin", "destination", START)
        self.assertEqual(result["routes"][0]["total_minutes"], 11)
        self.assertFalse(graph.metadata["probability_pruning"])


if __name__ == "__main__":
    unittest.main()
