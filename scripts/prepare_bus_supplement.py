"""Extract unambiguous active stop coordinates from official TOPIS node CSV."""

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path


def build(source, destination):
    candidates = defaultdict(set)
    names = {}
    clean = lambda value: str(value or "").replace("\ufeff", "").strip()
    with source.open(encoding="utf-8-sig", newline="") as stream:
        for raw in csv.DictReader(stream):
            row = {clean(k): clean(v) for k, v in raw.items()}
            if row["사용여부"] != "1" or row["노드유형"] != "0":
                continue
            try:
                lon, lat = float(row["좌표X"]), float(row["좌표Y"])
            except ValueError:
                continue
            if not (124 <= lon <= 132 and 33 <= lat <= 39):
                continue
            sid = row["노드ID"]
            if len(sid) != 9 or not sid.isdigit():
                continue
            candidates[sid].add((lat, lon))
            names[sid] = row["노드명"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["NODE_ID", "LAT", "LOT", "정류소명"])
        for sid, coords in sorted(candidates.items()):
            if len(coords) != 1:
                continue  # Never guess between conflicting versions.
            lat, lon = next(iter(coords))
            writer.writerow([sid, lat, lon, names[sid]])
    report = {
        "source": "서울시 TOPIS 노드정보 CSV (노드유형 0, 사용여부 1)",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "rows": sum(len(v) == 1 for v in candidates.values()),
        "conflicting_ids_excluded": sum(len(v) > 1 for v in candidates.values()),
        "matching": "exact NODE_ID only; no coordinate inference",
    }
    destination.with_suffix(".provenance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    build(
        args.source,
        Path(__file__).resolve().parents[1]
        / "data/transit/files/bus_coordinates_supplement.csv",
    )
