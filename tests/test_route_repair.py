from pathlib import Path
import sys
import unittest
import networkx as nx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.route_engine_fixture import route_engine_class
from multimodal import Network, Router, Config
from routing_errors import NoBicycleRoute


class RouteRepairTests(unittest.TestCase):
    def graph(self):
        g = nx.MultiDiGraph()
        # C is an isolated spur, B a slightly farther but accessible road node.
        for node, x, y in [
            ("A", 127, 37.5),
            ("B", 127.001, 37.5),
            ("C", 127.001, 37.5001),
        ]:
            g.add_node(node, x=x, y=y, elevation=10)
        g.add_edge("A", "B", length=88.0, highway="cycleway")
        return g

    def test_retry_is_connected_not_an_artificial_bridge(self):
        engine = route_engine_class()(self.graph(), ":memory:", "test")
        try:
            routes = engine.candidates(37.5, 127, 37.5001, 127.001)
            self.assertEqual(routes[0]["snap_method"], "nearest_reachable_node_bounded")
            self.assertEqual(routes[0]["network_distance_m"], 88.0)
            self.assertEqual(routes[0]["geometry"]["coordinates"][-1], [127.001, 37.5])
            self.assertEqual(len(engine.graph.edges), 1)
        finally:
            engine.close()

    def test_distant_disconnected_roads_are_not_joined(self):
        engine = route_engine_class()(self.graph(), ":memory:", "test")
        try:
            self.assertEqual(engine.candidates(37.5, 127, 37.51, 127.02), [])
        finally:
            engine.close()

    def test_one_way_is_respected(self):
        engine = route_engine_class()(self.graph(), ":memory:", "test", max_snap_m=5)
        try:
            self.assertEqual(engine.candidates(37.5, 127.001, 37.5, 127), [])
        finally:
            engine.close()

    def test_structurally_unavailable_pair_is_reported_once_not_model_error(self):
        class Provider:
            def __init__(self):
                self.calls = 0

            def __call__(self, *args):
                self.calls += 1
                raise NoBicycleRoute("disconnected")

        provider = Provider()
        network = Network(
            {"O": {}, "Z": {}},
            walks=[{"from": "O", "to": "Z", "minutes": 5}],
            bikes=[{"from": "O", "to": "Z"}],
        )
        result = Router(network, provider, Config(top_k=1, horizon_minutes=10)).search(
            "O", "Z", "2026-10-17T08:00:00+09:00"
        )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result["prediction_failures"], 0)
        self.assertEqual(len(result["unroutable_bike_pairs"]), 1)
        self.assertEqual(result["status"], "ok")
