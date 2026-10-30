"""Retain pure and mixed routes, middle bicycle transfers and repeat rentals."""

import sys
import unittest
import random
import tempfile
import json, hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import Config, Network, Router
from candidate_edges import LazyConnections
from multimodal_cache import CachedBikeProvider

START = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)


def at(minutes):
    return (START + timedelta(minutes=minutes)).isoformat()


def walk(a, b, minutes):
    return {"from": a, "to": b, "minutes": minutes}


def ride(a, b, departure, arrival, trip):
    return {
        "from": a,
        "to": b,
        "departure": at(departure),
        "arrival": at(arrival),
        "mode": "subway",
        "trip_id": trip,
    }


def eta(edge, departure):
    return {"minutes": edge.get("test_minutes", 1.0), "p_empty": 0.0}


class IntegratedPreservationTests(unittest.TestCase):
    def search(self, network, preserve=True):
        return Router(
            network,
            eta,
            Config(
                top_k=1,
                mixed_top_k=1,
                preserve_mixed_routes=preserve,
                horizon_minutes=10,
                max_seconds=10,
                max_expansions=100000,
            ),
        ).search("O", "Z", START)

    def test_slower_mixed_route_survives_fast_transit_top_one(self):
        n = Network(
            nodes={k: {} for k in ("O", "S", "A", "B", "C", "D", "Z")},
            walks=[walk("O", "S", 0.25), walk("A", "B", 0.25), walk("C", "D", 0.25)],
            bikes=[{"from": "B", "to": "C"}],
            transit=[
                ride("S", "Z", 1, 2, "fast"),
                ride("S", "A", 1, 2, "first"),
                ride("D", "Z", 4, 5, "last"),
            ],
        )
        before = self.search(n, False)
        after = self.search(n)
        self.assertEqual(
            before["routes"][0]["total_minutes"], after["routes"][0]["total_minutes"]
        )
        self.assertEqual(after["routes"][0]["total_minutes"], 2.0)
        mixed = after["mixed_routes"][0]
        self.assertEqual(mixed["total_minutes"], 5.0)
        self.assertEqual(
            [l["mode"] for l in mixed["legs"] if l["mode"] not in ("alight", "wait")],
            ["walk", "subway", "walk", "bicycle", "walk", "subway"],
        )
        self.assertTrue(after["search_complete"])

    def test_same_time_transit_state_keeps_bicycle_history(self):
        n = Network(
            nodes={k: {} for k in ("O", "A", "B", "X", "Z")},
            walks=[walk("O", "X", 2.0), walk("O", "A", 0.25), walk("B", "X", 1.25)],
            bikes=[{"from": "A", "to": "B", "test_minutes": 0.5}],
            transit=[ride("X", "Z", 3, 4, "shared")],
        )
        r = self.search(n)
        self.assertEqual(r["routes"][0]["total_minutes"], 4.0)
        self.assertEqual(r["mixed_routes"][0]["total_minutes"], 4.0)
        self.assertEqual(r["mixed_routes"][0]["bike_rentals"], 1)

    def test_two_rentals_on_opposite_sides_of_transit_are_retained(self):
        n = Network(
            nodes={k: {} for k in ("O", "A", "B", "S", "T", "C", "D", "Z")},
            walks=[
                walk("O", "A", 0.25),
                walk("B", "S", 0.25),
                walk("T", "C", 0.25),
                walk("D", "Z", 0.25),
            ],
            bikes=[
                {"from": "A", "to": "B", "test_minutes": 0.5},
                {"from": "C", "to": "D", "test_minutes": 0.5},
            ],
            transit=[ride("S", "T", 2, 3, "middle")],
        )
        r = self.search(n)
        self.assertEqual(r["mixed_routes"][0]["bike_rentals"], 2)
        self.assertEqual(r["mixed_routes"][0]["total_minutes"], 4.0)

    def test_candidate_reordering_preserves_every_directed_pair(self):
        nodes = {"destination": dict(latitude=37.54, longitude=127.0, kind="endpoint")}
        for i in range(35):
            nodes[str(i)] = dict(
                latitude=37.50 + i * 0.001,
                longitude=127.0 + (i % 3) * 0.002,
                station_id=str(i),
                kind="bike",
            )
        nodes["S"] = dict(latitude=37.506, longitude=127.0, kind="stop")
        base = Network(
            nodes=nodes,
            transit=[{}],
            bike_policy={"max_distance_m": 7000},
            walk_policy={"max_distance_m": 400},
        )
        ordered = Network(
            nodes=nodes,
            transit=[{}],
            bike_policy={"max_distance_m": 7000, "prioritize_transfers": True},
            walk_policy={"max_distance_m": 400},
        )
        a, b = LazyConnections(base), LazyConnections(ordered)
        for origin in ("0", "12", "34"):
            before = a.bikes_from(origin)
            after = b.bikes_from(origin)
            self.assertEqual(
                {(e["from"], e["to"]) for e in before},
                {(e["from"], e["to"]) for e in after},
            )
            self.assertEqual(len(after), len(before))

    def test_time_dependent_cases_keep_legacy_best_and_a_mixed_route(self):
        randomizer = random.Random(1027)
        for _ in range(30):
            direct = randomizer.randint(3, 9)
            first = randomizer.randint(1, 7)
            later = randomizer.randint(1, 5)

            def timed_eta(edge, departure):
                minute = (
                    datetime.fromisoformat(departure) - START
                ).total_seconds() / 60
                return {"minutes": first if minute < 3 else later, "p_empty": 0.0}

            n = Network(
                nodes={k: {} for k in ("O", "A", "B", "X", "S", "C", "D", "Z")},
                walks=[
                    walk("O", "A", 0.25),
                    walk("B", "X", 0.25),
                    walk("S", "C", 0.25),
                    walk("D", "Z", 0.25),
                ],
                bikes=[{"from": "A", "to": "B"}, {"from": "C", "to": "D"}],
                transit=[
                    ride("O", "Z", 0.1, direct, "direct"),
                    ride("X", "Z", 7, 9, "late"),
                    ride("O", "S", 1, 2, "first"),
                ],
            )

            def run(preserve):
                return Router(
                    n,
                    timed_eta,
                    Config(
                        top_k=1,
                        preserve_mixed_routes=preserve,
                        horizon_minutes=15,
                        max_seconds=10,
                    ),
                ).search("O", "Z", START)

            before, after = run(False), run(True)
            self.assertAlmostEqual(
                before["routes"][0]["total_minutes"],
                after["routes"][0]["total_minutes"],
            )
            self.assertTrue(after["mixed_routes"])
            self.assertTrue(after["mixed_routes"][0]["bike_rentals"])
            self.assertTrue(after["mixed_routes"][0]["transit_boardings"])

    def test_identity_memo_keeps_original_hash_and_detects_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CachedBikeProvider(
                None, Path(directory) / "cache.sqlite", {"model": "same"}
            )
            try:
                edge = {"from": "A", "to": "B", "distance": 1}
                for value in (1, 1.0, True, "1", {"nested": [1, 2]}, 2):
                    edge["distance"] = value
                    key, bucket = cache._identity(edge, at(1))
                    expected = hashlib.sha256(
                        (
                            cache.context + json.dumps([edge, bucket], sort_keys=True)
                        ).encode()
                    ).hexdigest()
                    self.assertEqual(key, expected)
                    self.assertEqual(cache._identity(edge, at(2))[0], expected)
            finally:
                cache.close()


if __name__ == "__main__":
    unittest.main()
