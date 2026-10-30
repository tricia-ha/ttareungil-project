"""Search modes on small fixtures; never call real models or external APIs."""

from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import sys, tempfile, unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multimodal import Config, Network, Router
from scheduled_runtime import (
    ScheduledConfig,
    build_scheduled_network,
    run_scheduled_optimization,
)


class FakeClient:
    def __init__(self, *a, **kw):
        self.calls = 0

    def get(self, *a):
        return {
            "status": "OK",
            "route": {"properties": {"totalTime": 60, "totalDistance": 80}, "legs": []},
        }, "test"

    def stats(self):
        return {"new_api_calls": 0, "cache_hits": 0}

    def close(self):
        pass


class FakeETA:
    def predict(self, **kw):
        return {
            "eta": {"p50_min": 10.0, "p80_min": 15.0, "p90_min": 20.0},
            "route": {},
            "model": {},
        }

    def predict_batch(self, rows):
        return [self.predict(**row) for row in rows]


class SearchModeTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime.now(timezone.utc) + timedelta(hours=1)
        self.when = self.start.isoformat()
        self.origin = dict(latitude=37.5, longitude=127.0)
        self.destination = dict(latitude=37.51, longitude=127.0)
        self.nodes = {
            "subway:A": dict(self.origin, name="A", kind="stop"),
            "subway:B": dict(self.destination, name="B", kind="stop"),
            "bus:A": dict(self.origin, name="busA", kind="stop"),
            "bus:B": dict(self.destination, name="busB", kind="stop"),
        }
        self.rides = [
            dict(
                mode=mode,
                trip_id=mode,
                **{"from": prefix + ":A", "to": prefix + ":B"},
                departure=(self.start + timedelta(minutes=3)).isoformat(),
                arrival=(self.start + timedelta(minutes=8)).isoformat(),
            )
            for mode, prefix in (("bus", "bus"), ("subway", "subway"))
        ]
        self.table = Network(
            nodes=self.nodes,
            transit=self.rides,
            metadata={"valid_departure": self.when, "valid_horizon_minutes": 180},
        )
        self.stations = [
            dict(station_id="1", latitude=37.5001, longitude=127.0),
            dict(station_id="2", latitude=37.5099, longitude=127.0),
            dict(station_id="3", latitude=37.53, longitude=127.0),
        ]

    def walk(self, a, b, start, end):
        return {"from": start, "to": end, "minutes": 1.0, "source": "test"}

    def test_builder_filters_before_search_and_preserves_integrated_default(self):
        integrated = build_scheduled_network(
            self.table,
            self.stations,
            self.origin,
            self.destination,
            self.walk,
            ScheduledConfig(),
        )
        rail = build_scheduled_network(
            self.table,
            self.stations,
            self.origin,
            self.destination,
            self.walk,
            ScheduledConfig(search_mode="subway"),
        )
        bike = build_scheduled_network(
            self.table,
            self.stations,
            self.origin,
            self.destination,
            self.walk,
            ScheduledConfig(search_mode="bicycle"),
        )
        self.assertEqual(integrated.transit, self.rides)
        self.assertEqual({e["mode"] for e in rail.transit}, {"subway"})
        self.assertFalse(rail.bike_policy)
        self.assertFalse(any(n.startswith(("bike:", "bus:")) for n in rail.nodes))
        self.assertFalse(bike.transit)
        self.assertFalse(any(n.startswith(("subway:", "bus:")) for n in bike.nodes))
        self.assertNotIn("bike:3", bike.nodes)
        from candidate_edges import LazyConnections

        lazy = LazyConnections(bike, self.walk)
        self.assertEqual([e["to"] for e in lazy.bikes_from("bike:1")], ["bike:2"])
        self.assertEqual(lazy.bikes_from("bike:2"), [])

    def test_required_subway_survives_same_time_walk_dominance(self):
        edge = dict(
            mode="subway",
            trip_id="T",
            **{"from": "O", "to": "C"},
            departure=self.when,
            arrival=(self.start + timedelta(minutes=2)).isoformat(),
        )
        n = Network(
            nodes={k: {} for k in ("O", "C", "Z")},
            transit=[edge],
            walks=[
                {"from": "O", "to": "C", "minutes": 2},
                {"from": "C", "to": "Z", "minutes": 1},
            ],
        )
        result = Router(n, None, Config(required_mode="subway", top_k=1)).search(
            "O", "Z", self.when
        )
        self.assertTrue(result["routes"])
        self.assertTrue(any(e["mode"] == "subway" for e in result["routes"][0]["legs"]))

    def test_required_bike_survives_same_time_walk_dominance(self):
        n = Network(
            nodes={k: {} for k in ("O", "B", "C", "Z")},
            walks=[
                {"from": "O", "to": "C", "minutes": 2},
                {"from": "B", "to": "C", "minutes": 1},
                {"from": "C", "to": "Z", "minutes": 1},
            ],
            bikes=[{"from": "O", "to": "B"}],
        )
        r = Router(
            n, lambda e, t: {"minutes": 1.0}, Config(required_mode="bicycle", top_k=1)
        ).search("O", "Z", self.when)
        self.assertEqual(r["routes"][0]["bike_rentals"], 1)
        self.assertEqual(r["routes"][0]["total_minutes"], 3.0)

    def test_walking_only_is_not_returned_as_a_dedicated_mode(self):
        n = Network(
            nodes={"O": {}, "Z": {}}, walks=[{"from": "O", "to": "Z", "minutes": 1.0}]
        )
        for mode in ("subway", "bicycle", "transit"):
            r = Router(n, None, Config(required_mode=mode)).search("O", "Z", self.when)
            self.assertEqual(r["routes"], [])

    def test_transit_keeps_bus_and_subway_without_bike_candidates(self):
        n = build_scheduled_network(
            self.table,
            self.stations,
            self.origin,
            self.destination,
            self.walk,
            ScheduledConfig(search_mode="transit"),
        )
        self.assertEqual({e["mode"] for e in n.transit}, {"bus", "subway"})
        self.assertFalse(n.bike_policy)
        self.assertFalse(any(k.startswith("bike:") for k in n.nodes))

    def test_transit_can_choose_bus_only_without_eta_or_inventory(self):
        import scheduled_runtime as runtime

        table = Network(
            nodes=self.nodes, transit=[self.rides[0]], metadata=self.table.metadata
        )
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(runtime, "KakaoClient", FakeClient),
            patch.object(runtime, "STATION_CATALOG", Path(d) / "missing.csv"),
            patch(
                "eta_service.BicycleETAService", side_effect=AssertionError("no ETA")
            ),
            patch(
                "inventory_service.InventoryPredictor",
                side_effect=AssertionError("no stock"),
            ),
            patch("transit_geometry.attach_transit_geometry"),
        ):
            r = run_scheduled_optimization(
                self.origin,
                self.destination,
                self.when,
                config=ScheduledConfig(search_mode="transit", top_k=1),
                timetable=table,
                use_kma=True,
                use_inventory=True,
                output_dir=Path(d) / "out",
                progress=None,
            )
        self.assertTrue(r["routes"])
        self.assertEqual(
            {e["mode"] for e in r["routes"][0]["legs"]} - {"walk", "alight", "wait"},
            {"bus"},
        )
        self.assertFalse(r["inventory_connected"])
        self.assertFalse(r["weather_requested"])

    def test_transit_can_transfer_between_bus_and_subway(self):
        edges = [
            dict(
                mode=mode,
                trip_id=mode,
                **{"from": a, "to": b},
                departure=(self.start + timedelta(minutes=d)).isoformat(),
                arrival=(self.start + timedelta(minutes=t)).isoformat(),
            )
            for mode, a, b, d, t in [
                ("bus", "A", "B", 2, 5),
                ("subway", "B", "C", 6, 10),
            ]
        ]
        n = Network(
            nodes={k: {} for k in ("O", "A", "B", "C", "Z")},
            transit=edges,
            walks=[
                {"from": "O", "to": "A", "minutes": 1},
                {"from": "C", "to": "Z", "minutes": 1},
                {"from": "O", "to": "Z", "minutes": 0.5},
            ],
        )
        r = Router(n, None, Config(required_mode="transit", top_k=1)).search(
            "O", "Z", self.when
        )
        self.assertTrue(r["routes"])
        self.assertEqual(
            [
                e["mode"]
                for e in r["routes"][0]["legs"]
                if e["mode"] in ("bus", "subway")
            ],
            ["bus", "subway"],
        )
        self.assertAlmostEqual(r["routes"][0]["total_minutes"], 11.0)

    def test_full_runtime_subway_never_loads_bike_data_or_weather(self):
        import scheduled_runtime as runtime

        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(runtime, "KakaoClient", FakeClient),
            patch.object(runtime, "STATION_CATALOG", Path(d) / "does-not-exist.csv"),
            patch(
                "eta_service.BicycleETAService",
                side_effect=AssertionError("no bicycle model"),
            ),
            patch(
                "inventory_service.InventoryPredictor",
                side_effect=AssertionError("no inventory model"),
            ),
            patch("transit_geometry.attach_transit_geometry"),
        ):
            r = run_scheduled_optimization(
                self.origin,
                self.destination,
                self.when,
                config=ScheduledConfig(search_mode="subway", top_k=1),
                timetable=self.table,
                use_kma=True,
                use_inventory=True,
                output_dir=Path(d) / "out",
                progress=None,
            )
        self.assertEqual(r["search_mode"], "subway")
        self.assertFalse(r["weather_requested"])
        self.assertFalse(r["inventory_connected"])
        self.assertIsNone(r["bicycle_checkpoint"])
        self.assertTrue(r["routes"])
        self.assertEqual(
            {e["mode"] for e in r["routes"][0]["legs"]} - {"walk", "alight", "wait"},
            {"subway"},
        )

    def test_full_runtime_bike_skips_timetable_and_uses_existing_model(self):
        import scheduled_runtime as runtime

        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            catalog = p / "stations.csv"
            catalog.write_text(
                "station_id,latitude,longitude\n1,37.5001,127\n2,37.5099,127\n",
                encoding="utf-8",
            )
            with (
                patch.object(runtime, "KakaoClient", FakeClient),
                patch.object(runtime, "STATION_CATALOG", catalog),
                patch.object(runtime, "RUNTIME_CACHE_DIR", p / "cache"),
                patch.object(runtime, "_artifact_identity", return_value=[]),
                patch(
                    "transit_files.prepare_transit_files",
                    side_effect=AssertionError("no timetable"),
                ),
                patch("transit_geometry.attach_transit_geometry"),
            ):
                r = run_scheduled_optimization(
                    self.origin,
                    self.destination,
                    self.when,
                    config=ScheduledConfig(
                        search_mode="bicycle",
                        require_known_inventory=False,
                        max_empty_probability=None,
                        top_k=1,
                        max_seconds=5,
                        horizon_minutes=30,
                    ),
                    eta_service=FakeETA(),
                    use_kma=False,
                    use_inventory=False,
                    output_dir=p / "out",
                    progress=None,
                )
            self.assertTrue(r["routes"])
            self.assertEqual(r["routes"][0]["bike_rentals"], 1)
            self.assertEqual(r["routes"][0]["transit_boardings"], 0)
            self.assertIsNone(r["timetable_file"])
            self.assertGreater(r["bicycle_checkpoint"]["misses"], 0)


if __name__ == "__main__":
    unittest.main()
