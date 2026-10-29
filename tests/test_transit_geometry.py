import sys
from pathlib import Path
import unittest
import tempfile
import copy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from transit_geometry import parse_link_geometry, trim, attach_transit_geometry
from rail_shapes import RailShapes
import json


class GeometryTests(unittest.TestCase):
    def test_topis_coordinates(self):
        self.assertEqual(
            parse_link_geometry("|127.001.37.501||127.002.37.502|"),
            [[127.001, 37.501], [127.002, 37.502]],
        )

    def test_trim_rejects_far_station(self):
        self.assertIsNone(
            trim([[127, 37.5], [127.01, 37.5]], [128, 37.5], [127.01, 37.5])
        )

    def test_missing_geometry_no_fake_line_and_no_timing_changes(self):
        result = {
            "nodes": {},
            "routes": [
                {
                    "total_minutes": 7,
                    "legs": [
                        {
                            "mode": "bus",
                            "from": "a",
                            "to": "b",
                            "route_id": "1",
                            "minutes": 7,
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[127, 37], [128, 38]],
                            },
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            attach_transit_geometry(result, folder)
        self.assertIsNone(result["routes"][0]["legs"][0]["geometry"])
        self.assertEqual(result["routes"][0]["total_minutes"], 7)

    def test_bicycle_shape_is_preserved(self):
        geometry = {
            "type": "LineString",
            "coordinates": [[127, 37], [127.1, 37.1], [127.2, 37.2]],
        }
        result = {
            "nodes": {},
            "routes": [
                {"legs": [{"mode": "bicycle", "geometry": copy.deepcopy(geometry)}]}
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            attach_transit_geometry(result, folder)
        self.assertEqual(result["routes"][0]["legs"][0]["geometry"], geometry)

    def test_real_rail_curve_and_line_separation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "sources.json").write_text(
                json.dumps({"files": [{"line": "5호선", "file": "5.geojson"}]}),
                encoding="utf-8",
            )
            points = [[127, 37.5], [127.001, 37.501], [127.002, 37.5]]
            (path / "5.geojson").write_text(
                json.dumps(
                    {
                        "features": [
                            {"geometry": {"type": "LineString", "coordinates": points}}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            matcher = RailShapes(path)
            result = matcher.match("5호선", points[0], points[-1])
            self.assertIn(points[1], result["coordinates"])
            self.assertIsNone(matcher.match("공항철도", points[0], points[-1]))

    def test_disconnected_tracks_are_not_bridged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "sources.json").write_text(
                json.dumps({"files": [{"line": "5호선", "file": "5.geojson"}]}),
                encoding="utf-8",
            )
            (path / "5.geojson").write_text(
                json.dumps(
                    {
                        "features": [
                            {
                                "geometry": {
                                    "type": "MultiLineString",
                                    "coordinates": [
                                        [[127, 37.5], [127.001, 37.5]],
                                        [[127.02, 37.5], [127.021, 37.5]],
                                    ],
                                }
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(
                RailShapes(path).match(
                    "5호선", [127, 37.5], [127.021, 37.5], max_snap=50
                )
            )

    def test_transfer_diagram_is_separate_from_actual_geometry(self):
        result = {
            "nodes": {
                "a": {"latitude": 37.5, "longitude": 127},
                "b": {"latitude": 37.501, "longitude": 127},
            },
            "routes": [
                {
                    "legs": [
                        {
                            "mode": "walk",
                            "from": "a",
                            "to": "b",
                            "minutes": 3,
                            "geometry": None,
                        }
                    ]
                }
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            attach_transit_geometry(result, folder)
        leg = result["routes"][0]["legs"][0]
        self.assertIsNone(leg["geometry"])
        self.assertEqual(leg["display_geometry_kind"], "schematic_transfer")
        self.assertEqual(leg["minutes"], 3)
