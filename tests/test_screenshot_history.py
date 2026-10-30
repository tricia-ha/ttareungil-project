from pathlib import Path
import sys, json, tempfile, unittest, copy
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from screenshot_history import extract_cards, validate_record, catalogs
from personalization import PersonalizationStore

FIX = Path(__file__).resolve().parent / "fixtures" / "screenshot_ocr"


def sample(i):
    return json.loads((FIX / f"original_{i}.json").read_text(encoding="utf-8"))


class ScreenshotTests(unittest.TestCase):
    def test_numeric_region_confusions_corrected_with_audit(self):
        s = sample(2)
        changes = {
            "1.99km": "l.99km",
            "11분": "II분",
            "03.23": "O3.23",
            "03.34": "O3.34",
            "05756": "O5756",
            "2026-09-25": "2O26-09-25",
            "자전거 번호 73606": "자전거 번호 736O6",
        }
        for t in s["tokens"]:
            t["text"] = changes.get(t["text"], t["text"])
        r = extract_cards(s["tokens"], s["size"])
        self.assertEqual(r["eligible_count"], 1)
        self.assertEqual(r["cards"][0]["record"], self.record())
        self.assertEqual(
            {c["field"] for c in r["cards"][0]["corrections"]},
            {"distance", "minutes", "time", "date", "station", "bike"},
        )

    def test_small_label_value_coordinate_error_does_not_drop_values(self):
        s = sample(2)
        header_y = 1200
        for label, value in [("이용시간", "11분"), ("이용거리", "1.99km")]:
            a = next(
                t
                for t in s["tokens"]
                if t["text"] == label and min(y for x, y in t["box"]) > header_y
            )
            b = next(t for t in s["tokens"] if t["text"] == value)
            center = lambda t: sum(y for x, y in t["box"]) / len(t["box"])
            delta = center(a) - 1 - center(b)
            b["box"] = [[x, y + delta] for x, y in b["box"]]
        r = extract_cards(s["tokens"], s["size"])
        self.assertEqual(r["eligible_count"], 1)
        self.assertEqual(r["cards"][0]["record"], self.record())

    def test_numeric_normalization_does_not_rewrite_names_or_ambiguous_words(self):
        from screenshot_history import _number_text

        c = {"corrections": []}
        for text in ["O목교", "SEOUL", "coffee", "1.99km 0.37km"]:
            self.assertEqual(_number_text(text, "distance", c), text.replace(" ", ""))
        self.assertEqual(c["corrections"], [])

    def record(self, i=2):
        s = sample(i)
        return extract_cards(s["tokens"], s["size"])["cards"][0]["record"]

    def test_actual_ocr_both_images_correct_fields(self):
        for i, bike, km, when in [
            (1, "69041", 1.93, "2026-09-25T23:52"),
            (2, "73606", 1.99, "2026-09-25T03:23"),
        ]:
            s = sample(i)
            r = extract_cards(s["tokens"], s["size"])
            self.assertEqual(r["eligible_count"], 1)
            v = r["cards"][0]["record"]
            self.assertEqual(
                (v["bicycle_number"], v["start_station_id"], v["end_station_id"]),
                (bike, "5756", "4859"),
            )
            self.assertEqual(
                (v["actual_minutes"], v["distance_km"], v["bike_type"]),
                (11.0, km, "regular"),
            )
            self.assertTrue(v["started_at"].startswith(when))

    def test_partial_previous_record_is_not_used(self):
        s = sample(2)
        r = extract_cards(s["tokens"], s["size"])["cards"][0]["record"]
        self.assertNotEqual(r["end_station_id"], "1278")
        self.assertNotEqual(r["actual_minutes"], 2.0)
        self.assertNotEqual(r["distance_km"], 0.37)

    def test_missing_current_distance_does_not_borrow_previous(self):
        s = sample(2)
        s["tokens"] = [t for t in s["tokens"] if t["text"] != "1.99km"]
        self.assertEqual(extract_cards(s["tokens"], s["size"])["eligible_count"], 0)

    def test_missing_current_station_does_not_borrow_previous(self):
        s = sample(2)
        s["tokens"] = [t for t in s["tokens"] if t["text"] != "4859"]
        self.assertEqual(extract_cards(s["tokens"], s["size"])["eligible_count"], 0)

    def test_multiple_complete_cards_stay_separate(self):
        a, b = sample(1), sample(2)
        first = [t for t in a["tokens"] if min(p[1] for p in t["box"]) >= 1578]
        second = copy.deepcopy(b["tokens"])
        for t in second:
            t["box"] = [[x, y + 3000] for x, y in t["box"]]
        result = extract_cards(first + second, (1200, 6000))
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(
            [c["record"]["distance_km"] for c in result["cards"]], [1.93, 1.99]
        )

    def test_blank_and_unrelated_text_are_rejected(self):
        self.assertEqual(extract_cards([], (1000, 2000))["status"], "rejected")
        s = sample(2)
        t = [v for v in s["tokens"] if "자전거" not in v["text"]]
        self.assertEqual(extract_cards(t, s["size"])["eligible_count"], 0)

    def test_invalid_values_unknown_station_and_future_rejected(self):
        r = self.record()
        for changes in [
            dict(actual_minutes=99),
            dict(distance_km=float("nan")),
            dict(start_station_id="999999"),
            dict(end_station_id=r["start_station_id"]),
            dict(started_at="2026-09-25T03:40:00+09:00"),
        ]:
            with self.assertRaises(ValueError):
                validate_record(dict(r, **changes))
        with self.assertRaises(ValueError):
            validate_record(r, now=datetime(2025, 1, 1, tzinfo=timezone.utc))

    def test_unknown_bike_is_not_assumed_regular(self):
        self.assertEqual(
            validate_record(dict(self.record(), bicycle_number="999999"))["bike_type"],
            "unknown",
        )

    def test_import_duration_preserved_and_duplicate_does_not_update_twice(self):
        with tempfile.TemporaryDirectory() as d:
            with PersonalizationStore(Path(d) / "test.sqlite") as s:
                token = s.create_profile()
                r = self.record(1)
                v = s.import_screenshot_record(
                    token, r, 10.0, "test", {"source": "test"}
                )
                self.assertTrue(v["used_for_update"])
                self.assertFalse(v["duplicate"])
                self.assertEqual(
                    s.db.execute(
                        "SELECT actual_minutes,label_source FROM personal_observations"
                    ).fetchone(),
                    (11.0, "screenshot_user_confirmed"),
                )
                again = s.import_screenshot_record(
                    token, r, 9.0, "other_model", {"source": "another_crop"}
                )
                self.assertTrue(again["duplicate"])
                self.assertEqual(
                    s.db.execute(
                        "SELECT COUNT(*) FROM personal_observations"
                    ).fetchone()[0],
                    1,
                )
                other = s.create_profile()
                self.assertFalse(
                    s.import_screenshot_record(other, r, 10.0, "test", {})["duplicate"]
                )

    def test_import_failure_rolls_back_both_ride_and_observation(self):
        with tempfile.TemporaryDirectory() as d:
            with PersonalizationStore(Path(d) / "test.sqlite") as s:
                token = s.create_profile()
                with self.assertRaises(TypeError):
                    s.import_screenshot_record(
                        token, self.record(), 10.0, "test", {"bad": object()}
                    )
                self.assertEqual(
                    s.db.execute("SELECT COUNT(*) FROM personal_rides").fetchone()[0], 0
                )
                self.assertEqual(
                    s.db.execute(
                        "SELECT COUNT(*) FROM personal_observations"
                    ).fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
