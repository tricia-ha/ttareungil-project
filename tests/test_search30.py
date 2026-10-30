"""Conservative closed-component pruning and cache-only continuation."""

import sys, unittest
from pathlib import Path

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).parent),
]
from test_query_waste import point, ride, dead_end_case
from test_walking_budget import BudgetWalking
from candidate_edges import LazyConnections, DeadEndGuard
from scheduled_runtime import ScheduledConfig, build_scheduled_network
from multimodal import Config, Network
from kakao_routes import CallLimit


class Search30Tests(unittest.TestCase):
    def test_default_time_is_thirty_seconds(self):
        from project_service import RouteQuery

        self.assertEqual(Config().max_seconds, 30)
        self.assertEqual(ScheduledConfig().max_seconds, 30)
        self.assertEqual(RouteQuery.model_fields["max_seconds"].default, 30)

    def component(self, escape=False):
        o, d, s, t = dead_end_case()
        t.nodes["E"] = point(37.508, "stop")
        t.transit.append(ride("X", "E", "XE"))
        if escape:
            t.nodes["R"] = point(37.501, "stop")
            t.transit.append(ride("X", "R", "XR"))
        walking = BudgetWalking(20)
        n = build_scheduled_network(
            t,
            s,
            o,
            d,
            walking,
            ScheduledConfig(search_mode="transit", max_kakao_calls=20),
        )
        return n, walking

    def test_closed_walking_component_is_not_queried(self):
        n, w = self.component()
        self.assertNotIn(("origin", "D"), w.queries)
        self.assertNotIn(("origin", "E"), w.queries)
        self.assertTrue(DeadEndGuard(n).is_dead("D"))
        self.assertTrue(DeadEndGuard(n).is_dead("E"))

    def test_walking_escape_from_component_is_retained(self):
        n, w = self.component(escape=True)
        self.assertFalse(DeadEndGuard(n).is_dead("D"))
        self.assertIn(("origin", "D"), w.queries)

    def test_exhausted_budget_skips_provider_calls_but_keeps_cache(self):
        class Walking(BudgetWalking):
            attempts = 0

            def __call__(self, *args):
                self.attempts += 1
                return super().__call__(*args)

        nodes = {
            "origin": point(37.5),
            "A": point(37.501),
            "B": point(37.502),
            "C": point(37.503),
            "D": point(37.504),
            "destination": point(37.51),
        }
        cached = dict(**{"from": "origin", "to": "C"}, minutes=2)
        walking = Walking(1, {("origin", "C"): cached})
        lazy = LazyConnections(
            Network(nodes=nodes, walk_policy={"max_distance_m": 900}), walking
        )
        found = lazy.walks_from("origin")
        self.assertIn(cached, found)
        self.assertEqual(walking.client.calls, 1)
        self.assertEqual(walking.attempts, 2)
        self.assertTrue(lazy._new_walks_blocked)
        self.assertGreater(lazy.network.metadata["skipped_blocked_walk_lookups"], 0)

    def test_large_unresolved_component_is_retained(self):
        nodes = {"origin": point(37.49), "destination": point(37.8)}
        nodes.update({str(i): point(37.5 + i * 0.00001, "stop") for i in range(40)})
        guard = DeadEndGuard(Network(nodes=nodes, walk_policy={"max_distance_m": 900}))
        self.assertFalse(guard.is_dead("0"))

    def test_provider_without_cache_interface_keeps_later_cached_edges(self):
        nodes = {
            "origin": point(37.5),
            "A": point(37.501),
            "C": point(37.502),
            "destination": point(37.505),
        }
        first = [True]

        def walk(a, b, start, end):
            if end == "C":
                return {"from": start, "to": end, "minutes": 2}
            if first[0]:
                first[0] = False
                return {"from": start, "to": end, "minutes": 1}
            raise CallLimit("limit; later entries may be cached")

        lazy = LazyConnections(
            Network(nodes=nodes, walk_policy={"max_distance_m": 900}), walk
        )
        self.assertIn("C", {e["to"] for e in lazy.walks_from("origin")})


if __name__ == "__main__":
    unittest.main()
