"""Static dataset adapters: leading-zero IDs, day types, terminal null times."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import csv
import tempfile
import unittest
from transit_files import normalized_subway_rows, station_name
from transit_schedule import subway_edges


class StaticFileTests(unittest.TestCase):
    def test_file_normalization_and_terminal_times(self):
        fields = [
            "고유번호",
            "호선",
            "역사코드",
            "역사명",
            "주중주말",
            "방향",
            "급행여부",
            "열차코드",
            "열차도착시간",
            "열차출발시간",
            "출발역",
            "도착역",
        ]
        rows = [
            [
                "1",
                "1",
                "0150",
                "서울역",
                "DAY",
                "UP",
                "0",
                "K1",
                "",
                "08:00:00",
                "서울역",
                "종각",
            ],
            [
                "2",
                "1",
                "0152",
                "종각",
                "DAY",
                "UP",
                "0",
                "K1",
                "08:05:00",
                "",
                "서울역",
                "종각",
            ],
            [
                "3",
                "1",
                "0150",
                "서울역",
                "SAT",
                "UP",
                "0",
                "K2",
                "",
                "09:00:00",
                "서울역",
                "종각",
            ],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timetable.csv"
            with path.open("w", encoding="cp949", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(fields)
                writer.writerows(rows)
            normalized = list(normalized_subway_rows(path, {"1"}))
            self.assertEqual(len(normalized), 2)
            self.assertEqual(normalized[0]["STATION_CD"], "1:0150")
            stops = {
                r["STATION_CD"]: {
                    "name": r["STATION_NM"],
                    "latitude": 37.5,
                    "longitude": 127,
                }
                for r in normalized
            }
            network, _ = subway_edges(
                normalized, stops, "2026-10-16T07:55:00+09:00", 30, set()
            )
            self.assertEqual(len(network.transit), 1)
            self.assertEqual(network.transit[0]["arrival"], "2026-10-16T08:05:00+09:00")

    def test_names_link_documented_renamings(self):
        self.assertEqual(station_name("서울역"), station_name("서울"))
        self.assertEqual(station_name("뚝섬유원지"), station_name("자양"))


if __name__ == "__main__":
    unittest.main()
