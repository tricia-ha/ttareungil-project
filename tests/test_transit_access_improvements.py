"""Posted schedules, station-entry context and expanded connector regressions."""

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import json
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import Config, Network, Router, timestamp
from rail_access import transfer_walks
from transit_seed import timetable_seed
from candidate_edges import adaptive_radii, adaptive_walk_policy, LazyConnections
from bus_web_timetables import matrix, published_airport_network, collect
import requests
from scheduled_runtime import build_scheduled_network, ScheduledConfig
from bus_section_times import trip_offsets, route_profiles
from transit_schedule import reconstruct_bus

START = datetime.fromisoformat("2026-11-02T08:00:00+09:00")


def event(a, b, leave, arrive, trip, line="1호선"):
    return dict(
        **{"from": a, "to": b},
        departure=(START + timedelta(minutes=leave)).isoformat(),
        arrival=(START + timedelta(minutes=arrive)).isoformat(),
        trip_id=trip,
        route_id=line,
        mode="subway",
        boarding_seconds=120,
    )


def scan(network):
    rides = defaultdict(list)
    for e in network.transit:
        rides[e["from"]].append((timestamp(e["departure"]), timestamp(e["arrival"]), e))
    for rows in rides.values():
        rows.sort(key=lambda x: x[0])
    return timetable_seed(
        network.walks,
        rides,
        "O",
        "D",
        START.timestamp(),
        START.timestamp() + 3600,
        START.tzinfo,
        "transit",
        time.monotonic() + 3,
    )[0]


class AccessTests(unittest.TestCase):
    def test_blocked_collection_preserves_last_good_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "published.json"
            output.write_bytes(b'{"last_good": true}')
            with patch("bus_web_timetables.requests.Session") as factory:
                factory.return_value.get.side_effect = requests.HTTPError("403")
                with self.assertRaises(requests.HTTPError):
                    collect(root / "cache", output)
            self.assertEqual(output.read_bytes(), b'{"last_good": true}')
            self.assertEqual(factory.return_value.get.call_count, 1)

    def test_section_hour_changes_at_midnight_and_missing_values_fall_back(self):
        friday = datetime.fromisoformat("2026-11-06").date()
        first = [None] * 24
        first[23] = 90
        second = [None] * 24
        second[0] = 40
        profiles = [{"weekday": first}, {"saturday": second}, {}]
        values, observed = trip_offsets(
            profiles, friday, 86370, [0, 60, 120, 180], set()
        )
        self.assertEqual(values, [0, 90, 130, 190])
        self.assertEqual(observed, 2)
        _, bound = route_profiles(
            {"sections": {"r|a|b|1|2": {"weekday": first}}}, "r", ["a", "b"], [0, 60]
        )
        self.assertEqual(bound, 90)

    def test_longer_sections_retain_a_trip_dispatched_before_the_old_lookback(self):
        route = dict(
            노선ID="r",
            노선명="r",
            운행소요시간="2",
            배차="120",
            첫차시간="0.0800",
            막차시간="0.0800",
        )
        stops = {
            k: dict(name=k, latitude=37.5, longitude=127 + i * 0.001)
            for i, k in enumerate("abcd")
        }
        rows = [
            dict(노선ID="r", 노드ID=k, 정류장순번=str(i + 1), 링크거리누계="100")
            for i, k in enumerate("abcd")
        ]
        data = dict(
            valid_from="2026-11-01",
            sections={
                f"r|{a}|{b}|{i}|{i + 1}": {"weekday": [600] * 24}
                for i, (a, b) in enumerate(zip("abcd", "bcd"), 1)
            },
        )
        net, _ = reconstruct_bus(
            [route],
            rows,
            stops,
            "2026-11-02T08:15:00+09:00",
            30,
            holiday_dates=set(),
            section_times=data,
        )
        self.assertTrue(
            any(
                e["to"] == "bus:d" and e["arrival"] == "2026-11-02T08:30:00+09:00"
                for e in net.transit
            )
        )
        old, _ = reconstruct_bus(
            [route], rows, stops, START.isoformat(), 30, holiday_dates=set()
        )
        missing, _ = reconstruct_bus(
            [route],
            rows,
            stops,
            START.isoformat(),
            30,
            holiday_dates=set(),
            section_times=dict(valid_from="2026-11-01", sections={}),
        )
        self.assertEqual(old.transit, missing.transit)

    def test_posted_origin_times_do_not_invent_a_lunchtime_departure(self):
        route = dict(
            노선ID="r",
            노선명="r",
            운행소요시간="30",
            배차="15",
            첫차시간="0.0900",
            막차시간="0.1615",
        )
        rows = [
            dict(노선ID="r", 노드ID=k, 정류장순번=str(i + 1), 링크거리누계="100")
            for i, k in enumerate("ab")
        ]
        stops = {
            k: dict(name=k, latitude=37.5, longitude=127 + i * 0.001)
            for i, k in enumerate("ab")
        }
        posted = {
            "r": dict(
                valid_from="2026-11-01",
                weekday=[
                    9 * 3600,
                    10 * 3600 + 900,
                    11 * 3600 + 1800,
                    13 * 3600 + 2700,
                    15 * 3600,
                    16 * 3600 + 900,
                ],
                runtime_seconds=7200,
            )
        }
        net, _ = reconstruct_bus(
            [route],
            rows,
            stops,
            "2026-11-02T12:00:00+09:00",
            240,
            holiday_dates=set(),
            published_dispatches=posted,
        )
        self.assertTrue(net.transit)
        self.assertTrue(
            all(e["departure"] == "2026-11-02T13:45:00+09:00" for e in net.transit)
        )

    def network(self, entry=False):
        nodes = {
            k: dict(
                name="시청" if k in ["A", "B"] else k, latitude=37.56, longitude=126.97
            )
            for k in ["O", "S", "A", "B", "D"]
        }
        walks = [dict(**{"from": "O", "to": "S"}, minutes=1)]
        rides = [
            event("S", "A", 4, 10, "first"),
            event("B", "D", 12, 14, "next", "2호선"),
            event("B", "D", 22, 24, "later", "2호선"),
        ]
        transfers = transfer_walks(
            nodes, rides, Path(__file__).resolve().parents[1] / "data/transit"
        )
        if entry:
            walks = [dict(**{"from": "O", "to": "A"}, minutes=9)]
        return Network(nodes, walks=walks + transfers, transit=rides)

    def test_actual_transfer_does_not_add_a_second_boarding_buffer(self):
        net = self.network()
        self.assertEqual(
            next(e for e in net.walks if e["from"] == "A" and e["to"] == "B")[
                "minutes"
            ],
            1.4,
        )
        self.assertEqual(scan(net)["total_minutes"], 14)
        result = Router(
            net, lambda *a: {}, Config(required_mode="transit", max_seconds=3)
        ).search("O", "D", START.isoformat())
        self.assertEqual(result["routes"][0]["total_minutes"], 14)
        # Compare identical services with the old 3-minute transfer + 2-minute buffer.
        old = Network(
            net.nodes,
            transit=net.transit,
            walks=[
                dict(**{"from": e["from"], "to": e["to"]}, minutes=3)
                if e.get("platform_transfer")
                else e
                for e in net.walks
            ],
        )
        self.assertEqual(scan(old)["total_minutes"], 24)

    def test_walking_into_a_station_does_not_skip_initial_entry(self):
        net = self.network(entry=True)
        self.assertEqual(scan(net)["total_minutes"], 24)
        result = Router(
            net, lambda *a: {}, Config(required_mode="transit", max_seconds=3)
        ).search("O", "D", START.isoformat())
        self.assertEqual(result["routes"][0]["total_minutes"], 24)

    def test_radius_extension_retains_core_and_discovers_long_bicycle_od(self):
        o = dict(latitude=37.5, longitude=127.0)
        d = dict(latitude=37.6, longitude=127.0)
        walk, bike = adaptive_radii(o, d, {}, [], 900, 7000, 30)
        self.assertEqual(walk, 2400)
        self.assertGreater(bike, 11000)
        nodes = {
            "bike:a": dict(o, kind="bike", station_id="1"),
            "bike:b": dict(d, kind="bike", station_id="2"),
            "destination": d,
        }
        old = LazyConnections(Network(nodes, bike_policy={"max_distance_m": 7000}))
        new = LazyConnections(
            Network(
                nodes, bike_policy={"max_distance_m": bike, "core_distance_m": 7000}
            )
        )
        self.assertEqual(old.bikes_from("bike:a"), [])
        self.assertEqual([e["to"] for e in new.bikes_from("bike:a")], ["bike:b"])

    def test_walk_priority_radius_uses_od_length_and_endpoint_density(self):
        origin = dict(latitude=37.5, longitude=127.0)
        destination = dict(latitude=37.6, longitude=127.0)
        nodes, stations = {}, []
        for i in range(6):
            nodes[f"dense:{i}"] = dict(
                latitude=37.5004 + i * 0.0002,
                longitude=127.0,
            )
            nodes[f"sparse:{i}"] = dict(
                latitude=37.582 + i * 0.001,
                longitude=127.0,
            )
            stations.extend(
                [
                    dict(
                        station_id=f"d{i}",
                        latitude=37.5004 + i * 0.0002,
                        longitude=127.0002,
                    ),
                    dict(
                        station_id=f"s{i}",
                        latitude=37.582 + i * 0.001,
                        longitude=127.0002,
                    ),
                ]
            )
        policy = adaptive_walk_policy(origin, destination, nodes, stations, 900, 30)
        radii = policy["preferred_endpoint_radius_m"]
        self.assertEqual(policy["max_distance_m"], 2400)
        self.assertEqual(radii["origin"], 240)
        self.assertGreater(radii["destination"], radii["origin"])
        self.assertLessEqual(radii["destination"], policy["preferred_cap_m"])
        self.assertEqual(policy["target_neighbors"], 6)

        short = adaptive_walk_policy(
            origin,
            dict(latitude=37.51, longitude=127.0),
            nodes,
            stations,
            900,
            30,
        )
        self.assertEqual(short["preferred_cap_m"], 900)
        self.assertEqual(short["max_distance_m"], 2400)

    def test_walk_priority_keeps_outer_fallback_in_weighted_order(self):
        nodes = {
            "origin": dict(latitude=37.5, longitude=127.0, kind="endpoint"),
            "destination": dict(latitude=37.55, longitude=127.0, kind="endpoint"),
        }
        for i, offset in enumerate((0.001, 0.002, 0.003, 0.004, 0.012, 0.014)):
            nodes[f"stop:{i}"] = dict(
                latitude=37.5, longitude=127.0 + offset, kind="stop"
            )

        class Walk:
            def __init__(self):
                self.calls = []

            def __call__(self, a, b, start, end):
                self.calls.append(end)
                return dict(**{"from": start, "to": end}, minutes=1)

        walk = Walk()
        network = Network(
            nodes=nodes,
            transit=[
                {"from": key, "to": "destination"}
                for key in nodes
                if key.startswith("stop:")
            ],
            walk_policy={
                "core_distance_m": 900,
                "priority_floor_m": 240,
                "max_distance_m": 2400,
                "preferred_endpoint_radius_m": {"origin": 400},
                "target_neighbors": 4,
            },
        )
        list(LazyConnections(network, walk).iter_walks_from("origin"))
        self.assertIn("stop:4", walk.calls)
        self.assertIn("stop:5", walk.calls)
        self.assertLess(walk.calls.index("stop:4"), 4)

    def test_more_distant_rail_access_is_retained_without_new_api_limit(self):
        o = dict(latitude=37.5, longitude=127.0)
        d = dict(latitude=37.6, longitude=127.0)
        nodes = {
            "subway:A": dict(latitude=37.511, longitude=127.0, name="A"),
            "subway:B": dict(latitude=37.59, longitude=127.0, name="B"),
        }
        e = event("subway:A", "subway:B", 25, 35, "t")
        table = Network(nodes, transit=[e])

        class Walk:
            def __call__(self, a, b, start, end):
                return dict(**{"from": start, "to": end}, minutes=1, source="synthetic")

        old = build_scheduled_network(
            table,
            [],
            o,
            d,
            Walk(),
            ScheduledConfig(search_mode="subway", adaptive_connections=False),
            START.isoformat(),
        )
        new = build_scheduled_network(
            table,
            [],
            o,
            d,
            Walk(),
            ScheduledConfig(search_mode="subway"),
            START.isoformat(),
        )
        self.assertEqual(old.metadata["endpoint_candidate_count"], 0)
        self.assertGreater(new.metadata["endpoint_candidate_count"], 0)
        self.assertEqual(ScheduledConfig().max_seconds, 100)

    def test_bus_matrix_keeps_direction_groups_separate_and_rejects_bad_rows(self):
        html = '<table class="table"><thead><tr><th>순번</th><th>A</th><th>T1 도착</th><th>T2 도착</th><th>T2 출발</th><th>T1 출발</th></tr></thead><tbody><tr><td>1</td><td>08:00</td><td>09:00</td><td>09:20</td><td>05:00</td><td>05:20</td></tr></tbody></table>'
        self.assertEqual(matrix(html, 1)["trips"][0]["outbound"], [28800, 32400, 33600])
        with self.assertRaises(ValueError):
            matrix(html.replace("09:00", "07:59"), 1)

    def test_airport_bus_cannot_be_used_for_city_to_city_travel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = dict(
                valid_from="2026-11-01",
                airport=[
                    dict(
                        route_number="6009",
                        stations=[dict(ars="00001"), dict(ars="00002")],
                        calendars={
                            "0": dict(
                                profiles=["weekday"],
                                source_url="https://seoulairbus.co.kr",
                                trips=[
                                    dict(
                                        number="1",
                                        outbound=[
                                            8 * 3600,
                                            8 * 3600 + 600,
                                            9 * 3600,
                                            9 * 3600 + 1200,
                                        ],
                                    )
                                ],
                            )
                        },
                    )
                ],
            )
            (root / "bus_web_timetables.json").write_text(
                json.dumps(data), encoding="utf8"
            )
            p = dict(
                bus_stops={
                    k: dict(name=n, latitude=37.5, longitude=127.0)
                    for k, n in [
                        ("a", "A"),
                        ("b", "B"),
                        ("t1", "인천공항T1-3층"),
                        ("t2", "인천공항T2-3층"),
                    ]
                }
            )
            with patch(
                "bus_web_timetables.station_ars",
                return_value={"a": "00001", "b": "00002"},
            ):
                n, report = published_airport_network(
                    root,
                    p,
                    [dict(노선명="6009", 노선ID="r")],
                    {"r": [{"노드ID": k} for k in p["bus_stops"]]},
                    START.isoformat(),
                    120,
                )
            self.assertEqual(len(n.transit), 3)
            self.assertFalse(n.transit[0]["dropoff_allowed"])
            r = Router(
                n, lambda *a: {}, Config(required_mode="transit", max_seconds=2)
            ).search("bus:a", "bus:b", START.isoformat())
            self.assertEqual(r["routes"], [])
            r = Router(
                n, lambda *a: {}, Config(required_mode="transit", max_seconds=2)
            ).search("bus:a", "bus:t1", (START - timedelta(minutes=1)).isoformat())
            self.assertTrue(r["routes"])


if __name__ == "__main__":
    unittest.main()
