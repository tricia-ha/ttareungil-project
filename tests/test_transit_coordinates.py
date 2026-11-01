import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from transit_coordinates import corrected_subway_stops, corrected_bus_stops
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
            8,
        )

    def test_yangwon_is_seoul_station_not_a_distant_namesake(self):
        root = Path(__file__).resolve().parents[1]
        stops = {
            "경의중앙선:양원": dict(
                name="양원역", latitude=36.963729, longitude=129.091321
            )
        }
        fixed = corrected_subway_stops(stops, root / "data/transit")
        self.assertGreater(
            distance(stops["경의중앙선:양원"], fixed["경의중앙선:양원"]), 180000
        )
        self.assertLess(
            distance(
                fixed["경의중앙선:양원"], dict(latitude=37.6066, longitude=127.1079)
            ),
            10,
        )

    def test_bus_id_does_not_match_same_name_elsewhere(self):
        root = Path(__file__).resolve().parents[1]
        bad = dict(name="글래드라이브강남호텔", latitude=37.48373, longitude=127.068697)
        stops = {"122000716": bad, "different_city_same_name": dict(bad)}
        fixed = corrected_bus_stops(stops, root / "data/transit")
        self.assertGreater(distance(bad, fixed["122000716"]), 3800)
        self.assertEqual(
            stops["different_city_same_name"], fixed["different_city_same_name"]
        )
        updated = {
            "122000716": dict(
                name="글래드라이브강남호텔", latitude=37.5081, longitude=127.037
            )
        }
        self.assertEqual(corrected_bus_stops(updated, root / "data/transit"), updated)

    def test_platform_fix_does_not_collapse_other_lines(self):
        root = Path(__file__).resolve().parents[1]
        old = dict(name="디지털미디어시티", latitude=37.577005, longitude=126.898643)
        stops = {"6:2619": old, "other:2619": dict(old)}
        fixed = corrected_subway_stops(stops, root / "data/transit")
        self.assertNotEqual(fixed["6:2619"], old)
        self.assertEqual(fixed["other:2619"], old)


if __name__ == "__main__":
    unittest.main()
