"""Walking relays do not create new itineraries; valid timed actions survive."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import json
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import Config, Network, Router
from route_identity import distinct_routes, route_signature, walking_minutes
from web_server import compact_routes
from kakao_optimizer import map_html

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
    def test_same_line_different_departures_keep_only_fastest_arrival(self):
        first = dict(ride("A", "B", 2, 6, "first"), route_id="L")
        later = dict(ride("A", "B", 4, 7, "later"), route_id="L")
        slow = dict(
            total_minutes=8,
            legs=[
                walk("O", "A", 1),
                {"mode": "wait", "from": "A", "to": "A", "minutes": 3},
                later,
                walk("B", "Z", 1),
            ],
        )
        fast = dict(total_minutes=7, legs=[walk("O", "A", 1), first, walk("B", "Z", 1)])
        self.assertEqual(route_signature(slow["legs"]), route_signature(fast["legs"]))
        self.assertEqual(distinct_routes([slow, fast]), [fast])
        self.assertEqual(distinct_routes([fast, slow]), [fast])
        self.assertEqual(compact_routes(dict(routes=[slow, fast]))["routes"], [fast])
        other = dict(
            fast,
            legs=[walk("O", "A", 1), dict(first, route_id="OTHER"), walk("B", "Z", 1)],
        )
        self.assertEqual(len(distinct_routes([slow, fast, other])), 2)
        original = dict(routes=[slow, fast], mixed_routes=[other], nodes={})
        html = map_html(original)
        displayed, _ = json.JSONDecoder().raw_decode(html.split("const data=", 1)[1])
        self.assertEqual(len(displayed["routes"]), 2)
        self.assertEqual(displayed["routes"][0], fast)
        self.assertEqual(original["routes"], [slow, fast])
        changed_stop = dict(
            fast,
            legs=[walk("O", "C", 1), dict(first, **{"from": "C"}), walk("B", "Z", 1)],
        )
        self.assertNotEqual(
            route_signature(fast["legs"]), route_signature(changed_stop["legs"])
        )

    def test_different_transfer_path_survives_without_overtaking(self):
        def train(a, b, dep, arr, trip, line="L", mode="subway"):
            return dict(ride(a, b, dep, arr, trip), route_id=line, mode=mode)

        updates = []
        net = Network(
            nodes={k: {} for k in "OABCDEZ"},
            walks=[walk("O", "A", 1), walk("C", "Z", 4), walk("E", "Z", 1)],
            transit=[
                train("A", "B", 2, 3, "first"),
                train("B", "C", 3.5, 5, "first"),
                train("A", "B", 4, 5, "later"),
                train("B", "C", 5.5, 7, "later"),
                train("A", "D", 4, 5, "other", "OTHER"),
                train("D", "E", 5.5, 6, "bus", "BUS", "bus"),
            ],
        )
        result = Router(
            net,
            None,
            Config(top_k=3, max_seconds=2, horizon_minutes=15),
            on_routes=updates.append,
        ).search("O", "Z", START)
        self.assertEqual([r["total_minutes"] for r in result["routes"]], [7, 9])
        self.assertEqual(
            {
                leg["route_id"]
                for leg in result["routes"][0]["legs"]
                if leg["mode"] in {"subway", "bus"}
            },
            {"OTHER", "BUS"},
        )
        self.assertEqual(
            {
                leg["trip_id"]
                for leg in result["routes"][1]["legs"]
                if leg["mode"] == "subway"
            },
            {"first"},
        )
        for update in updates:
            keys = [route_signature(r["legs"]) for r in update["routes"]]
            self.assertEqual(len(keys), len(set(keys)))

    def test_opaque_legacy_transit_options_are_kept_distinct(self):
        a = [dict(mode="transit", **{"from": "O", "to": "Z"}, route_option="fast")]
        b = [
            dict(
                mode="transit", **{"from": "O", "to": "Z"}, route_option="few_transfers"
            )
        ]
        self.assertNotEqual(route_signature(a), route_signature(b))

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
