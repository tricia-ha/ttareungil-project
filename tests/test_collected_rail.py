"""Calendar, train identity, missing-stop and resumable collection regressions."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from collected_rail import normalize, compile_collected, complete_constant_arrivals
from map_schedule_collection import atomic_json, collect
from transit_schedule import build_timetable


def lane():
    return dict(
        id=10,
        type=dict(id=217),
        stations=[
            dict(id=i, name=n, latitude=37.5 + i / 1000, longitude=127.0)
            for i, n in [(1, "A"), (2, "B"), (3, "C")]
        ],
    )


def schedule(sid, midnight=False):
    groups = []
    for cid in (1, 2, 3):
        if midnight:
            date, hour, minute = (
                ("2026-11-01", 23, 58) if sid == 1 else ("2026-11-02", 0, (sid - 2) * 2)
            )
        else:
            date, hour, minute = "2026-11-01", 8, cid * 10 + (sid - 1) * 2
        clock = f"{date}T{hour:02d}:{minute:02d}:00"
        record = dict(
            routeId=10,
            plateNo="1001",
            startStopId=1,
            headsignStopId=3,
            departureTime=clock,
            arrivalTime=clock,
        )
        groups.append(dict(calendarId=cid, up=[record], down=[]))
    return dict(
        stopId=sid,
        calendars=[
            dict(id=1, name="평일"),
            dict(id=2, name="토요일"),
            dict(id=3, name="공휴일"),
        ],
        schedules=groups,
    )


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(self.status_code)


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return next(self.responses)


class CollectedRailTests(unittest.TestCase):
    def terminal_example(self, variable=False):
        def key(plate, goal):
            return ("L", "weekday", "10", plate, "down", "1", goal)

        target = key("ending", "3")
        groups = {
            key("through1", "4"): [(1000, 1000, "2"), (1120, 1150, "3")],
            key("through2", "4"): [
                (2000, 2000, "2"),
                (2150 if variable else 2120, 2180, "3"),
            ],
            target: [(3000, 3000, "2")],
        }
        return groups, target, {"10": {("2", "3"), ("3", "4")}}

    def test_intermediate_terminating_train_uses_only_constant_same_direction_arc(self):
        groups, target, neighbors = self.terminal_example()
        inferred, report = complete_constant_arrivals(groups, neighbors)
        self.assertIn(target, inferred)
        self.assertEqual(groups[target][-1], (3120, 3120, "3"))
        self.assertEqual(report["inferred_terminal_arrivals"], 1)
        self.assertNotEqual(target[3], "through1")

    def test_variable_or_unobserved_last_segment_is_not_filled(self):
        groups, target, neighbors = self.terminal_example(variable=True)
        self.assertNotIn(target, complete_constant_arrivals(groups, neighbors)[0])
        groups, target, neighbors = self.terminal_example()
        new_target = (*target[:-1], "4")
        groups[new_target] = groups.pop(target)
        self.assertNotIn(new_target, complete_constant_arrivals(groups, neighbors)[0])

    def test_opposite_direction_evidence_does_not_complete_arrival(self):
        groups, target, neighbors = self.terminal_example()
        opposite = (*target[:4], "up", *target[5:])
        groups[opposite] = groups.pop(target)
        self.assertNotIn(opposite, complete_constant_arrivals(groups, neighbors)[0])

    def test_three_profiles_stay_distinct(self):
        rows, stops, lines, report = normalize(
            [lane()], {str(i): schedule(i) for i in (1, 2, 3)}, {}
        )
        first = {
            r["WEEK_TAG"]: r["LEFTTIME"] for r in rows if r["STATION_CD"].endswith(":1")
        }
        self.assertEqual(first, {"1": "08:10:00", "2": "08:20:00", "3": "08:30:00"})

    def test_midnight_belongs_to_previous_service_day(self):
        rows, _, _, _ = normalize(
            [lane()], {str(i): schedule(i, True) for i in (1, 2, 3)}, {}
        )
        values = {r["STATION_CD"]: r["LEFTTIME"] for r in rows if r["WEEK_TAG"] == "1"}
        self.assertEqual(values["naver:217:1"], "23:58:00")
        self.assertEqual(values["naver:217:2"], "24:00:00")
        self.assertEqual(values["naver:217:3"], "24:02:00")

    def test_missing_event_does_not_turn_normal_train_into_express(self):
        records = {str(i): schedule(i) for i in (1, 2, 3)}
        records["2"]["schedules"][0]["up"] = []
        rows, _, _, _ = normalize([lane()], records, {})
        # There is no valid weekday segment; Saturday/Sunday remain usable.
        self.assertFalse(any(r["WEEK_TAG"] == "1" for r in rows))
        self.assertTrue(any(r["WEEK_TAG"] == "2" for r in rows))

    def test_missing_weekend_train_identity_preserves_only_weekday_service(self):
        records = {str(i): schedule(i) for i in (1, 2, 3)}
        for data in records.values():
            for group in data["schedules"][1:]:
                group["up"][0]["plateNo"] = None
        rows, _, lines, report = normalize([lane()], records, {})
        self.assertEqual(lines, ["인천1호선"])
        self.assertEqual({r["WEEK_TAG"] for r in rows}, {"1"})
        self.assertEqual(report["usable_profiles"], {"인천1호선": ["weekday"]})
        self.assertEqual(report["complete_line_names"], [])

    def test_half_collected_line_is_not_published(self):
        rows, _, lines, _ = normalize(
            [lane()], {"1": schedule(1), "3": schedule(3)}, {}
        )
        self.assertEqual(rows, [])
        self.assertEqual(lines, [])

    def test_same_plate_different_destination_is_not_joined(self):
        records = {str(i): schedule(i) for i in (1, 2, 3)}
        for group in records["1"]["schedules"]:
            group["up"][0]["headsignStopId"] = 2
        rows, _, _, _ = normalize([lane()], records, {})
        first = next(r for r in rows if r["STATION_CD"].endswith(":1"))
        second = next(r for r in rows if r["STATION_CD"].endswith(":2"))
        self.assertNotEqual(first["DESTSTATION"], second["DESTSTATION"])

    def test_http429_pauses_then_resume_uses_saved_responses(self):
        with TemporaryDirectory() as tmp:
            atomic_json(Path(tmp) / "lanes.json.gz", [lane()])
            client = Session([Response(schedule(1)), Response({}, 429)])
            state = collect(tmp, 10, 0, session=client, progress=None)
            self.assertFalse(state["complete"])
            self.assertEqual(state["stopped"], "http_429")
            self.assertEqual(len(client.urls), 2)
            resumed = Session([Response(schedule(2)), Response(schedule(3))])
            state = collect(tmp, 10, 0, session=resumed, progress=None)
            self.assertTrue(state["complete"])
            self.assertEqual(len(resumed.urls), 2)
            self.assertEqual(len(state["stations"]), 3)

    def test_compiled_collection_is_used_for_actual_day_queries(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "map_timetables"
            atomic_json(cache / "lanes.json.gz", [lane()])
            for i in (1, 2, 3):
                atomic_json(cache / "stops" / f"{i}.json.gz", schedule(i))
            atomic_json(
                root / "prepared.json",
                dict(
                    valid_from="2026-11-01",
                    valid_until=None,
                    day_types=["weekday", "saturday", "holiday"],
                    subway_stops={},
                    subway_rows=[],
                    bus_stops={},
                ),
            )
            summary = compile_collected(root)
            self.assertEqual(summary["active_lines"], ["인천1호선"])
            published = list((cache / summary["version"]).rglob("*.csv.gz"))
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in published}
            self.assertEqual(compile_collected(root), summary)
            self.assertEqual(
                {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in published}, before
            )
            sunday = build_timetable(
                root, "2026-11-01T08:00:00+09:00", 60, include_bus=False
            )
            monday = build_timetable(
                root, "2026-11-02T08:00:00+09:00", 60, include_bus=False
            )
            self.assertEqual(len(sunday.transit), 2)
            self.assertIn("08:30:00", sunday.transit[0]["departure"])
            self.assertIn("08:10:00", monday.transit[0]["departure"])

    def test_budgeted_refresh_resumes_without_refetching_updated_stations(self):
        with TemporaryDirectory() as tmp:
            initial = Session(
                [Response([lane()])] + [Response(schedule(i)) for i in (1, 2, 3)]
            )
            self.assertTrue(
                collect(tmp, 10, 0, session=initial, progress=None)["complete"]
            )
            first = Session([Response([lane()]), Response(schedule(1))])
            self.assertFalse(
                collect(tmp, 2, 0, refresh=True, session=first, progress=None)[
                    "complete"
                ]
            )
            second = Session([Response(schedule(2)), Response(schedule(3))])
            self.assertTrue(
                collect(tmp, 2, 0, refresh=True, session=second, progress=None)[
                    "complete"
                ]
            )
            self.assertTrue(second.urls[0].endswith("/2?lang=ko&caller=pc_search"))

    def test_incomplete_refresh_preserves_previous_compiled_manifest(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "map_timetables"
            atomic_json(cache / "manifest.json", dict(previous="keep"))
            atomic_json(cache / "collection.json", dict(complete=False))
            before = (cache / "manifest.json").read_bytes()
            with self.assertRaises(ValueError):
                compile_collected(root)
            self.assertEqual((cache / "manifest.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
