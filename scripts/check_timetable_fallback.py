"""Hide known train identities and evaluate the existing minute-table matcher."""

from collections import defaultdict
import argparse
import csv
import gzip
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from collected_rail import collected_manifest, LINE_NAMES  # noqa: E402
from extended_rail import inferred_station_rows, name  # noqa: E402
from map_schedule_collection import read_json  # noqa: E402
from transit_schedule import clock_seconds  # noqa: E402


def evaluate(folder):
    manifest = collected_manifest(folder)
    lanes = read_json(manifest["root"] / "lanes.json.gz")
    stops = read_json(manifest["version_path"] / "stops.json")
    results = []
    for code in (214, 221, 223, 219):
        line = LINE_NAMES[code]
        route = next(
            r for r in lanes if r["type"]["id"] == code and not r.get("express")
        )
        order = [name(s["name"]) for s in route["stations"]]
        adjacent = set(zip(order, order[1:])) | set(zip(order[1:], order))
        catalog = {(line, name(s["name"])): s for s in route["stations"]}
        for profile in ("weekday", "saturday", "holiday"):
            path = manifest["version_path"] / manifest["tables"][profile][line]
            with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            if not rows:
                results.append(
                    dict(line=line, profile=profile, status="no_identified_trains")
                )
                continue
            events, truth = defaultdict(set), defaultdict(dict)
            for row in rows:
                if row.get("ARRIVAL_INFERRED") == "1":
                    continue  # Inferred arrivals are never evaluation ground truth.
                st = name(stops[row["STATION_CD"]]["name"])
                direction = 0 if row["INOUT_TAG"] == "up" else 1
                if line == "신림선":
                    direction = 1 - direction
                dep = clock_seconds(row["LEFTTIME"])
                minute = dep // 60 * 60
                goal = name(stops[row["DESTSTATION"]]["name"])
                identity = (
                    row["TRAIN_NO"],
                    row["BRANCH_LINE"],
                    row["ORIGINSTATION"],
                    row["DESTSTATION"],
                )
                events[(st, direction)].add((minute, goal))
                truth[(st, direction, minute, goal)][identity] = (
                    clock_seconds(row["ARRIVETIME"]),
                    dep,
                )
            tables = [
                (st, {(0, d): sorted(events[(st, d)]) for d in (0, 1)}) for st in order
            ]
            with (
                patch("extended_rail.station_tables", return_value=tables),
                patch("extended_rail.station_catalog", return_value=catalog),
            ):
                inferred = list(
                    inferred_station_rows(Path(folder) / "extended_rail", line)
                )
            groups = defaultdict(list)
            for row in inferred:
                groups[(row["TRAIN_NO"], int(row["INOUT_TAG"]))].append(row)
            checked = wrong = ambiguous = nonadjacent = 0
            errors = []
            for (_, direction), trip in groups.items():
                trip.sort(key=lambda r: clock_seconds(r["LEFTTIME"]))
                for a, b in zip(trip, trip[1:]):
                    a_st, b_st = a["STATION_NM"], b["STATION_NM"]
                    a_t, b_t = (
                        clock_seconds(a["LEFTTIME"]),
                        clock_seconds(b["LEFTTIME"]),
                    )
                    goal = a["DESTSTATION"]
                    left, right = (
                        truth[(a_st, direction, a_t, goal)],
                        truth[(b_st, direction, b_t, goal)],
                    )
                    checked += 1
                    nonadjacent += (a_st, b_st) not in adjacent
                    shared = left.keys() & right.keys()
                    if not shared:
                        wrong += 1
                    elif len(left) != 1 or len(right) != 1:
                        ambiguous += 1
                    else:
                        identity = next(iter(shared))
                        actual = right[identity][0] - left[identity][1]
                        errors.append(abs((b_t - a_t) - actual))
            results.append(
                dict(
                    line=line,
                    profile=profile,
                    status="evaluated",
                    inferred_segments=checked,
                    wrong_train_pairs=wrong,
                    ambiguous_pairs=ambiguous,
                    nonadjacent_pairs=nonadjacent,
                    travel_time_mae_seconds=round(sum(errors) / len(errors), 2)
                    if errors
                    else None,
                    wrong_pair_rate=round(wrong / checked, 6) if checked else None,
                )
            )
    return dict(
        signature=manifest["signature"],
        method="existing_minute_matcher_masked_train_identity",
        results=results,
    )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(ROOT / "data/transit")
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
