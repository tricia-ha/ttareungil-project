from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from extended_rail import name, clock, LINES
from transit_schedule import subway_edges
from transit_schedule import korean_holiday_dates, day_type
from datetime import date


class ExtendedRailTests(unittest.TestCase):
    def rows(self, start, end, tag="1"):
        return [
            dict(
                LINE_NUM="공항철도",
                TRAIN_NO="A2001",
                INOUT_TAG="down",
                BRANCH_LINE="",
                ORIGINSTATION="서울",
                DESTSTATION="공덕",
                WEEK_TAG=tag,
                VALID_FROM=start,
                VALID_UNTIL=end,
                TIMETABLE_METHOD="official_train_matrix",
                STATION_CD=station,
                ARRIVETIME=t,
                LEFTTIME=t,
            )
            for station, t in [("A", "08:00:00"), ("B", "08:04:00")]
        ]

    def edges(self, rows, departure):
        stops = {
            s: {"latitude": 37.55, "longitude": 126.97, "name": s} for s in ("A", "B")
        }
        return subway_edges(rows, stops, departure, 30, set())[0].transit

    def test_before_after_revision(self):
        rows = self.rows("2025-12-29", "2026-10-22") + self.rows("2026-10-23", "")
        self.assertEqual(len(self.edges(rows, "2026-10-22T07:55:00+09:00")), 1)
        self.assertEqual(len(self.edges(rows, "2026-10-23T07:55:00+09:00")), 1)

    def test_weekend_not_weekday(self):
        self.assertEqual(
            self.edges(self.rows("2026-01-01", ""), "2026-10-17T07:55:00+09:00"), []
        )
        self.assertEqual(
            len(
                self.edges(
                    self.rows("2026-01-01", "", "2"), "2026-10-17T07:55:00+09:00"
                )
            ),
            1,
        )

    def test_shared_station_name(self):
        self.assertEqual(name("서울역"), name("서울"))
        self.assertEqual(name("4.19민주묘지"), name("4·19민주묘지"))
        self.assertEqual(len(LINES), 7)

    def test_public_holiday_calendar_is_cached_without_changing_day_type(self):
        self.assertIs(korean_holiday_dates(2026), korean_holiday_dates(2026))
        self.assertEqual(day_type(date(2026, 10, 9)), "holiday")
        self.assertEqual(day_type(date(2026, 10, 17)), "saturday")
        self.assertEqual(day_type(date(2026, 10, 19)), "weekday")

    def test_inferred_method_is_preserved(self):
        rows = self.rows("2026-01-01", "")
        for row in rows:
            row["TIMETABLE_METHOD"] = "inferred_minute_station_matching"
        self.assertEqual(
            self.edges(rows, "2026-10-16T07:55:00+09:00")[0]["timetable_method"],
            "inferred_minute_station_matching",
        )
