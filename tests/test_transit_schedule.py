"""Offline timetable tests: no model loading and no network requests."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime
from multimodal_data import KST
from transit_schedule import (
    clock_seconds,
    reconstruct_bus,
    subway_edges,
    day_type,
    build_timetable,
)
from transit_prepare import coordinate, SeoulStaticClient, prepare_transit
from multimodal import Router, Config


class TimetableTests(unittest.TestCase):
    def setUp(self):
        self.stops = {
            s: {"name": s, "latitude": 37.5 + i * 0.01, "longitude": 127.0}
            for i, s in enumerate("ABC")
        }
        self.route = {
            "노선ID": "R",
            "노선명": "100",
            "운행여부": "1",
            "운행소요시간": "20",
            "배차": "10",
            "첫차시간": "0.2350",
            "막차시간": "0.0030",
            "배차(토요일)": "20",
            "첫차시간(토요일)": "0.2350",
            "막차시간(토요일)": "0.0030",
            "배차(공휴일)": "30",
            "첫차시간(공휴일)": "0.2350",
            "막차시간(공휴일)": "0.0030",
        }
        self.seq = [
            {
                "노선ID": "R",
                "노드ID": s,
                "정류장순번": str(i + 1),
                "링크거리누계": str(d),
            }
            for i, (s, d) in enumerate(zip("ABC", [0, 100, 300]))
        ]

    def test_clock_and_holiday(self):
        self.assertEqual(clock_seconds("0.0030"), 1800)
        self.assertEqual(clock_seconds("0.0600"), 21600)
        self.assertEqual(clock_seconds("25:10:00"), 90600)
        self.assertEqual(day_type(date(2026, 10, 16), {date(2026, 10, 16)}), "holiday")
        with self.assertRaises(ValueError):
            clock_seconds("0.6")

    def test_bus_previous_day_distance_and_vehicle_identity(self):
        n, report = reconstruct_bus(
            [self.route],
            self.seq,
            self.stops,
            "2026-10-17T00:00:00+09:00",
            45,
            holiday_dates=set(),
        )
        trip = [e for e in n.transit if e["trip_id"] == "bus:2026-10-16:R:1"]
        self.assertEqual(len(trip), 2)
        self.assertEqual(trip[0]["arrival"], "2026-10-17T00:05:00+09:00")
        self.assertEqual(trip[1]["arrival"], "2026-10-17T00:20:00+09:00")
        self.assertEqual(report["routes_with_edges"], 1)
        n.validate()
        result = Router(n, lambda *_: None, Config(top_k=1, max_seconds=3)).search(
            "bus:A", "bus:C", "2026-10-17T00:09:00+09:00"
        )
        self.assertEqual(result["routes"][0]["transit_boardings"], 1)

    def test_missing_stop_does_not_create_a_jump(self):
        n, report = reconstruct_bus(
            [self.route],
            self.seq,
            {s: v for s, v in self.stops.items() if s != "B"},
            "2026-10-17T00:00:00+09:00",
            45,
            holiday_dates=set(),
        )
        self.assertFalse(n.transit)
        self.assertEqual(report["missing_coordinates"], 1)

    def test_saturday_headway(self):
        n, _ = reconstruct_bus(
            [self.route],
            self.seq,
            self.stops,
            "2026-10-17T23:45:00+09:00",
            90,
            holiday_dates=set(),
        )
        self.assertEqual(
            [e["departure"] for e in n.transit if e["from"] == "bus:A"],
            [
                "2026-10-17T23:50:00+09:00",
                "2026-10-18T00:10:00+09:00",
                "2026-10-18T00:30:00+09:00",
            ],
        )

    def test_subway_uses_train_times_not_station_code_order(self):
        rows = [
            dict(
                STATION_CD=s,
                ARRIVETIME=t,
                LEFTTIME=t,
                LINE_NUM="02호선",
                TRAIN_NO="123",
                INOUT_TAG="1",
                BRANCH_LINE="",
                ORIGINSTATION="C",
                DESTSTATION="B",
                WEEK_TAG="1",
            )
            for s, t in [("C", "08:00:00"), ("A", "08:03:00"), ("B", "08:07:00")]
        ]
        n, _ = subway_edges(rows, self.stops, "2026-10-16T07:55:00+09:00", 30, set())
        self.assertEqual(
            [(e["from"], e["to"]) for e in n.transit],
            [("subway:C", "subway:A"), ("subway:A", "subway:B")],
        )
        n, _ = subway_edges(
            rows,
            {s: v for s, v in self.stops.items() if s != "A"},
            "2026-10-16T07:55:00+09:00",
            30,
            set(),
        )
        self.assertFalse(n.transit)

    def test_official_coordinates(self):
        self.assertEqual(
            coordinate({"LAT": "37.5", "LOT": "127"}),
            {"latitude": 37.5, "longitude": 127},
        )
        self.assertEqual(
            coordinate({"CRDNT_X": "37.5", "CRDNT_Y": "127"}),
            {"latitude": 37.5, "longitude": 127},
        )
        with self.assertRaises(ValueError):
            coordinate({"LAT": "5", "LOT": "-5"})

    def test_page_checkpoint_does_not_repeat_request_or_store_key(self):
        class Response:
            status_code = 200

            def json(self):
                return {
                    "test": {
                        "row": [{"ID": "1"}],
                        "list_total_count": 1,
                        "RESULT": {"CODE": "INFO-000"},
                    }
                }

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, *a, **kw):
                self.calls += 1
                return Response()

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            session = Session()
            client = SeoulStaticClient("private-key", folder, session=session)
            self.assertEqual(client.rows("test"), [{"ID": "1"}])
            self.assertEqual(client.rows("test"), [{"ID": "1"}])
            self.assertEqual(session.calls, 1)
            self.assertNotIn(
                "private-key",
                "".join(p.read_text(encoding="utf-8") for p in Path(folder).glob("*")),
            )

    def test_preparation_to_combined_network_without_live_api(self):
        class StaticClient:
            calls = 0
            hits = 0

            def __init__(self, *args, **kw):
                pass

            def close(self):
                pass

            def rows(self, service, *args):
                if service == "tbisMasterStation":
                    return [
                        {
                            "CRTR_ID": "10000000" + str(i),
                            "CRTR_NM": s,
                            "LAT": str(37.5 + i * 0.01),
                            "LOT": "127",
                        }
                        for i, s in enumerate("AB", 1)
                    ]
                if service == "subwayStationMaster":
                    return [
                        {
                            "BLDN_NM": s,
                            "ROUTE": "1호선",
                            "LAT": str(37.5 + i * 0.01),
                            "LOT": "127",
                        }
                        for i, s in enumerate("AB", 1)
                    ]
                if service == "SearchInfoBySubwayNameService":
                    return [
                        {"STATION_CD": s, "STATION_NM": s, "LINE_NUM": "01호선"}
                        for s in "AB"
                    ]
                sid, tag, direction = args
                if direction == "2":
                    return []
                at = "08:10:00" if sid == "A" else "08:15:00"
                return [
                    dict(
                        STATION_CD=sid,
                        ARRIVETIME=at,
                        LEFTTIME=at,
                        LINE_NUM="01호선",
                        TRAIN_NO="123",
                        ORIGINSTATION="A",
                        DESTSTATION="B",
                    )
                ]

        today = datetime.now(KST).date().isoformat()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("transit_prepare.SeoulStaticClient", StaticClient),
        ):
            folder = Path(tmp)
            (folder / "bus_raw.zip").write_text(
                "노선ID,노선명,운행소요시간,배차,첫차시간,막차시간,배차(토요일),첫차시간(토요일),막차시간(토요일),배차(공휴일),첫차시간(공휴일),막차시간(공휴일)\n"
                "R,100,20,10,0.0800,0.0900,20,0.0800,0.0900,30,0.0800,0.0900\n",
                encoding="utf-8",
            )
            (folder / "bus_route_nodes.dat").write_text(
                "노선ID,노드ID,정류장순번,링크거리누계\nR,100000001,1,0\nR,100000002,2,100\n",
                encoding="utf-8",
            )
            departure = today + "T08:00:00+09:00"
            prepare_transit(
                folder,
                departure,
                60,
                api_key="unused",
                progress=None,
                holiday_dates=set(),
            )
            # Cache can be reused without initializing the API client again.
            with patch(
                "transit_prepare.SeoulStaticClient",
                side_effect=AssertionError("unexpected API"),
            ):
                prepare_transit(
                    folder, departure, 60, progress=None, holiday_dates=set()
                )
            network = build_timetable(folder, departure, 60, holiday_dates=set())
            self.assertEqual({e["mode"] for e in network.transit}, {"bus", "subway"})
            network.validate()


if __name__ == "__main__":
    unittest.main()
