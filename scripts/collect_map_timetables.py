"""Collect weekday/Saturday/holiday timetables; completed stations are reused."""

from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from map_schedule_collection import collect  # noqa: E402


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder", type=Path, default=ROOT / "data/transit/map_timetables"
    )
    parser.add_argument("--max-requests", type=int, default=1000)
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--compile", action="store_true", help="검증한 수집본을 요일별 시간표로 변환"
    )
    args = parser.parse_args()
    if args.max_requests < 0 or args.interval < 1.0:
        parser.error("max-requests >= 0, interval >= 1.0 required")
    result = collect(args.folder, args.max_requests, args.interval, args.refresh)
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "stations"}, ensure_ascii=False
        )
    )
    if args.compile:
        if result.get("complete"):
            from collected_rail import compile_collected

            summary = compile_collected(args.folder.parent)
            print(json.dumps(summary, ensure_ascii=False))
        else:
            print("기존 사용 시간표를 유지합니다. 다시 실행하면 저장 지점부터 이어집니다.")
