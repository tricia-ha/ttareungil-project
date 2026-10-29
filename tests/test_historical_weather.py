from pathlib import Path
import sys, tempfile, unittest, json
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from historical_weather import (
    HistoricalWeatherProvider,
    HistoricalWeatherUnavailable,
    parse_table,
    hour_at,
)

META = """# STN_ID LON LAT STN_KO
108 126.9658 37.5714 서울
509 127.1000 37.5100 송파
"""
OBS = """# TM STN TA HM WD WS RN_HR1
202610250300 108 11.2 60 350 2.1 0.0
202610250300 509 12.5 70 10 3.0 1.5
"""


class Session:
    def __init__(self, status=200, obs=OBS):
        self.calls = []
        self.status = status
        self.obs = obs

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        from types import SimpleNamespace

        return SimpleNamespace(
            status_code=self.status,
            content=(META if "stn_inf" in url else self.obs).encode(),
        )


class HistoricalWeatherTests(unittest.TestCase):
    def test_distinct_endpoint_weather_time_floor_and_persistent_cache(self):
        with tempfile.TemporaryDirectory() as d:
            session = Session()
            p = HistoricalWeatherProvider(
                Path(d) / "cache.sqlite", session, "private-test-key"
            )
            r = p.for_route(
                (37.51, 127.1), (37.5714, 126.9658), "2026-10-25T03:23:00+09:00"
            )
            self.assertEqual(r["observed_at"], "202610250300")
            self.assertEqual(r["weather"]["origin"]["temperature_c"], 12.5)
            self.assertEqual(r["weather"]["destination"]["temperature_c"], 11.2)
            self.assertEqual(r["weather"]["origin"]["rainfall_mm"], 1.5)
            self.assertEqual(r["weather"]["origin"]["wind_direction_deg"], 10)
            self.assertIsNone(r["weather"]["origin"]["snowfall_cm"])
            self.assertEqual(r["new_api_calls"], 2)
            p2 = HistoricalWeatherProvider(
                Path(d) / "cache.sqlite", session, "private-test-key"
            )
            again = p2.for_route(
                (37.51, 127.1), (37.5714, 126.9658), "2026-10-25T03:59:00+09:00"
            )
            self.assertEqual(again["new_api_calls"], 0)
            self.assertEqual(len(session.calls), 2)
            self.assertNotIn(
                b"private-test-key", (Path(d) / "cache.sqlite").read_bytes()
            )

    def test_permissions_stop_and_do_not_log_key_or_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            session = Session(status=403)
            p = HistoricalWeatherProvider(Path(d) / "c.sqlite", session, "secret-key")
            for _ in range(2):
                with self.assertRaises(HistoricalWeatherUnavailable) as e:
                    p.for_route(
                        (37.51, 127.1), (37.57, 126.96), "2026-10-25T03:23:00+09:00"
                    )
                self.assertNotIn("secret-key", str(e.exception))
            self.assertEqual(len(session.calls), 1)

    def test_wrong_timestamp_rows_are_not_used(self):
        with tempfile.TemporaryDirectory() as d:
            p = HistoricalWeatherProvider(
                Path(d) / "c.sqlite",
                Session(obs=OBS.replace("202610250300", "202610250400")),
                "test",
            )
            with self.assertRaises(HistoricalWeatherUnavailable):
                p.for_route(
                    (37.51, 127.1), (37.57, 126.96), "2026-10-25T03:23:00+09:00"
                )

    def test_missing_values_are_not_zeroed_or_negative_weather(self):
        with tempfile.TemporaryDirectory() as d:
            p = HistoricalWeatherProvider(
                Path(d) / "c.sqlite",
                Session(obs=OBS.replace("70 10 3.0 1.5", "-99 -99 -99 -99")),
                "test",
            )
            r = p.for_route(
                (37.51, 127.1), (37.57, 126.96), "2026-10-25T03:23:00+09:00"
            )
            for field in (
                "humidity_pct",
                "wind_direction_deg",
                "wind_speed_ms",
                "rainfall_mm",
                "snowfall_cm",
            ):
                self.assertIsNone(r["weather"]["origin"][field])

    def test_schema_changes_and_distant_stations_are_rejected(self):
        with self.assertRaises(HistoricalWeatherUnavailable):
            parse_table("202610250300 108 10", {"STN", "TA"})
        with tempfile.TemporaryDirectory() as d:
            p = HistoricalWeatherProvider(Path(d) / "c.sqlite", Session(), "test")
            with self.assertRaises(HistoricalWeatherUnavailable):
                p.for_route((35.0, 129.0), (35.0, 129.0), "2026-10-25T03:23:00+09:00")

    def test_kst_floor_and_future_guard(self):
        self.assertEqual(hour_at("2026-10-24T18:23:00+00:00"), "202610250300")
        with self.assertRaises(HistoricalWeatherUnavailable):
            hour_at("2099-01-01T00:00:00+09:00")


if __name__ == "__main__":
    unittest.main()
