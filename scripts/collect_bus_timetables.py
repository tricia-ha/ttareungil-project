"""Collect posted bus tables separately from route searches."""

from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from bus_web_timetables import collect  # noqa: E402
from project_paths import RUNTIME_CACHE_DIR  # noqa: E402

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir", type=Path, default=RUNTIME_CACHE_DIR / "bus_web_timetables"
    )
    parser.add_argument("--max-requests", type=int, default=40)
    args = parser.parse_args()
    result = collect(
        args.cache_dir, ROOT / "data/transit/bus_web_timetables.json", args.max_requests
    )
    print(json.dumps(result, ensure_ascii=False))
