from pathlib import Path
import sys, tempfile, unittest, copy, json
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from open_meteo_history import (
    OpenMeteoHistoryProvider,
    FIELDS,
    UNITS,
    local_hour,
    HistoricalWeatherUnavailable,
)

POINTS = [
    dict(location="A", latitude=37.5, longitude=127.1),
    dict(location="B", latitude=37.6, longitude=126.9),
    dict(location="seoul_reference", latitude=37.5665, longitude=126.978),
]


def payload():
    result = []
    for i, p in enumerate(POINTS):
        hours = {"time": [f"2026-10-25T{h:02}:00" for h in range(24)]}
        for k in FIELDS:
            hours[k] = [i + 10.0] * 24
        hours["relative_humidity_2m"] = [60.0] * 24
        hours["wind_direction_10m"] = [350.0, 10.0, 90.0][i : i + 1] * 24
        result.append(
            dict(
                hourly=hours,
                hourly_units=UNITS.copy(),
                utc_offset_seconds=32400,
                latitude=p["latitude"],
                longitude=p["longitude"],
            )
        )
    return result


class Session:
    def __init__(self, data=None, status=200):
        self.data = data or payload()
        self.status = status
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        from types import SimpleNamespace

        return SimpleNamespace(status_code=self.status, json=lambda: self.data)


class OpenMeteoHistoryTests(unittest.TestCase):
    def provider(self, d, session):
        return OpenMeteoHistoryProvider(
            Path(d) / "cache.sqlite", session, POINTS, Path(d) / "missing.parquet"
        )

    def test_training_selection_seoul_mean_units_and_daily_cache(self):
        with tempfile.TemporaryDirectory() as d:
            session = Session()
            p = self.provider(d, session)
            r = p.for_route((37.5, 127.1), (37.6, 126.9), "2026-10-25T03:23:00+09:00")
            self.assertEqual(r["weather"]["origin"]["temperature_c"], 10.0)
            self.assertEqual(r["weather"]["destination"]["temperature_c"], 11.0)
            self.assertEqual(r["weather"]["seoul"]["temperature_c"], 11.0)
            self.assertEqual(r["weather"]["seoul"]["wind_direction_deg"], 150.0)
            self.assertEqual(r["weather"]["origin"]["snowfall_cm"], 10.0)
            self.assertEqual(r["observed_at"], "2026-10-25T03:00:00")
            self.assertEqual(r["new_api_calls"], 1)
            again = self.provider(d, session).for_route(
                (37.5, 127.1), (37.6, 126.9), "2026-10-25T23:52:00+09:00"
            )
            self.assertEqual(again["new_api_calls"], 0)
            self.assertEqual(len(session.calls), 1)
            params = session.calls[0][1]
            self.assertEqual(params["wind_speed_unit"], "ms")
            self.assertEqual(params["timezone"], "Asia/Seoul")
            self.assertNotIn("authKey", params)
            self.assertNotIn("models", params)

    def test_bad_unit_timezone_date_and_location_count_rejected(self):
        variants = []
        a = payload()
        a[0]["hourly_units"]["wind_speed_10m"] = "km/h"
        variants.append(a)
        a = payload()
        a[0]["utc_offset_seconds"] = 0
        variants.append(a)
        a = payload()
        a[0]["hourly"]["time"][0] = "2026-10-26T00:00"
        variants.append(a)
        variants.append(payload()[:2])
        for data in variants:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(HistoricalWeatherUnavailable):
                    self.provider(d, Session(data)).for_route(
                        (37.5, 127.1), (37.6, 126.9), "2026-10-25T03:23:00+09:00"
                    )

    def test_null_and_absent_fields_are_distinguished(self):
        data = payload()
        data[0]["hourly"]["snowfall"][3] = None
        with tempfile.TemporaryDirectory() as d:
            r = self.provider(d, Session(data)).for_route(
                (37.5, 127.1), (37.6, 126.9), "2026-10-25T03:23:00+09:00"
            )
            self.assertIsNone(r["weather"]["origin"]["snowfall_cm"])
        del data[0]["hourly"]["snowfall"]
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(HistoricalWeatherUnavailable):
                self.provider(d, Session(data)).for_route(
                    (37.5, 127.1), (37.6, 126.9), "2026-10-25T03:23:00+09:00"
                )

    def test_429_and_unavailable_recent_dates_do_not_retry_immediately(self):
        for status in (429, 400):
            with tempfile.TemporaryDirectory() as d:
                s = Session(status=status)
                p = self.provider(d, s)
                for _ in range(2):
                    with self.assertRaises(HistoricalWeatherUnavailable):
                        p.for_route(
                            (37.5, 127.1), (37.6, 126.9), "2026-10-25T03:23:00+09:00"
                        )
                self.assertEqual(len(s.calls), 1)

    def test_no_future_hour_and_latest_point_within_two_hours(self):
        with tempfile.TemporaryDirectory() as d:
            p = self.provider(d, Session())
            rows = []
            for hour, temp in [(0, 4), (2, 8), (4, 99)]:
                rows.append(
                    dict(
                        POINTS[0],
                        valid_at=f"2026-10-25T{hour:02}:00",
                        temperature_c=temp,
                    )
                )
            r = p._select_hour(rows, datetime(2026, 10, 25, 3))
            self.assertEqual(len(r), 1)
            self.assertEqual(r[0]["temperature_c"], 8)
        self.assertEqual(
            local_hour("2026-10-24T18:23:00+00:00"), datetime(2026, 10, 25, 3)
        )
        with self.assertRaises(HistoricalWeatherUnavailable):
            local_hour("2099-01-01T00:00:00+09:00")

    def test_packaged_training_weather_reused_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            s = Session()
            p = OpenMeteoHistoryProvider(Path(d) / "c.sqlite", s)
            r = p.for_route((37.5, 127.1), (37.6, 126.9), "2026-06-01T03:23:00+09:00")
            self.assertEqual(r["source"], "open_meteo_training_archive")
            self.assertEqual(r["location_count"], 26)
            self.assertEqual(s.calls, [])

    def test_not_ready_archive_is_not_permanently_cached(self):
        data = payload()
        for item in data:
            item["hourly"]["temperature_2m"] = [None] * 24
        with tempfile.TemporaryDirectory() as d:
            s = Session(data)
            p = self.provider(d, s)
            with self.assertRaises(HistoricalWeatherUnavailable):
                p.for_route((37.5, 127.1), (37.6, 126.9), "2026-10-25T03:23:00+09:00")
            from contextlib import closing
            import sqlite3

            with closing(sqlite3.connect(Path(d) / "cache.sqlite")) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM archive_days").fetchone()[0], 0
                )


if __name__ == "__main__":
    unittest.main()
