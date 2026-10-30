"""Reviewed exact-station corrections; never overwrite an updated source point."""

import json
from pathlib import Path


def corrected_subway_stops(stops, folder):
    path = Path(folder) / "files" / "subway_coordinate_corrections.json"
    if not path.is_file():
        return stops
    output = dict(stops)
    for row in json.loads(path.read_text(encoding="utf-8"))["corrections"]:
        current = stops.get(row["station_key"])
        if current is None or current.get("name") != row["name"]:
            continue
        # A later dataset can have its own improved platform/entrance point.
        if any(
            float(current[k]) != float(row["original"][k])
            for k in ("latitude", "longitude")
        ):
            continue
        output[row["station_key"]] = dict(
            current, **row["corrected"], coordinate_source=row["source"]
        )
    return output
