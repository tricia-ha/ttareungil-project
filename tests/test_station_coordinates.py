import sys
import json
import csv
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from station_coordinates import bicycle_coordinates, routing_station_catalog
from kakao_runtime import eligible_station_catalog
from eta_service import BicycleETAService
from multimodal import V521BikeProvider
from multimodal_cache import CachedBikeProvider


class StationCoordinateTests(unittest.TestCase):
    def setUp(self):
        import pandas as pd

        self.service = object.__new__(BicycleETAService)
        self.service.stations = pd.DataFrame(
            [
                dict(station_id="1", latitude=37.55, longitude=126.92, district="A"),
                dict(station_id="2", latitude=37.56, longitude=126.93, district="B"),
            ]
        ).set_index("station_id")
        self.service.lock = threading.RLock()
        self.seen = []

        def candidates(*points):
            self.seen.append(points)
            return [
                dict(
                    distance_km=1,
                    geometry=[[points[1], points[0]], [points[3], points[2]]],
                )
            ]

        self.service.routes = SimpleNamespace(candidates=candidates)
        self.moved = dict(latitude=37.551, longitude=126.921)

    def test_request_local_route_keeps_historical_catalog_intact(self):
        before = self.service.stations.copy(deep=True)
        moved, _ = self.service._route_row(
            "1",
            "2",
            "2026-10-30T09:00+09:00",
            "regular",
            "domestic",
            start_coordinates=self.moved,
        )
        normal, _ = self.service._route_row(
            "1", "2", "2026-10-30T09:00+09:00", "regular", "domestic"
        )
        self.assertEqual((moved["start_lat"], moved["start_lon"]), (37.551, 126.921))
        self.assertEqual((normal["start_lat"], normal["start_lon"]), (37.55, 126.92))
        self.assertTrue(before.equals(self.service.stations))
        self.assertEqual(self.seen[0], (37.551, 126.921, 37.56, 126.93))

    def test_bad_coordinates_do_not_corrupt_catalog(self):
        for coordinates in [
            dict(latitude=float("nan"), longitude=127),
            dict(latitude=0, longitude=127),
        ]:
            with self.assertRaises(ValueError):
                self.service._request_station("1", coordinates)
        self.assertEqual(self.service.stations.loc["1"].latitude, 37.55)

    def test_moved_position_cannot_reuse_old_od_cache_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = CachedBikeProvider(
                None, Path(folder) / "pred.sqlite", {"version": 1}
            )
            try:
                edge = dict(eta_from="1", eta_to="2")
                when = "2026-10-30T09:00+09:00"
                before = cache._identity(edge, when)
                moved = dict(
                    self.moved, eta_catalog_latitude=37.55, eta_catalog_longitude=126.92
                )
                updated = bicycle_coordinates(
                    dict(edge), moved, dict(latitude=37.56, longitude=126.93)
                )
                self.assertNotEqual(before, cache._identity(updated, when))
                self.assertEqual(before, cache._identity(dict(edge), when))
            finally:
                cache.close()

    def test_live_merge_and_edge_preserve_positions_only_for_moved_station(self):
        stations = [dict(station_id="1", latitude=37.55, longitude=126.92)]
        snapshot = {"ST-1": dict(self.moved, current_bikes=2)}
        rows, _ = eligible_station_catalog(
            stations, {"1": "ST-1"}, SimpleNamespace(station_ids={"ST-1"}), snapshot
        )
        edge = bicycle_coordinates(
            dict(eta_from="1", eta_to="2"),
            rows[0],
            dict(latitude=37.56, longitude=126.93),
        )
        self.assertEqual(edge["eta_from_latitude"], self.moved["latitude"])
        self.assertNotIn("eta_to_latitude", edge)
        self.assertNotIn("eta_catalog_latitude", stations[0])
        provider = object.__new__(V521BikeProvider)
        provider.bike_type, provider.user_type = "regular", "domestic"
        self.assertEqual(
            provider._eta_request(edge, "2026-10-30T09:00+09:00")["start_coordinates"],
            self.moved,
        )

    def test_float_serialization_noise_does_not_invalidate_unchanged_od(self):
        original = dict(
            station_id="224", latitude=37.520088200000004, longitude=126.93236542
        )
        live = dict(latitude=37.5200882, longitude=126.93236542, current_bikes=1)
        rows, _ = eligible_station_catalog(
            [original],
            {"224": "ST-224"},
            SimpleNamespace(station_ids={"ST-224"}),
            {"ST-224": live},
        )
        self.assertNotIn("eta_catalog_latitude", rows[0])
        edge = dict(eta_from="224", eta_to="2")
        self.assertEqual(bicycle_coordinates(dict(edge), rows[0], rows[0]), edge)

    def test_weather_and_batch_fallback_use_request_positions(self):
        positions, prepared = [], []

        def for_route(*points):
            positions.append(points[:4])
            return dict(origin={}, destination={}, seoul={})

        self.service.weather_provider = SimpleNamespace(
            for_route=for_route, model_weather=lambda _: {}
        )
        self.service.predict = lambda *a, **kw: prepared.append(kw) or dict(model={})
        self.service.predict_with_kma(
            "1", "2", "2026-10-30T09:00+09:00", start_coordinates=self.moved
        )
        self.assertEqual(positions[-1], (37.551, 126.921, 37.56, 126.93))
        self.assertEqual(prepared[-1]["start_coordinates"], self.moved)

        self.service.predict_batch = lambda requests: prepared.extend(requests) or [
            dict(model={}) for _ in requests
        ]
        request = dict(
            start_station_id="1",
            end_station_id="2",
            departure="2026-10-30T09:00+09:00",
            start_coordinates=self.moved,
        )
        self.service.predict_with_kma_batch([request])
        self.assertEqual(positions[-1], (37.551, 126.921, 37.56, 126.93))

        def unavailable(*args):
            raise RuntimeError("offline")

        self.service.weather_provider.for_route = unavailable
        output = self.service.predict_with_kma_batch([request])
        self.assertEqual(output[0]["weather_error"], "RuntimeError")
        self.assertEqual(prepared[-1]["start_coordinates"], self.moved)

    def test_saved_current_location_live_refresh_and_catalog_are_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = Path(folder) / "stations.csv"
            fields = ["station_id", "latitude", "longitude", "catalog_as_of"]
            original = dict(
                station_id="1",
                latitude=37.55,
                longitude=126.92,
                catalog_as_of="2026-06",
            )
            with catalog.open("w", newline="", encoding="utf8") as stream:
                writer = csv.DictWriter(stream, fields)
                writer.writeheader()
                writer.writerow(original)
            updates = Path(folder) / "current.json"
            updates.write_text(
                json.dumps(
                    dict(
                        snapshot_at="2026-11-01",
                        stations={"1": dict(original=original, current=self.moved)},
                    )
                ),
                encoding="utf8",
            )
            before = catalog.read_bytes()
            rows = routing_station_catalog(catalog, updates)
            self.assertEqual(rows[0]["latitude"], 37.551)
            self.assertEqual(rows[0]["eta_catalog_latitude"], 37.55)
            self.assertEqual(catalog.read_bytes(), before)
            # A later live position still compares against the model's original,
            # rather than overwriting the original with the saved current point.
            snapshot = {
                "ST-1": dict(latitude=37.552, longitude=126.922, current_bikes=1)
            }
            latest, _ = eligible_station_catalog(
                rows, {"1": "ST-1"}, SimpleNamespace(station_ids={"ST-1"}), snapshot
            )
            self.assertEqual(latest[0]["latitude"], 37.552)
            self.assertEqual(latest[0]["eta_catalog_latitude"], 37.55)
            snapshot["ST-1"].update(latitude=37.55, longitude=126.92)
            returned, _ = eligible_station_catalog(
                rows, {"1": "ST-1"}, SimpleNamespace(station_ids={"ST-1"}), snapshot
            )
            self.assertNotIn("eta_catalog_latitude", returned[0])

    def test_newer_catalog_is_not_replaced_by_saved_position(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = Path(folder) / "stations.csv"
            catalog.write_text(
                "station_id,latitude,longitude,catalog_as_of\n1,37.55,126.92,2026-12\n",
                encoding="utf8",
            )
            updates = Path(folder) / "current.json"
            updates.write_text(
                json.dumps(
                    dict(
                        snapshot_at="2026-11-01",
                        stations={
                            "1": dict(
                                original=dict(latitude=37.55, longitude=126.92),
                                current=self.moved,
                            )
                        },
                    )
                ),
                encoding="utf8",
            )
            self.assertEqual(
                float(routing_station_catalog(catalog, updates)[0]["latitude"]), 37.55
            )


if __name__ == "__main__":
    unittest.main()
