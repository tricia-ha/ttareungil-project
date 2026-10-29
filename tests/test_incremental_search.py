"""No API/model calls: fair scheduling, retained candidates and shared walk cache."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import Network, Router, Config
from candidate_edges import LazyConnections

START = "2026-10-22T10:00:00+09:00"


class IncrementalSearchTests(unittest.TestCase):
    def test_follows_mixed_route_before_finishing_large_station_fanout(self):
        nodes = {
            k: {} for k in ["O", "A", "B", "S", "Z"] + [f"D{i}" for i in range(30)]
        }
        net = Network(
            nodes,
            walks=[
                {"from": "O", "to": "A", "minutes": 0.1},
                {"from": "B", "to": "S", "minutes": 0.1},
            ]
            + [{"from": f"D{i}", "to": "Z", "minutes": 20} for i in range(30)],
            bikes=[{"from": "A", "to": "B", "test_minutes": 1}]
            + [{"from": "A", "to": f"D{i}", "test_minutes": 30} for i in range(30)],
            transit=[
                {
                    "from": "S",
                    "to": "Z",
                    "mode": "subway",
                    "trip_id": "T1",
                    "departure": "2026-10-22T10:03:00+09:00",
                    "arrival": "2026-10-22T10:04:00+09:00",
                }
            ],
        )
        clock = [0.0]
        calls = []

        def predict(edge, departure):
            clock[0] += 0.2
            calls.append(edge["to"])
            return {"minutes": edge["test_minutes"], "p_empty": 0.0}

        with patch("multimodal.time.monotonic", side_effect=lambda: clock[0]):
            result = Router(
                net,
                predict,
                Config(top_k=1, max_seconds=1.1, bike_expansion_batch_size=1),
            ).search("O", "Z", START)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["routes"][0]["bike_rentals"], 1)
        self.assertEqual(result["routes"][0]["transit_boardings"], 1)
        self.assertEqual(result["routes"][0]["total_minutes"], 4)
        self.assertLess(len(calls), 31)
        self.assertGreater(result["pending_work_items"], 0)

    def test_later_candidate_is_not_deleted_or_ignored_after_first_goal(self):
        targets = [f"B{i}" for i in range(12)]
        net = Network(
            {k: {} for k in ["O", "Z"] + targets},
            walks=[{"from": b, "to": "Z", "minutes": 1} for b in targets],
            bikes=[
                {"from": "O", "to": b, "test_minutes": 1 if b == targets[-1] else 5}
                for b in targets
            ],
        )
        seen = set()

        def predict(edge, departure):
            seen.add(edge["to"])
            return {"minutes": edge["test_minutes"], "p_empty": 0.0}

        result = Router(
            net,
            predict,
            Config(top_k=1, horizon_minutes=10, bike_expansion_batch_size=1),
        ).search("O", "Z", START)
        self.assertEqual(seen, set(targets))
        self.assertEqual(result["routes"][0]["total_minutes"], 2)
        self.assertEqual(result["routes"][0]["legs"][0]["to"], targets[-1])
        self.assertTrue(result["search_complete"])

    def test_interleaved_walk_states_share_lookups_but_receive_every_edge(self):
        nodes = {
            k: {"latitude": 37.5 + i * 0.001, "longitude": 127.0}
            for i, k in enumerate(("origin", "A", "B"))
        }
        calls = []

        def walking(a, b, start, end):
            calls.append((start, end))
            return {"from": start, "to": end, "minutes": 1}

        lazy = LazyConnections(
            Network(nodes, walk_policy={"max_distance_m": 900}), walking
        )
        first = lazy.iter_walks_from("origin")
        second = lazy.iter_walks_from("origin")
        self.assertEqual(next(first)["to"], "A")
        self.assertEqual(len(calls), 1)
        self.assertEqual(next(second)["to"], "A")
        self.assertEqual(len(calls), 1)
        self.assertEqual(next(second)["to"], "B")
        self.assertEqual(next(first)["to"], "B")
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(first), [])
        self.assertEqual(list(second), [])

    def test_quantum_must_be_positive_integer(self):
        for value in (0, -1, 1.5):
            with self.assertRaises(ValueError):
                Router(
                    Network({"O": {}, "Z": {}}),
                    None,
                    Config(bike_expansion_batch_size=value),
                ).search("O", "Z", START)

    def test_random_timetables_match_independent_best_arrival(self):
        import random
        from datetime import datetime, timedelta

        rng = random.Random(41)
        start = datetime.fromisoformat(START)
        for case in range(12):
            net = Network(
                {"O": {}, "Z": {}}, walks=[{"from": "O", "to": "Z", "minutes": 30}]
            )
            expected = 30
            for i in range(4):
                a, b, s = f"A{i}", f"B{i}", f"S{i}"
                net.nodes.update({a: {}, b: {}, s: {}})
                duration = rng.randint(1, 10)
                departure = rng.randint(4, 18)
                net.walks.extend(
                    [
                        {"from": "O", "to": a, "minutes": 1},
                        {"from": b, "to": s, "minutes": 1},
                    ]
                )
                net.bikes.append({"from": a, "to": b, "test_minutes": duration})
                net.transit.append(
                    {
                        "from": s,
                        "to": "Z",
                        "mode": "bus",
                        "trip_id": s,
                        "departure": (start + timedelta(minutes=departure)).isoformat(),
                        "arrival": (
                            start + timedelta(minutes=departure + 5)
                        ).isoformat(),
                    }
                )
                if duration + 2 <= departure:
                    expected = min(expected, departure + 5)
            for quantum in (1, 2, 8):
                with self.subTest(case=case, quantum=quantum):
                    result = Router(
                        net,
                        lambda e, t: {"minutes": e["test_minutes"]},
                        Config(
                            top_k=1,
                            horizon_minutes=35,
                            bike_expansion_batch_size=quantum,
                        ),
                    ).search("O", "Z", START)
                    self.assertEqual(result["routes"][0]["total_minutes"], expected)
                    self.assertTrue(result["search_complete"])


if __name__ == "__main__":
    unittest.main()
