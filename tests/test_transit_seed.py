import random
import sys
import time
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import Config, Network, Router, timestamp
from transit_seed import timetable_seed

START = datetime.fromisoformat("2026-10-30T10:00:00+09:00")


def event(a, b, leave, arrive, trip="T", mode="bus", **flags):
    return dict(
        **{"from": a, "to": b},
        departure=(START + timedelta(minutes=leave)).isoformat(),
        arrival=(START + timedelta(minutes=arrive)).isoformat(),
        trip_id=trip,
        mode=mode,
        **flags,
    )


def scan(net, mode=None):
    rides = defaultdict(list)
    for e in net.transit:
        rides[e["from"]].append((timestamp(e["departure"]), timestamp(e["arrival"]), e))
    for rows in rides.values():
        rows.sort(key=lambda r: r[0])
    return timetable_seed(
        net.walks,
        rides,
        "O",
        "D",
        START.timestamp(),
        START.timestamp() + 7200,
        START.tzinfo,
        mode,
        time.monotonic() + 5,
    )[0]


class TransitSeedTests(unittest.TestCase):
    def test_early_walk_arrival_can_wait_for_later_service(self):
        net = Network(
            {k: {} for k in ("O", "A", "B", "D")},
            walks=[
                {"from": "O", "to": "A", "minutes": 1},
                {"from": "O", "to": "B", "minutes": 3},
                {"from": "B", "to": "A", "minutes": 2},
            ],
            transit=[event("A", "D", 10, 15)],
        )
        route = scan(net, "transit")
        self.assertEqual(route["total_minutes"], 15)
        self.assertEqual(route["legs"][1]["wait_minutes"], 9)

    def test_destination_is_not_used_as_offboard_roundtrip(self):
        net = Network(
            {k: {} for k in ("O", "A", "D")},
            walks=[
                {"from": "O", "to": "D", "minutes": 1},
                {"from": "A", "to": "D", "minutes": 1},
            ],
            transit=[event("D", "A", 2, 3)],
        )
        self.assertIsNone(scan(net, "transit"))

    def test_boarding_buffer_and_no_pickup_through_service(self):
        net = Network(
            {k: {} for k in ("O", "A", "B", "C", "D")},
            walks=[
                {"from": "O", "to": "A", "minutes": 1},
                {"from": "C", "to": "D", "minutes": 2},
            ],
            transit=[
                event("A", "B", 2, 5, boarding_seconds=30, dropoff_allowed=False),
                event("B", "C", 6, 10, pickup_allowed=False),
            ],
        )
        route = scan(net, "transit")
        self.assertEqual(route["total_minutes"], 12)
        self.assertEqual(route["transit_boardings"], 1)
        net.transit[0]["boarding_seconds"] = 90
        self.assertIsNone(scan(net, "transit"))

    def test_walking_transfers_waiting_and_required_mode(self):
        net = Network(
            {k: {} for k in ("O", "A", "B", "C", "D")},
            walks=[
                {"from": "O", "to": "A", "minutes": 1},
                {"from": "B", "to": "C", "minutes": 2},
            ],
            transit=[event("A", "B", 2, 5), event("C", "D", 8, 12, "S", "subway")],
        )
        r = scan(net, "subway")
        self.assertEqual(r["total_minutes"], 12)
        self.assertEqual(r["transit_boardings"], 2)
        self.assertAlmostEqual(sum(e["minutes"] for e in r["legs"]), 12)
        net.transit[-1]["dropoff_allowed"] = False
        self.assertIsNone(scan(net))

    def test_walk_only_does_not_satisfy_required_transit(self):
        net = Network(
            {k: {} for k in ("O", "D")}, walks=[{"from": "O", "to": "D", "minutes": 4}]
        )
        self.assertEqual(scan(net)["total_minutes"], 4)
        self.assertIsNone(scan(net, "transit"))

    def test_random_small_networks_match_exhaustive_router_arrival(self):
        for seed in range(20):
            rng = random.Random(seed)
            names = ["O", "A", "B", "C", "D"]
            edges = [
                event(*rng.sample(names, 2), leave, leave + rng.randint(1, 5), f"T{i}")
                for i in range(15)
                for leave in [rng.randint(0, 20)]
            ]
            walks = [
                {"from": a, "to": b, "minutes": rng.randint(1, 4)}
                for a, b in (rng.sample(names, 2) for _ in range(5))
            ]
            net = Network({k: {} for k in names}, walks=walks, transit=edges)
            for mode in (None, "transit"):
                expected = Router(
                    net, None, Config(top_k=1, required_mode=mode, horizon_minutes=120)
                ).search("O", "D", START)["routes"]
                actual = scan(net, mode)
                self.assertEqual(bool(actual), bool(expected), (seed, mode))
                if actual:
                    self.assertEqual(
                        actual["total_minutes"],
                        expected[0]["total_minutes"],
                        (seed, mode),
                    )

    def test_dense_wait_states_cannot_starve_seed(self):
        nodes = {
            k: {"latitude": 37.5, "longitude": 127.0, "kind": "stop"}
            for k in ("O", "A", "B", "D")
        }
        nodes.update(
            {
                f"X{i}": {"latitude": 40 + i * 0.02, "longitude": 127.0, "kind": "stop"}
                for i in range(300)
            }
        )
        for i, key in enumerate(("O", "A", "B", "D")):
            nodes[key]["latitude"] = 37 + i * 0.02
        net = Network(
            nodes,
            walks=[{"from": "O", "to": "A", "minutes": 10}]
            + [{"from": "O", "to": f"X{i}", "minutes": 1} for i in range(300)]
            + [{"from": "B", "to": "D", "minutes": 10}],
            transit=[event("A", "B", 20, 60)],
            walk_policy={"max_distance_m": 900},
        )
        # No new walking API access is possible; confirmed links suffice.
        from kakao_routes import CallLimit

        def blocked(*args):
            raise CallLimit("test")

        result = Router(
            net, None, Config(top_k=1, max_expansions=100), walking_provider=blocked
        ).search("O", "D", START)
        self.assertEqual(result["routes"][0]["total_minutes"], 70)
        self.assertTrue(result["timetable_seed_scans"][0]["found"])


if __name__ == "__main__":
    unittest.main()
