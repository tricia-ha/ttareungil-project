import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from transit_coordinates import corrected_subway_stops
from multimodal_data import distance


class CoordinateCorrectionTests(unittest.TestCase):
    def test_only_exact_reviewed_old_point_is_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "files" / "subway_coordinate_corrections.json"
            path.parent.mkdir()
            row = dict(
                station_key="2:0244",
                name="용답",
                original=dict(latitude=37.566412, longitude=126.977863),
                corrected=dict(latitude=37.562066, longitude=127.05087863),
                source="reviewed",
            )
            path.write_text(json.dumps(dict(corrections=[row])), encoding="utf8")
            stops = {"2:0244": dict(name="용답", **row["original"])}
            fixed = corrected_subway_stops(stops, folder)
            self.assertGreater(distance(stops["2:0244"], fixed["2:0244"]), 6000)
            self.assertEqual(stops["2:0244"]["longitude"], 126.977863)
            changed = {
                "2:0244": dict(name="용답", latitude=37.5621, longitude=127.0509)
            }
            self.assertEqual(corrected_subway_stops(changed, folder), changed)
            wrong = {"2:0244": dict(name="다른 역", **row["original"])}
            self.assertEqual(corrected_subway_stops(wrong, folder), wrong)

    def test_reviewed_yongdap_cannot_form_cityhall_walking_shortcut(self):
        root = Path(__file__).resolve().parents[1]
        stops = {"2:0244": dict(name="용답", latitude=37.566412, longitude=126.977863)}
        fixed = corrected_subway_stops(stops, root / "data/transit")
        cityhall = dict(latitude=37.563534, longitude=126.975271)
        self.assertGreater(distance(cityhall, fixed["2:0244"]), 6000)
        self.assertEqual(
            len(
                json.loads(
                    (
                        root / "data/transit/files/subway_coordinate_corrections.json"
                    ).read_text(encoding="utf8")
                )["corrections"]
            ),
            4,
        )


if __name__ == "__main__":
    unittest.main()
