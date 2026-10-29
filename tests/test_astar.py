"""A* vs Dijkstra on identical synthetic networks and deterministic predictions."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import random
import unittest
from datetime import datetime
from multimodal import Config, Network, Router, relaxed_lower_bounds


def at(minute):
    return f"2026-10-16T08:{minute:02d}:00+09:00"


def predict(edge, departure):
    minute = datetime.fromisoformat(departure).minute
    return {"minutes": 0.2 if minute % 4 == 0 else 7.0, "p_empty": 0.0}


class AStarTests(unittest.TestCase):
    def search(self, n, algorithm, **kwargs):
        return Router(
            n,
            predict,
            Config(
                search_algorithm=algorithm,
                top_k=1,
                horizon_minutes=30,
                max_seconds=10,
                **kwargs,
            ),
        ).search("O", "Z", at(0))

    def test_lower_bound_does_not_assume_a_bicycle_speed(self):
        n = Network(
            {"O": {}, "A": {}, "Z": {}},
            bikes=[{"from": "O", "to": "A"}],
            walks=[{"from": "A", "to": "Z", "minutes": 2}],
        )
        h = relaxed_lower_bounds(n, "Z")
        self.assertEqual(h["O"], 120)
        self.assertEqual(h["A"], 120)
        self.assertEqual(h["Z"], 0)
        for algorithm in ("astar", "dijkstra"):
            r = self.search(n, algorithm)
            self.assertAlmostEqual(r["routes"][0]["total_minutes"], 2.2)
            self.assertAlmostEqual(r["routes"][0]["score_minutes"], 2.2)

    def test_future_train_wait_is_not_counted_in_lower_bound(self):
        n = Network(
            {"O": {}, "Z": {}},
            transit=[
                {
                    "from": "O",
                    "to": "Z",
                    "mode": "subway",
                    "trip_id": "T",
                    "departure": at(20),
                    "arrival": at(25),
                }
            ],
        )
        self.assertEqual(relaxed_lower_bounds(n, "Z")["O"], 300)
        for algorithm in ("astar", "dijkstra"):
            self.assertEqual(
                self.search(n, algorithm)["routes"][0]["total_minutes"], 25
            )

    def test_optimistic_bike_bound_keeps_non_fifo_waiting(self):
        n = Network({"O": {}, "Z": {}}, bikes=[{"from": "O", "to": "Z"}])

        # Override the 08:00 fast case so the best departure is later.
        def model(e, t):
            return {"minutes": 1 if t == at(4) else 20}

        answers = []
        for algorithm in ("astar", "dijkstra"):
            r = Router(
                n,
                model,
                Config(search_algorithm=algorithm, top_k=1, horizon_minutes=30),
            ).search("O", "Z", at(0))
            answers.append(r["routes"][0]["total_minutes"])
        self.assertEqual(answers, [5, 5])

    def test_goal_directed_priority_avoids_unpromising_branches(self):
        nodes = {key: {} for key in ["O", "Z", *[f"B{i}" for i in range(40)]]}
        walks = [{"from": "O", "to": "Z", "minutes": 20}]
        for i in range(40):
            walks.extend(
                [
                    {"from": "O", "to": f"B{i}", "minutes": 1},
                    {"from": f"B{i}", "to": "Z", "minutes": 100},
                ]
            )
        n = Network(nodes, walks=walks)
        a, d = self.search(n, "astar"), self.search(n, "dijkstra")
        self.assertEqual(
            a["routes"][0]["total_minutes"], d["routes"][0]["total_minutes"]
        )
        self.assertLess(a["expanded_states"], d["expanded_states"])
        self.assertEqual(a["routes"][0]["score_minutes"], 20)

    def test_missing_reverse_path_falls_back_without_error(self):
        n = Network(
            {"O": {}, "X": {}, "Z": {}}, walks=[{"from": "O", "to": "X", "minutes": 1}]
        )
        r = self.search(n, "astar")
        self.assertEqual(r["status"], "no_route")
        self.assertEqual(r["heuristic_origin_minutes"], 0)

    def test_random_graphs_match_best_arrival_without_truncation(self):
        for seed in range(40):
            rng = random.Random(seed)
            names = ["O", "A", "B", "C", "D", "Z"]
            walks = [
                {"from": a, "to": b, "minutes": rng.randint(1, 4)}
                for a, b in zip(names, names[1:])
            ]
            bikes = []
            transit = []
            for a in names[:-1]:
                for b in names:
                    if a == b:
                        continue
                    if rng.random() < 0.15:
                        walks.append({"from": a, "to": b, "minutes": rng.randint(1, 6)})
                    if rng.random() < 0.12:
                        bikes.append({"from": a, "to": b})
                    if rng.random() < 0.1:
                        dep = rng.randint(1, 18)
                        arr = dep + rng.randint(1, 5)
                        transit.append(
                            {
                                "from": a,
                                "to": b,
                                "mode": "bus",
                                "trip_id": f"{a}-{b}",
                                "departure": at(dep),
                                "arrival": at(arr),
                            }
                        )
            n = Network(
                {name: {} for name in names}, walks=walks, bikes=bikes, transit=transit
            )
            a, d = self.search(n, "astar"), self.search(n, "dijkstra")
            with self.subTest(seed=seed):
                self.assertTrue(a["search_complete"])
                self.assertTrue(d["search_complete"])
                self.assertEqual(a["routes"][0]["arrival"], d["routes"][0]["arrival"])
                self.assertEqual(
                    a["routes"][0]["score_minutes"], d["routes"][0]["score_minutes"]
                )


if __name__ == "__main__":
    unittest.main()
