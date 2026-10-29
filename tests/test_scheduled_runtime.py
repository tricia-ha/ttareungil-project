from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import unittest

from multimodal import Network
from scheduled_runtime import (
    ScheduledConfig,
    build_scheduled_network,
    select_station_candidates,
)


class FakeWalking:
    def __init__(self):
        self.calls = []

    def __call__(self, a, b, start, end):
        self.calls.append((start, end))
        return {"from": start, "to": end, "minutes": 2, "source": "fake"}


class ScheduledRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.origin = {"latitude": 37.5, "longitude": 127.0}
        self.destination = {"latitude": 37.51, "longitude": 127.01}
        self.stops = {
            "stop:A": {
                "kind": "stop",
                "name": "A",
                "latitude": 37.5005,
                "longitude": 127.0005,
            },
            "stop:B": {
                "kind": "stop",
                "name": "B",
                "latitude": 37.5095,
                "longitude": 127.0095,
            },
        }
        self.timetable = Network(
            nodes=self.stops,
            transit=[
                {
                    "from": "stop:A",
                    "to": "stop:B",
                    "departure": "2026-10-15T08:10:00+09:00",
                    "arrival": "2026-10-15T08:20:00+09:00",
                    "mode": "bus",
                    "trip_id": "T",
                }
            ],
            metadata={"transit_type": "scheduled_gtfs_not_realtime"},
        )
        self.stations = [
            {
                "station_id": "1",
                "station_name": "S1",
                "latitude": "37.501",
                "longitude": "127.001",
                "inventory_station_id": "ST-1",
            },
            {
                "station_id": "2",
                "station_name": "S2",
                "latitude": "37.509",
                "longitude": "127.009",
                "inventory_station_id": "ST-2",
            },
            {
                "station_id": "3",
                "station_name": "far",
                "latitude": "38",
                "longitude": "128",
                "inventory_station_id": "ST-3",
            },
        ]

    def test_station_candidates_are_bounded(self):
        chosen = select_station_candidates(
            self.stations, self.stops, self.origin, self.destination, 2
        )
        self.assertEqual({x["station_id"] for x in chosen}, {"1", "2"})

    def test_network_uses_timetable_and_bounded_walks(self):
        walking = FakeWalking()
        c = ScheduledConfig(
            complete_candidates=False,
            max_station_candidates=2,
            endpoint_stop_neighbors=1,
            endpoint_station_neighbors=1,
            station_stop_neighbors=1,
            bike_neighbors=1,
            max_connector_distance_m=2000,
        )
        network = build_scheduled_network(
            self.timetable, self.stations, self.origin, self.destination, walking, c
        )
        self.assertEqual(network.transit, self.timetable.transit)
        self.assertEqual(len([n for n in network.nodes if n.startswith("bike:")]), 2)
        self.assertTrue(network.bikes)
        self.assertLessEqual(len(walking.calls), 11)
        self.assertIn(("stop:A", "stop:B"), walking.calls)
        self.assertTrue(network.metadata["transit_departures_are_scheduled"])

    def test_rail_interchange_not_lost_when_candidate_budget_is_small(self):
        nodes = {
            "subway:1:A": dict(name="서울역", latitude=37.55, longitude=126.97),
            "subway:공항철도:서울": dict(
                name="서울", latitude=37.551, longitude=126.97
            ),
            "subway:2:C": dict(name="다른역", latitude=37.56, longitude=126.97),
        }
        rides = [
            dict(
                self.timetable.transit[0],
                **{"from": "subway:1:A", "to": "subway:2:C", "mode": "subway"},
            ),
            dict(
                self.timetable.transit[0],
                **{
                    "from": "subway:공항철도:서울",
                    "to": "subway:2:C",
                    "mode": "subway",
                    "trip_id": "T2",
                },
            ),
        ]
        network = build_scheduled_network(
            Network(nodes=nodes, transit=rides),
            [],
            self.origin,
            self.destination,
            FakeWalking(),
            ScheduledConfig(transfer_stop_candidates=1, max_transfer_walks=1),
        )
        pairs = {(e["from"], e["to"]) for e in network.walks}
        self.assertIn(("subway:1:A", "subway:공항철도:서울"), pairs)
        self.assertIn(("subway:공항철도:서울", "subway:1:A"), pairs)


if __name__ == "__main__":
    unittest.main()
