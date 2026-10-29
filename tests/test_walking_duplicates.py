"""Walking relays do not create new itineraries; valid timed actions survive."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import Config, Network, Router
from route_identity import distinct_routes, route_signature, walking_minutes
from web_server import compact_routes

START = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)


def at(minutes):
    return (START + timedelta(minutes=minutes)).isoformat()


def walk(a, b, minutes):
    return {"mode": "walk", "from": a, "to": b, "minutes": minutes}


def ride(a, b, dep, arr, trip):
    return {
        "mode": "subway",
        "from": a,
        "to": b,
        "departure": at(dep),
        "arrival": at(arr),
        "trip_id": trip,
    }


def run(nodes, walks=(), transit=(), bikes=(), provider=None):
    return Router(
        Network(
            nodes={k: {} for k in nodes},
            walks=list(walks),
            transit=list(transit),
            bikes=list(bikes),
        ),
        provider,
        Config(top_k=3, max_seconds=2, horizon_minutes=10),
    ).search("O", "Z", START)


class WalkingDuplicateTests(unittest.TestCase):
    def test_same_train_and_walk_endpoints_share_one_identity(self):
        train = ride("A", "B", 2, 3, "T")
        direct = [walk("O", "A", 1), train, walk("B", "Z", 2)]
        relay = [walk("O", "X", 0.5), walk("X", "A", 1), train, walk("B", "Z", 2)]
        loop = [
            walk("O", "A", 1),
            train,
            walk("B", "P", 0.8),
            walk("P", "B", 0.83),
            walk("B", "Z", 2),
        ]
        self.assertEqual(route_signature(direct), route_signature(relay))
        self.assertEqual(route_signature(direct), route_signature(loop))
        self.assertNotEqual(
            route_signature(direct),
            route_signature(
                [walk("O", "A", 1), ride("A", "B", 2, 3, "other"), walk("B", "Z", 2)]
            ),
        )

    def test_shortest_walk_wins_when_same_vehicle_arrival_hides_detour(self):
        train = ride("A", "B", 4, 5, "T")
        short = dict(
            total_minutes=7, legs=[walk("O", "A", 1), train, walk("B", "Z", 2)]
        )
        long = dict(
            total_minutes=7,
            legs=[walk("O", "X", 1), walk("X", "A", 2), train, walk("B", "Z", 2)],
        )
        self.assertEqual(distinct_routes([long, short]), [short])
        self.assertEqual(compact_routes(dict(routes=[long, short]))["routes"], [short])

    def test_router_keeps_one_pure_walk_itinerary(self):
        result = run(
            "OAXZ",
            [
                walk("O", "A", 1),
                walk("A", "Z", 2),
                walk("A", "X", 1),
                walk("X", "Z", 1.5),
            ],
        )
        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(result["routes"][0]["total_minutes"], 3)

    def test_walk_out_and_back_does_not_fill_top_three(self):
        result = run(
            "OABPZ",
            [
                walk("O", "A", 1),
                walk("B", "Z", 2),
                walk("B", "P", 0.8),
                walk("P", "B", 0.83),
            ],
            [ride("A", "B", 2, 3, "T")],
        )
        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(result["routes"][0]["total_minutes"], 5)
        self.assertFalse(
            any(leg.get("to") == "P" for leg in result["routes"][0]["legs"])
        )
        self.assertGreater(result["walking_roundtrip_replacements"], 0)

    def test_different_transfer_stops_remain_different_itineraries(self):
        result = run(
            "OABZ",
            [walk("A", "Z", 1), walk("B", "Z", 2)],
            [ride("O", "A", 0, 2, "T"), ride("A", "B", 2, 3, "T")],
        )
        self.assertEqual(len(result["routes"]), 2)

    def test_walking_transfer_through_a_relay_is_retained(self):
        result = run(
            "OAXBZ",
            [walk("A", "X", 0.25), walk("X", "B", 0.25)],
            [ride("O", "A", 0, 2, "T1"), ride("B", "Z", 3, 4, "T2")],
        )
        self.assertEqual(result["routes"][0]["total_minutes"], 4)
        self.assertEqual(walking_minutes(result["routes"][0]), 0.5)

    def test_cycle_wait_preserves_non_grid_inventory_opportunity(self):
        def provider(edge, departure):
            return {"minutes": 1 if departure == at(0.75) else 100, "p_empty": 0}

        result = run(
            "OABZ",
            [walk("O", "A", 0.1), walk("A", "B", 0.3), walk("B", "A", 0.35)],
            bikes=[{"from": "A", "to": "Z"}],
            provider=provider,
        )
        route = result["routes"][0]
        self.assertAlmostEqual(route["total_minutes"], 1.75)
        self.assertTrue(
            any(
                leg.get("source") == "walking_roundtrip_replaced_by_wait"
                for leg in route["legs"]
            )
        )
        self.assertAlmostEqual(walking_minutes(route), 0.1)

    def test_bicycle_re_rental_state_is_not_lost(self):
        result = run(
            "OABZ",
            [walk("A", "B", 0.2), walk("B", "A", 0.2)],
            bikes=[{"from": "O", "to": "A"}, {"from": "A", "to": "Z"}],
            provider=lambda e, t: {"minutes": 1, "p_empty": 0},
        )
        route = result["routes"][0]
        self.assertAlmostEqual(route["total_minutes"], 2.4)
        self.assertEqual(route["bike_rentals"], 2)

    def test_distinct_bicycle_ods_are_not_merged(self):
        a = [
            walk("O", "A", 1),
            {"mode": "bicycle", "from": "A", "to": "B"},
            walk("B", "Z", 1),
        ]
        b = [
            walk("O", "C", 1),
            {"mode": "bicycle", "from": "C", "to": "B"},
            walk("B", "Z", 1),
        ]
        self.assertNotEqual(route_signature(a), route_signature(b))
