"""Optional local checks. Synthetic responses only; no API/model execution.

Run from repository root:
python -m unittest discover -s tests -p test_kakao_optimizer.py -v
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import unittest

from kakao_routes import walking_edge, transit_edges
from kakao_optimizer import KakaoConfig, search, build_network


AT = "2026-10-15T08:00:00+09:00"


def transit_response(total=600):
    return dict(
        status="OK",
        routes=[
            dict(
                properties=dict(totalTime=total, totalDistance=2000),
                steps=[
                    dict(
                        properties=dict(type="WALKING", time=60),
                        path=dict(points=[[127.0, 37.5], [127.001, 37.5]]),
                    ),
                    dict(
                        properties=dict(type="SUBWAY", time=480),
                        path=dict(points=[[127.001, 37.5], [127.02, 37.5]]),
                    ),
                    dict(
                        properties=dict(type="WALKING", time=60),
                        path=dict(points=[[127.02, 37.5], [127.021, 37.5]]),
                    ),
                ],
            )
        ],
    )


def demo_network():
    nodes = {
        k: dict(latitude=37.5, longitude=127.0 + i * 0.005, name=k)
        for i, k in enumerate(["origin", "a", "b", "c", "d", "destination"])
    }
    return dict(
        nodes=nodes,
        metadata={},
        edges=[
            dict(mode="walk", **{"from": "origin", "to": "a"}, minutes=1),
            dict(mode="bicycle", **{"from": "a", "to": "b"}, eta_from="1", eta_to="2"),
            dict(
                mode="transit",
                **{"from": "b", "to": "c"},
                minutes=7,
                steps=[],
                boardings=1,
            ),
            dict(mode="bicycle", **{"from": "c", "to": "d"}, eta_from="3", eta_to="4"),
            dict(mode="walk", **{"from": "d", "to": "destination"}, minutes=1),
            dict(mode="walk", **{"from": "origin", "to": "destination"}, minutes=50),
        ],
    )


class RoutingTests(unittest.TestCase):
    def test_two_rentals_and_actual_departure(self):
        departures = []

        def bike(edge, at):
            departures.append(at)
            return dict(
                minutes=3,
                p_empty=None,
                route=dict(geometry=None),
                model=dict(od_history_count=7),
            )

        r = search(demo_network(), bike, AT, KakaoConfig(), progress=None)
        self.assertEqual(r["routes"][0]["bike_rentals"], 2)
        self.assertEqual(r["routes"][0]["total_minutes"], 15)
        self.assertIn("2026-10-15T08:11:00+09:00", departures)
        self.assertFalse(r["whole_route_p80"])
        self.assertEqual(
            r["routes"][0]["legs"][1]["details"]["model"]["od_history_count"], 7
        )

    def test_walking_wins(self):
        n = demo_network()
        n["edges"][-1]["minutes"] = 2
        r = search(n, lambda *args: dict(minutes=3), AT, progress=None)
        self.assertEqual(r["routes"][0]["total_minutes"], 2)
        self.assertEqual(r["routes"][0]["bike_rentals"], 0)

    def test_api_total_not_step_sum_or_double_access(self):
        edges = transit_edges(transit_response(660), AT, "origin", "destination")
        self.assertEqual(edges[0]["minutes"], 11)
        self.assertEqual(edges[0]["step_minutes_sum"], 10)
        self.assertEqual(edges[0]["unexplained_minutes"], 1)
        n = dict(nodes={"origin": {}, "destination": {}}, edges=edges, metadata={})
        r = search(n, None, AT, progress=None)
        self.assertEqual(r["routes"][0]["total_minutes"], 11)
        self.assertEqual(r["routes"][0]["walking_minutes"], 2)

    def test_unknown_inventory_strict_and_bike_failure(self):
        strict = search(
            demo_network(),
            lambda *args: dict(minutes=3, p_empty=None),
            AT,
            KakaoConfig(require_known_inventory=True),
            progress=None,
        )
        self.assertEqual(strict["routes"][0]["total_minutes"], 50)

        def bad(*args):
            raise ValueError("private upstream details")

        failed = search(demo_network(), bad, AT, progress=None)
        self.assertEqual(failed["status"], "partial")
        self.assertEqual(failed["routes"][0]["total_minutes"], 50)
        self.assertNotIn("private upstream details", str(failed))

    def test_inventory_precheck_skips_expensive_eta(self):
        class Provider:
            calls = 0

            def precheck_inventory(self, edge, departure):
                return {"p_empty": 0.9}

            def __call__(self, edge, departure):
                self.calls += 1
                return {"minutes": 3, "p_empty": 0.9}

        provider = Provider()
        result = search(
            demo_network(),
            provider,
            AT,
            KakaoConfig(require_known_inventory=True, max_empty_probability=0.1),
            progress=None,
        )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(result["routes"][0]["bike_rentals"], 0)

    def test_direct_rerental_not_allowed(self):
        n = demo_network()
        n["edges"][2] = dict(
            mode="bicycle", **{"from": "b", "to": "c"}, eta_from="2", eta_to="3"
        )
        r = search(n, lambda *args: dict(minutes=1), AT, progress=None)
        self.assertEqual(r["routes"][0]["bike_rentals"], 0)

    def test_query_budgets_with_fake_client(self):
        class Client:
            stopped = None

            def __init__(self):
                self.calls = []

            def get(self, kind, a, b):
                self.calls.append(kind)
                if kind == "publictraffic":
                    return transit_response(), AT
                return dict(
                    status="OK",
                    route=dict(
                        properties=dict(totalTime=120, totalDistance=100), legs=[]
                    ),
                ), AT

            def stats(self):
                return dict(new_api_calls=len(self.calls))

        client = Client()
        origin = dict(latitude=37.5, longitude=127.0)
        destination = dict(latitude=37.5, longitude=127.021)
        stations = [
            dict(station_id=str(i), latitude=37.5, longitude=127.0 + i * 0.002)
            for i in range(1, 11)
        ]
        c = KakaoConfig(max_transit_queries=3, max_walk_queries=4)
        n = build_network(client, stations, origin, destination, c, progress=None)
        self.assertLessEqual(client.calls.count("walk"), 4)
        self.assertLessEqual(client.calls.count("publictraffic"), 3)
        self.assertTrue(any(e["mode"] == "bicycle" for e in n["edges"]))

    def test_walk_seconds_and_geojson_order(self):
        payload = dict(
            status="OK",
            route=dict(
                properties=dict(totalTime=90),
                legs=[
                    dict(steps=[dict(path=dict(points=[[127.0, 37.5], [127.1, 37.6]]))])
                ],
            ),
        )
        edge = walking_edge(payload, AT, "a", "b")
        self.assertEqual(edge["minutes"], 1.5)
        self.assertEqual(edge["geometry"]["coordinates"][0], [127.0, 37.5])


if __name__ == "__main__":
    unittest.main()
