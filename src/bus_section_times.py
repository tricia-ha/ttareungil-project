"""Typical hourly bus section runtimes; these are not published departures."""

from collections import defaultdict
import csv
from datetime import date, timedelta
from functools import lru_cache
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
from statistics import median
import zipfile
import threading

_LOAD_LOCK = threading.Lock()


def section_key(rid, a, b, order):
    return f"{rid}|{a}|{b}|{order}|{order + 1}"


def prepare(archive, folder, valid_from, exclude_dates=()):
    from transit_schedule import read_csv, day_type

    folder, archive = Path(folder), Path(archive)
    grouped = defaultdict(list)
    for row in read_csv(folder / "bus_route_nodes.dat"):
        grouped[row["노선ID"]].append(row)
    allowed = set()
    for rid, rows in grouped.items():
        rows.sort(key=lambda r: int(r["정류장순번"]))
        if [int(r["정류장순번"]) for r in rows] != list(range(1, len(rows) + 1)):
            continue
        allowed.update(
            section_key(rid, a["노드ID"], b["노드ID"], i)
            for i, (a, b) in enumerate(zip(rows, rows[1:]), 1)
        )
    samples = defaultdict(lambda: defaultdict(lambda: [[] for _ in range(24)]))
    dates, matched, total, invalid = set(), 0, 0, 0
    kinds = {}
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.namelist():
            if not item.lower().endswith(".csv"):
                continue
            with zipped.open(item) as raw:
                for row in csv.DictReader(
                    io.TextIOWrapper(raw, encoding="cp949", newline="")
                ):
                    total += 1
                    stamp = row["기준_날짜"]
                    if stamp in exclude_dates:
                        continue
                    key = section_key(
                        row["노선_ID"],
                        row["출발_정류장_ID"],
                        row["도착_정류장_ID"],
                        int(row["출발_정류장_순서"]),
                    )
                    if (
                        key not in allowed
                        or int(row["도착_정류장_순서"])
                        != int(row["출발_정류장_순서"]) + 1
                    ):
                        continue
                    dates.add(stamp)
                    if stamp not in kinds:
                        kinds[stamp] = day_type(
                            date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:]))
                        )
                    slots = samples[key][kinds[stamp]]
                    matched += 1
                    for h in range(24):
                        value = float(row[f"운행시간_{h:02d}시"])
                        if math.isfinite(value) and value > 0:
                            slots[h].append(value)
                        elif value != 0:
                            invalid += 1
    records = {
        k: {
            kind: [round(median(v), 3) if v else None for v in slots]
            for kind, slots in profiles.items()
        }
        for k, profiles in samples.items()
    }
    return dict(
        schema=1,
        unit="seconds",
        statistic="median_of_daily_hourly_means",
        valid_from=valid_from,
        dates=sorted(dates),
        rows=total,
        matched_rows=matched,
        invalid_values=invalid,
        archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        source_url="https://data.seoul.go.kr/dataList/OA-21217/S/1/datasetView.do",
        sections=records,
    )


def load(folder):
    path = Path(folder) / "bus_section_times.json.gz"
    if not path.exists():
        return None
    with _LOAD_LOCK:
        return _load(str(path.resolve()), path.stat().st_mtime_ns)


@lru_cache(maxsize=2)
def _load(path, stamp):
    with gzip.open(path, "rt", encoding="utf8") as stream:
        data = json.load(stream)
    if data["unit"] != "seconds":
        raise ValueError("Unexpected bus section time unit")
    return data


def route_profiles(data, rid, ids, fallback):
    profiles = [
        data["sections"].get(section_key(rid, a, b, i), {})
        for i, (a, b) in enumerate(zip(ids, ids[1:]), 1)
    ]
    bound = sum(
        max(
            [fallback[i + 1] - fallback[i]]
            + [v for slots in p.values() for v in slots if v is not None]
        )
        for i, p in enumerate(profiles)
    )
    return profiles, bound


def trip_offsets(profiles, day, dispatch, fallback, holiday_dates=None):
    from transit_schedule import day_type

    values, observed = [0.0], 0
    kinds = {}
    for i, profile in enumerate(profiles):
        seconds = dispatch + values[-1]
        offset = int(seconds // 86400)
        if offset not in kinds:
            kinds[offset] = day_type(day + timedelta(days=offset), holiday_dates)
        hour = int(seconds % 86400 // 3600)
        found = profile.get(kinds[offset])
        duration = found[hour] if found else None
        if duration is None:
            duration = fallback[i + 1] - fallback[i]
        else:
            observed += 1
        values.append(values[-1] + duration)
    return values, observed
