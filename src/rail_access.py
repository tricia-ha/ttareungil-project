"""Published platform-transfer times; unknown entry times keep their fallback."""

import csv
from pathlib import Path
from extended_rail import name
from multimodal_data import distance


def complex_name(value):
    value = name(value)
    return {"총신대입구": "이수"}.get(value, value)


def line_name(value):
    value = str(value).strip()
    return value + "호선" if value.isdigit() else value


def transfer_walks(nodes, transit, folder):
    published = {}
    path = Path(folder) / "rail_transfer_times.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                mm, ss = map(int, row["환승소요시간"].split(":"))
                if mm < 0 or not 0 <= ss < 60 or mm * 60 + ss <= 0:
                    raise ValueError("Invalid published rail transfer time")
                key = (
                    complex_name(row["환승역명"]),
                    line_name(row["호선"]),
                    line_name(row["환승노선"]),
                )
                value = (mm * 60 + ss, float(row["환승거리"]))
                if key in published and published[key] != value:
                    raise ValueError("Conflicting published rail transfer")
                published[key] = value
    lines, complexes = {}, {}
    for edge in transit:
        if edge["mode"] == "subway":
            for key in (edge["from"], edge["to"]):
                lines.setdefault(key, set()).add(line_name(edge.get("route_id", "")))
    for key in lines:
        if key in nodes:
            complexes.setdefault(complex_name(nodes[key].get("name", "")), []).append(
                key
            )
    walks = []
    for station, platforms in complexes.items():
        if not station:
            continue
        for a in platforms:
            for b in platforms:
                if a == b:
                    continue
                pair = next(
                    (
                        (la, lb)
                        for la in lines[a]
                        for lb in lines[b]
                        if (station, la, lb) in published
                    ),
                    None,
                )
                separation = distance(nodes[a], nodes[b])
                if pair:
                    seconds, length = published[(station, *pair)]
                    minutes, source = seconds / 60, "published_rail_transfer_time"
                elif separation <= 700:
                    minutes, length, source = (
                        3 + separation / 80,
                        separation,
                        "rail_interchange_estimate",
                    )
                else:
                    continue
                walks.append(
                    dict(
                        **{"from": a, "to": b},
                        minutes=minutes,
                        source=source,
                        distance_m=length,
                        geometry=None,
                        platform_access_complete=True,
                        platform_transfer=True,
                        entry_fallback_seconds=120,
                    )
                )
    return walks
