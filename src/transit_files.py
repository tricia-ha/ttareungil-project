"""Prepare downloaded public datasets locally; no network or API key required."""

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from multimodal_data import KST
from transit_prepare import coordinate, normalized_name, save_json
from transit_schedule import clean, service_days

FILES = (
    "subway_timetable.csv",
    "subway_coordinates.csv",
    "subway_line9_coordinates.csv",
    "bus_coordinates.xlsx",
    "national_rail_stations.xlsx",
)


def csv_rows(path):
    """Both official CSV downloads are CP949; tolerate later UTF-8 replacements."""
    with Path(path).open("rb") as stream:
        sample = stream.read(4096)
    try:
        sample.decode("utf-8-sig")
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        encoding = "cp949"
    with Path(path).open(encoding=encoding, newline="") as stream:
        for row in csv.DictReader(stream):
            yield {clean(k): clean(v) for k, v in row.items() if k is not None}


def station_name(value):
    value = normalized_name(value)
    # Coordinate CSV calls Seoul station '서울', the timetable calls it '서울역'.
    value = value[:-1] if value.endswith("역") else value
    return {"뚝섬유원지": "자양", "당고개": "불암산", "신길온천": "능길"}.get(
        value, value
    )


def normalized_subway_rows(path, tags=None):
    days = {"DAY": "1", "SAT": "2", "END": "3"}
    for row in csv_rows(path):
        tag = days.get(row["주중주말"])
        if tag is None or (tags is not None and tag not in tags):
            continue
        line = row["호선"]
        yield {
            "LINE_NUM": line + "호선",
            "STATION_CD": line + ":" + row["역사코드"],
            "STATION_NM": row["역사명"],
            "TRAIN_NO": row["열차코드"],
            "WEEK_TAG": tag,
            "INOUT_TAG": row["방향"],
            "BRANCH_LINE": "",
            "EXPRESS_YN": row["급행여부"],
            "ORIGINSTATION": row["출발역"],
            "DESTSTATION": row["도착역"],
            "ARRIVETIME": row["열차도착시간"],
            "LEFTTIME": row["열차출발시간"],
        }


def prepare_transit_files(folder, departure, horizon_minutes=180, progress=print):
    import pandas as pd

    folder = Path(folder)
    for name in FILES:
        if not (folder / "files" / name).is_file():
            raise FileNotFoundError(
                "공식 파일이 없습니다: "
                + str(folder / "files" / name)
                + " / scripts/download_transit_files.py로 내려받으세요."
            )
    start, finish, _ = service_days(departure, horizon_minutes)
    from extended_rail import prepare_extended_rail

    extended = prepare_extended_rail(folder, progress)
    source_date = "2026-09-01"  # Reference date of the downloaded 15098251 release.
    if start.date().isoformat() < source_date:
        raise ValueError("시간표 기준일보다 앞선 날짜입니다: " + source_date)
    inputs = [folder / "files" / n for n in FILES] + [
        folder / n for n in ("bus_raw.zip", "bus_route_nodes.dat")
    ]
    supplement = folder / "files" / "bus_coordinates_supplement.csv"
    if supplement.exists():
        inputs.append(supplement)
    fingerprints = {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in inputs
    }
    fingerprints["parser"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if extended:
        fingerprints["extended_rail"] = extended["signature"]
    output = folder / "prepared.json"
    if output.is_file():
        cached = json.loads(output.read_text(encoding="utf-8"))
        if (
            cached.get("schema") == 3
            and cached.get("file_fingerprints") == fingerprints
        ):
            if progress:
                progress("저장된 파일 기반 시간표·좌표 재사용", flush=True)
            return cached["report"]
    bus = pd.read_excel(folder / "files" / "bus_coordinates.xlsx", dtype=str).fillna("")
    bus_stops = {}
    for row in bus.to_dict("records"):
        sid = clean(row["NODE_ID"])
        if not (len(sid) == 9 and sid.isdigit()):
            continue
        try:
            bus_stops[sid] = dict(coordinate(row), name=clean(row["정류소명"]))
        except ValueError:
            continue
    by_code, by_name = {}, defaultdict(set)
    if supplement.exists():
        for row in csv_rows(supplement):
            sid = clean(row["NODE_ID"])
            if sid not in bus_stops:
                bus_stops[sid] = dict(
                    coordinate(row),
                    name=clean(row["정류소명"]),
                    coordinate_source="official_topis_exact_node_id",
                )
    for row in csv_rows(folder / "files" / "subway_coordinates.csv"):
        point = coordinate(row)
        line = clean(row["호선"])
        xy = (point["latitude"], point["longitude"])
        by_name[(line, station_name(row["역명"]))].add(xy)
        by_code[(line, clean(row["고유역번호(외부역코드)"]).lstrip("0"))] = xy
    for row in csv_rows(folder / "files" / "subway_line9_coordinates.csv"):
        point = coordinate(row)
        by_name[("9", station_name(row["역명"]))].add(
            (point["latitude"], point["longitude"])
        )
    national = pd.read_excel(
        folder / "files" / "national_rail_stations.xlsx", dtype=str
    ).fillna("")
    line_aliases = {
        "경부선": "1",
        "경원선": "1",
        "경인선": "1",
        "장항선": "1",
        "일산선": "3",
        "안산과천선": "4",
        "진접선": "4",
        "도시철도7호선": "7",
    }
    supplement = defaultdict(set)
    for row in national.to_dict("records"):
        route = clean(row["노선명"]).replace(" ", "")
        match = re.fullmatch(r"([1-9])호선", route)
        line = match.group(1) if match else line_aliases.get(route)
        if line is None and route in ("서울도시철도9호선", "수도권도시철도9호선"):
            line = "9"
        if line is None and route == "수도권광역철도8호선":
            line = "8"
        if line is None:
            continue
        try:
            point = coordinate({"LAT": row["역위도"], "LOT": row["역경도"]})
        except ValueError:
            continue
        supplement[(line, station_name(row["역사명"]))].add(
            (point["latitude"], point["longitude"])
        )
    for key, options in supplement.items():
        if key not in by_name:
            by_name[key] = options
    stations, counts = {}, Counter()
    for row in csv_rows(folder / "files" / "subway_timetable.csv"):
        key = (row["호선"], row["역사코드"])
        stations[key] = row["역사명"]
        counts[row["주중주말"]] += 1
    subway_stops, unmatched = {}, []
    for (line, code), name in stations.items():
        options = by_name.get((line, station_name(name)), set())
        direct = by_code.get((line, code.lstrip("0")))
        # Exact code is used only when it does not contradict the name match.
        if direct is not None and direct in options:
            xy = direct
        elif len(options) == 1:
            xy = next(iter(options))
        else:
            unmatched.append({"line": line, "station_code": code, "name": name})
            continue
        subway_stops[line + ":" + code] = {
            "name": name,
            "latitude": xy[0],
            "longitude": xy[1],
        }
    if not bus_stops or not subway_stops:
        raise ValueError("좌표 파일의 열 이름과 좌표값을 확인하세요.")
    if extended:
        subway_stops.update(extended["stops"])
    report = {
        "source": "downloaded_files",
        "bus_stops": len(bus_stops),
        "subway_stops": len(subway_stops),
        "subway_stations_in_timetable": len(stations),
        "subway_timetable_rows": sum(counts.values()),
        "day_type_rows": dict(counts),
        "subway_unmatched_stations": unmatched,
        "new_api_calls": 0,
        "timetable_reference_date": source_date,
        "sources": ["OA-15067", "15098251", "15099316", "15041335", "KRIC-32"],
        "extended_rail": {k: v for k, v in extended.items() if k != "stops"}
        if extended
        else None,
    }
    save_json(
        output,
        {
            "schema": 3,
            "created_at": datetime.now(KST).isoformat(),
            "valid_from": source_date,
            "valid_until": None,
            "day_types": ["weekday", "saturday", "holiday"],
            "subway_rows_file": "files/subway_timetable.csv",
            "distance_mode": "segment",
            "file_fingerprints": fingerprints,
            "bus_stops": bus_stops,
            "subway_stops": subway_stops,
            "report": report,
        },
    )
    save_json(folder / "preparation_report.json", report)
    if progress:
        progress(
            f"파일 준비 완료: 버스 정류장 {len(bus_stops):,} / 지하철 {len(subway_stops):,} / 시간표 {sum(counts.values()):,}행",
            flush=True,
        )
    return report
