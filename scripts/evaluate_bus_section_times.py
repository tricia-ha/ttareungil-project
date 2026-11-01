"""Temporal holdout of section-hour means, not passenger-level ETA accuracy."""

from collections import defaultdict
import argparse
import csv
import io
import json
from pathlib import Path
import sys
import zipfile
import math

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from bus_section_times import prepare, section_key  # noqa: E402
from transit_schedule import read_csv  # noqa: E402
from transit_coordinates import corrected_bus_stops  # noqa: E402
from multimodal_data import distance  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    folder = ROOT / "data/transit"
    payload = json.loads((folder / "prepared.json").read_text(encoding="utf8"))
    stops = corrected_bus_stops(payload["bus_stops"], folder)
    data = prepare(
        args.archive,
        folder,
        "2026-11-01",
        exclude_dates=("20261016", "20261017", "20261018"),
    )
    groups = defaultdict(list)
    for row in read_csv(folder / "bus_route_nodes.dat"):
        groups[row["노선ID"]].append(row)
    baseline = {}
    for route in read_csv(folder / "bus_raw.zip"):
        seq = sorted(groups[route["노선ID"]], key=lambda r: int(r["정류장순번"]))
        if [int(r["정류장순번"]) for r in seq] != list(range(1, len(seq) + 1)) or len(
            seq
        ) < 2:
            continue
        try:
            runtime = float(route.get("운행소요시간") or 0) * 60
        except (ValueError, TypeError):
            continue
        if not 60 <= runtime <= 12 * 3600:
            continue
        if any(r["노드ID"] not in stops for r in seq):
            continue
        lengths = [float(r["링크거리누계"]) for r in seq[1:]]
        lengths = [
            v
            if math.isfinite(v) and v > 0
            else max(1, distance(stops[a["노드ID"]], stops[b["노드ID"]]))
            for v, a, b in zip(lengths, seq, seq[1:])
        ]
        total = sum(lengths)
        running = 0
        offsets = [0]
        for value in lengths:
            running += value
            offsets.append(round(runtime * running / total))
        for i, (a, b) in enumerate(zip(seq, seq[1:]), 1):
            baseline[section_key(route["노선ID"], a["노드ID"], b["노드ID"], i)] = (
                offsets[i] - offsets[i - 1]
            )
    count = covered = 0
    old_error = new_error = 0.0
    with zipfile.ZipFile(args.archive) as archive:
        for filename in archive.namelist():
            if not filename.endswith(".csv"):
                continue
            with archive.open(filename) as raw:
                for row in csv.DictReader(
                    io.TextIOWrapper(raw, encoding="cp949", newline="")
                ):
                    if row["기준_날짜"] != "20261016":
                        continue
                    key = section_key(
                        row["노선_ID"],
                        row["출발_정류장_ID"],
                        row["도착_정류장_ID"],
                        int(row["출발_정류장_순서"]),
                    )
                    if key not in baseline:
                        continue
                    predicted = data["sections"].get(key, {}).get("weekday")
                    for h in range(24):
                        actual = float(row[f"운행시간_{h:02d}시"])
                        if actual <= 0:
                            continue
                        value = predicted[h] if predicted else None
                        covered += value is not None
                        count += 1
                        old_error += abs(baseline[key] - actual)
                        new_error += abs(
                            (baseline[key] if value is None else value) - actual
                        )
    result = dict(
        training_dates=data["dates"],
        test_date="20261016",
        evaluated_section_hours=count,
        covered_section_hours=covered,
        baseline_mae_seconds=old_error / count,
        historical_mae_seconds=new_error / count,
        reduction_percent=100 * (old_error - new_error) / old_error,
        limitation="Hourly section aggregates; not whole-journey or individual passenger errors.",
    )
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf8")
    print(text)
