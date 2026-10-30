from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kakao_runtime import eligible_station_catalog


class Inventory:
    station_ids = {"ST-1", "ST-3"}


class RuntimeTests(unittest.TestCase):
    def test_only_live_dual_model_stations_are_candidates(self):
        catalog = [
            {
                "station_id": "101",
                "station_name": "old",
                "latitude": "0",
                "longitude": "0",
            },
            {
                "station_id": "102",
                "station_name": "closed",
                "latitude": "0",
                "longitude": "0",
            },
            {
                "station_id": "103",
                "station_name": "unsupported",
                "latitude": "0",
                "longitude": "0",
            },
        ]
        mapping = {"101": "ST-1", "102": "ST-2", "103": "ST-3"}
        snapshot = {
            "ST-1": {
                "station_name": "live",
                "latitude": 37.5,
                "longitude": 127.0,
                "current_bikes": 4,
            },
            "ST-3": {
                "station_name": "live3",
                "latitude": 37.6,
                "longitude": 127.1,
                "current_bikes": 2,
            },
        }
        rows, coverage = eligible_station_catalog(
            catalog, mapping, Inventory(), snapshot
        )
        self.assertEqual([row["station_id"] for row in rows], ["101", "103"])
        self.assertEqual(rows[0]["latitude"], 37.5)
        self.assertEqual(coverage["missing_live"], 1)
        self.assertEqual(coverage["eligible"], 2)


if __name__ == "__main__":
    unittest.main()
