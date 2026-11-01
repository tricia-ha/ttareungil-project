"""Attach query-local positions only when a live station moved from the model catalog."""

import csv
import json
from pathlib import Path


def routing_station_catalog(catalog, updates=None):
    """Use current station locations for routing without rewriting historical data."""
    if updates is None:
        from project_paths import PROJECT_ROOT

        updates = PROJECT_ROOT / "data/inventory/station_location_updates.json"
    with Path(catalog).open(encoding="utf-8-sig", newline="") as stream:
        stations = list(csv.DictReader(stream))
    if not Path(updates).is_file():
        return stations
    payload = json.loads(Path(updates).read_text(encoding="utf-8"))
    for row in stations:
        current = payload["stations"].get(str(row["station_id"]))
        if row.get("catalog_as_of", "")[:10] > payload.get("snapshot_at", "")[:10]:
            continue
        if current is None or different_coordinates(row, current["original"]):
            continue  # Do not replace a newer/custom catalog with an old snapshot.
        if different_coordinates(row, current["current"]):
            row["eta_catalog_latitude"] = float(row["latitude"])
            row["eta_catalog_longitude"] = float(row["longitude"])
        row.update(current["current"])
    return stations


def different_coordinates(a, b):
    # CSV/JSON floats can differ at the last binary digit (observed ~1e-9 m).
    # Twelve decimal degrees remove that serialization noise only.
    return any(
        round(float(a[k]), 12) != round(float(b[k]), 12)
        for k in ("latitude", "longitude")
    )


def bicycle_coordinates(edge, source, target):
    for side, node in (("from", source), ("to", target)):
        if "eta_catalog_latitude" not in node or "eta_catalog_longitude" not in node:
            continue
        current = (float(node["latitude"]), float(node["longitude"]))
        original = (
            float(node["eta_catalog_latitude"]),
            float(node["eta_catalog_longitude"]),
        )
        if any(round(a, 12) != round(b, 12) for a, b in zip(current, original)):
            edge[f"eta_{side}_latitude"], edge[f"eta_{side}_longitude"] = current
    return edge
