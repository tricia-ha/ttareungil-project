from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import unittest
import tempfile
import zipfile
from pathlib import Path
from multimodal_data import load_gtfs
from multimodal_cache import CachedBikeProvider


class DataTests(unittest.TestCase):
    def fixture(self, path, exceptions="", frequencies=""):
        files = {
            "agency.txt": "agency_name,agency_url,agency_timezone\nTest,https://example.com,Asia/Seoul\n",
            "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nA,A,37.5,127\nB,B,37.51,127.01\n",
            "routes.txt": "route_id,route_type,route_short_name\nR,3,Bus\n",
            "trips.txt": "route_id,service_id,trip_id\nR,S,T\n",
            "calendar_dates.txt": "service_id,date,exception_type\nS,20261014,1\n"
            + exceptions,
            "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT,24:10:00,24:10:00,A,1\nT,24:20:00,24:20:00,B,2\n",
        }
        if frequencies:
            files["frequencies.txt"] = frequencies
        with zipfile.ZipFile(path, "w") as z:
            for name, value in files.items():
                z.writestr(name, value)

    def test_previous_service_day(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "feed.zip"
            self.fixture(path)
            n = load_gtfs(path, "2026-10-15T00:05:00+09:00", 30)
            self.assertEqual(len(n.transit), 1)
            self.assertEqual(n.transit[0]["departure"], "2026-10-15T00:10:00+09:00")

    def test_invalid_date_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "feed.zip"
            self.fixture(path)
            with self.assertRaises(ValueError):
                load_gtfs(path, "2026-10-16T00:05:00+09:00", 30)

    def test_unsupported_frequency_not_silent(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "feed.zip"
            self.fixture(
                path,
                frequencies="trip_id,start_time,end_time,headway_secs\nT,24:00:00,25:00:00,600\n",
            )
            with self.assertRaises(ValueError):
                load_gtfs(path, "2026-10-15T00:05:00+09:00", 30)

    def test_checkpoint_context_isolation(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []

            def predict(e, t):
                calls.append(1)
                return {"minutes": 3}

            for context in ({"factor": 1}, {"factor": 1}, {"factor": 0.9}):
                cache = CachedBikeProvider(predict, Path(d) / "cache.sqlite", context)
                cache({}, "2026-10-15T08:02:15+09:00")
                cache.close()
            self.assertEqual(len(calls), 2)

    def test_eta_cache_is_independent_from_inventory_attachment(self):
        with tempfile.TemporaryDirectory() as d:
            eta_calls = []

            class Provider:
                def __init__(self, risk):
                    self.risk = risk

                def predict_eta(self, edge, departure):
                    eta_calls.append(departure)
                    return {"minutes": 7, "weather_error": None}

                def attach_inventory(self, value, edge, departure):
                    return dict(value, p_empty=self.risk)

            values = []
            for risk in (0.05, 0.25):
                cache = CachedBikeProvider(
                    Provider(risk), Path(d) / "cache.sqlite", {"model": "same"}
                )
                values.append(
                    cache({"eta_from": "1", "eta_to": "2"}, "2026-10-15T08:04:30+09:00")
                )
                cache.close()
            self.assertEqual(len(eta_calls), 1)
            self.assertEqual([v["p_empty"] for v in values], [0.05, 0.25])


if __name__ == "__main__":
    unittest.main()
