"""Compile already downloaded official section statistics, separately from web departure tables."""

from pathlib import Path
import argparse
from datetime import datetime
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from bus_section_times import prepare  # noqa: E402
from map_schedule_collection import atomic_json  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    result = prepare(
        args.archive,
        ROOT / "data/transit",
        datetime.now().astimezone().date().isoformat(),
    )
    atomic_json(ROOT / "data/transit/bus_section_times.json.gz", result)
    print(json.dumps({k: v for k, v in result.items() if k != "sections"}))
    print("matched_sections", len(result["sections"]))
