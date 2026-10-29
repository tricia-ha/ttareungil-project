from pathlib import Path
from unittest.mock import patch, Mock
from types import SimpleNamespace
import sys, json, copy, tempfile, unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from screenshot_history import extract_cards, resolve_stations
from personalization import PersonalizationStore


def fixture(replace=None):
    path = Path(__file__).resolve().parent / "fixtures/screenshot_ocr/original_2.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    for token in value["tokens"]:
        token["text"] = (replace or {}).get(token["text"], token["text"])
    return value


class StationMappingTests(unittest.TestCase):
    def test_actual_ids_and_names_map_and_known_wrong_ids_hold(self):
        for replace, status in [
            ({}, "matched"),
            ({"05756": "00102"}, "name_mismatch"),
            ({"05756": "99999"}, "unknown_id"),
            ({"극동아파트": "극동아파드"}, "name_unverified"),
        ]:
            d = fixture(replace)
            result = extract_cards(d["tokens"], d["size"])
            card = result["cards"][0]
            self.assertTrue(card["reviewable"])
            self.assertEqual(
                card["station_mapping"]["stations"]["origin"]["status"], status
            )
            self.assertEqual(card["valid"], status == "matched")
            if status == "matched":
                self.assertEqual(
                    card["station_mapping"]["stations"]["origin"]["mapped_name"],
                    "극동아파트",
                )
            if status in ("name_mismatch", "unknown_id"):
                self.assertIn(
                    "5756",
                    [
                        s["station_id"]
                        for s in card["station_mapping"]["stations"]["origin"][
                            "candidates"
                        ]
                    ],
                )
                self.assertNotEqual(card["record"]["start_station_id"], "5756")

    def test_reversed_and_wrong_destination_ids_are_blocked(self):
        for replace in [{"4859": "00102"}, {"05756": "04859", "4859": "05756"}]:
            d = fixture(replace)
            r = extract_cards(d["tokens"], d["size"])
            self.assertEqual(r["eligible_count"], 0)
            self.assertTrue(r["cards"][0]["reviewable"])

    def test_unknown_number_and_new_name_have_no_automatic_candidate(self):
        d = fixture({"05756": "99999", "극동아파트": "신규 미등록 대여소"})
        c = extract_cards(d["tokens"], d["size"])["cards"][0]
        self.assertEqual(c["station_mapping"]["stations"]["origin"]["candidates"], [])
        self.assertFalse(c["valid"])

    def test_name_normalization_keeps_exit_numbers_distinct(self):
        r = resolve_stations(
            dict(start_station_id="00102", end_station_id="00104"),
            dict(origin="망원역 1번 출구 앞", destination="합정역 1번출구 앞"),
        )
        self.assertTrue(r["can_register"])
        r = resolve_stations(
            dict(start_station_id="102", end_station_id="104"),
            dict(origin="망원역 2번출구 앞", destination="합정역 1번출구 앞"),
        )
        self.assertFalse(r["can_register"])
        self.assertEqual(r["stations"]["origin"]["status"], "name_mismatch")


if __name__ == "__main__":
    unittest.main()
