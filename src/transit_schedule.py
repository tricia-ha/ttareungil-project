"""Dated subway schedules and distance-weighted bus dispatch reconstruction."""

from collections import Counter, defaultdict
from itertools import chain
from functools import lru_cache
from datetime import date, datetime, timedelta
from pathlib import Path
import csv
import json
import math
import threading

from multimodal import Network, timestamp
from multimodal_data import KST, distance
from file_signature import file_signature


def clean(value):
    return str(value or "").replace("\ufeff", "").strip()


def read_csv(path):
    # The downloaded bus_raw.zip is actually a UTF-8 CSV, not a ZIP archive.
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return [
            {clean(k): clean(v) for k, v in r.items() if k is not None}
            for r in csv.DictReader(stream)
        ]


def clock_seconds(value):
    """Preserve HHMM digits in TOPIS strings such as 0.0600 and 0.0030."""
    value = clean(value)
    if value.startswith("0."):
        value = value[2:]
        if len(value) != 4 or not value.isdigit():
            raise ValueError("버스 시각은 0.HHMM 형식이어야 합니다.")
    if ":" in value:
        parts = [int(p) for p in value.split(":")]
        if len(parts) == 2:
            parts.append(0)
        if len(parts) != 3:
            raise ValueError("잘못된 시각 형식입니다.")
        h, m, s = parts
    else:
        if not value.isdigit() or len(value) > 6:
            raise ValueError("잘못된 시각 형식입니다.")
        value = value.zfill(4 if len(value) <= 4 else 6)
        h, m = int(value[:2]), int(value[2:4])
        s = int(value[4:]) if len(value) == 6 else 0
    if not (0 <= h < 48 and 0 <= m < 60 and 0 <= s < 60):
        raise ValueError("시각 범위를 확인하세요.")
    return h * 3600 + m * 60 + s


def service_seconds(value):
    seconds = clock_seconds(value)
    return seconds + 86400 if seconds < 3 * 3600 else seconds


@lru_cache(maxsize=8)
def korean_holiday_dates(year):
    try:
        import holidays
    except ImportError:
        raise ImportError(
            "공휴일 구분에 holidays가 필요합니다. pip install -r requirements.txt"
        ) from None
    return frozenset(holidays.KR(years=year))


def day_type(day, holiday_dates=None):
    if holiday_dates is None:
        holiday_dates = korean_holiday_dates(day.year)
    if day in holiday_dates or day.weekday() == 6:
        return "holiday"
    return "saturday" if day.weekday() == 5 else "weekday"


def service_days(departure, horizon_minutes):
    timestamp(departure)
    start = datetime.fromisoformat(departure).astimezone(KST)
    finish = start + timedelta(minutes=horizon_minutes)
    # Bus operation and subway service times may continue after midnight.
    days = []
    day = start.date() - timedelta(days=1)
    while day <= finish.date():
        days.append(day)
        day += timedelta(days=1)
    return start, finish, days


def reconstruct_bus(
    routes,
    route_stops,
    stops,
    departure,
    horizon_minutes=180,
    holiday_dates=None,
    distance_mode="segment",
):
    """One trip = one dispatch, with ordered stops and a shared vehicle identity.

    Missing coordinates invalidate the route, rather than jumping over stops.
    Runtime is the supplied whole-route runtime (minutes); segment durations are
    proportional to link lengths. This includes the source's stop-time allowance.
    """
    if distance_mode not in ("segment", "cumulative"):
        raise ValueError("distance_mode는 segment/cumulative입니다.")
    start, finish, days = service_days(departure, horizon_minutes)
    grouped = defaultdict(list)
    for row in route_stops:
        grouped[row["노선ID"]].append(row)
    edges, nodes, report = [], {}, Counter()
    missing_stop_ids = set()
    excluded_routes = []
    for route in routes:
        rid = route["노선ID"]
        if route.get("운행여부") not in ("", None, "1"):
            report["inactive_routes"] += 1
            continue
        seq = sorted(grouped.get(rid, []), key=lambda r: int(r["정류장순번"]))
        if len(seq) < 2:
            report["missing_sequence"] += 1
            continue
        if len({r["정류장순번"] for r in seq}) != len(seq):
            report["duplicate_sequence"] += 1
            continue
        orders = [int(r["정류장순번"]) for r in seq]
        if orders != list(range(1, len(orders) + 1)):
            report["incomplete_sequence"] += 1
            continue
        ids = [r["노드ID"] for r in seq]
        if any(s not in stops for s in ids):
            report["missing_coordinates"] += 1
            missing_stop_ids.update(s for s in ids if s not in stops)
            if len(excluded_routes) < 100:
                excluded_routes.append(
                    {
                        "route_id": rid,
                        "route_name": route.get("노선명", rid),
                        "reason": "missing_coordinates",
                        "missing_stops": [s for s in ids if s not in stops],
                    }
                )
            continue
        try:
            runtime = float(route["운행소요시간"]) * 60
            if not math.isfinite(runtime) or not 60 <= runtime <= 12 * 3600:
                raise ValueError()
            lengths = [float(r["링크거리누계"]) for r in seq]
            if distance_mode == "cumulative":
                lengths = [b - a for a, b in zip(lengths, lengths[1:])]
                if any(not math.isfinite(x) or x <= 0 for x in lengths):
                    raise ValueError()
            else:
                lengths = lengths[1:]  # each row's incoming link; first stop offset = 0
                lengths = [
                    v
                    if math.isfinite(v) and v > 0
                    else max(1, distance(stops[a], stops[b]))
                    for v, a, b in zip(lengths, ids, ids[1:])
                ]
            total = sum(lengths)
            offsets, running = [0], 0
            for value in lengths:
                running += value
                offsets.append(round(runtime * running / total))
            if any(b <= a for a, b in zip(offsets, offsets[1:])):
                raise ValueError()
        except (KeyError, ValueError, ZeroDivisionError):
            report["invalid_runtime_or_distance"] += 1
            continue
        used = False
        for day in days:
            kind = day_type(day, holiday_dates)
            suffix = {"weekday": "", "saturday": "(토요일)", "holiday": "(공휴일)"}[
                kind
            ]
            try:
                headway = float(route["배차" + suffix]) * 60
                first = clock_seconds(route["첫차시간" + suffix])
                last = clock_seconds(route["막차시간" + suffix])
                if not math.isfinite(headway) or not 30 <= headway <= 6 * 3600:
                    raise ValueError()
                if last < first:
                    last += 86400
            except (KeyError, ValueError):
                report["invalid_day_parameters"] += 1
                continue
            midnight = datetime.combine(day, datetime.min.time(), KST)
            # Generate only dispatches capable of intersecting the query window.
            lo = max(
                0,
                math.ceil(
                    ((start - midnight).total_seconds() - runtime - first) / headway
                ),
            )
            hi = min(
                math.floor((last - first) / headway),
                math.floor(((finish - midnight).total_seconds() - first) / headway),
            )
            # Do not invent an extra last departure if it is off the headway grid.
            for index in range(lo, hi + 1):
                dispatch = first + index * headway
                trip = f"bus:{day.isoformat()}:{rid}:{index}"
                for i, (a, b) in enumerate(zip(ids, ids[1:])):
                    dep = midnight + timedelta(seconds=dispatch + offsets[i])
                    arr = midnight + timedelta(seconds=dispatch + offsets[i + 1])
                    if dep < start or arr > finish:
                        continue
                    edges.append(
                        {
                            "from": "bus:" + a,
                            "to": "bus:" + b,
                            "mode": "bus",
                            "departure": dep.isoformat(),
                            "arrival": arr.isoformat(),
                            "trip_id": trip,
                            "route_id": rid,
                            "route_name": route.get("노선명", rid),
                            "stop_sequence": i,
                            "boarding_seconds": 15,
                        }
                    )
                    nodes["bus:" + a] = dict(stops[a], kind="stop", transit_mode="bus")
                    nodes["bus:" + b] = dict(stops[b], kind="stop", transit_mode="bus")
                    used = True
        report["routes_with_edges" if used else "routes_outside_window"] += 1
    report["edges"] = len(edges)
    return Network(nodes=nodes, transit=edges), dict(
        report,
        missing_stop_ids=sorted(missing_stop_ids),
        excluded_route_examples=excluded_routes,
    )


def _group_subway_rows(rows):
    grouped, report, invalid_trains = defaultdict(list), Counter(), set()
    for row in rows:
        key = tuple(
            clean(row.get(k))
            for k in (
                "LINE_NUM",
                "TRAIN_NO",
                "INOUT_TAG",
                "BRANCH_LINE",
                "ORIGINSTATION",
                "DESTSTATION",
                "VALID_FROM",
                "VALID_UNTIL",
                "TIMETABLE_METHOD",
                "WEEK_TAG",
            )
        )
        if not key[1]:
            report["missing_train_id"] += 1
            continue
        try:
            left, arrival = clean(row.get("LEFTTIME")), clean(row.get("ARRIVETIME"))
            if left in ("00:00:00", ""):
                left = arrival
            if arrival in ("00:00:00", ""):
                arrival = left
            if not left or left == "00:00:00":
                raise ValueError()
            dep = service_seconds(left)
            arr = service_seconds(arrival)
            # Origin/terminal may use 00:00:00 for an unavailable time.
            if row["LEFTTIME"] in ("00:00:00", ""):
                dep = arr
            if row["ARRIVETIME"] in ("00:00:00", ""):
                arr = dep
            if dep < arr:
                dep += 86400
            grouped[key].append((arr, dep, row["STATION_CD"]))
        except (ValueError, KeyError):
            report["invalid_time"] += 1
            invalid_trains.add(key)
    return grouped, report, invalid_trains


def subway_edges(
    rows,
    stops,
    departure,
    horizon_minutes=180,
    holiday_dates=None,
    _prepared_groups=None,
):
    """Join station schedules by train/line/direction/branch/terminal, not station code order."""
    start, finish, days = service_days(departure, horizon_minutes)
    grouped, base_report, invalid_trains = (
        _group_subway_rows(rows) if _prepared_groups is None else _prepared_groups
    )
    report = Counter(
        base_report
    )  # Cached groups never contain query-specific report counters.
    nodes, edges = {}, []
    for key, records in grouped.items():
        if key in invalid_trains:
            report["trains_with_invalid_times"] += 1
            continue
        seq = sorted(set(records))
        # Conflicting events at the same station/time invalidate the train group.
        if len({(a, s) for a, d, s in seq}) != len(seq):
            report["ambiguous_train"] += 1
            continue
        for day in days:
            tag = {"weekday": "1", "saturday": "2", "holiday": "3"}[
                day_type(day, holiday_dates)
            ]
            if key[-1] != tag:
                continue
            if key[-4] and day.isoformat() < key[-4]:
                continue
            if key[-3] and day.isoformat() > key[-3]:
                continue
            midnight = datetime.combine(day, datetime.min.time(), KST)
            trip = "subway:" + day.isoformat() + ":" + ":".join(key)
            for (_, dep, a), (arr, _, b) in zip(seq, seq[1:]):
                if a == b or a not in stops or b not in stops:
                    report["missing_coordinates_or_repeated_station"] += 1
                    continue
                if not 0 < arr - dep <= 20 * 60:
                    report["invalid_segment"] += 1
                    continue
                dt, at = (
                    midnight + timedelta(seconds=dep),
                    midnight + timedelta(seconds=arr),
                )
                if dt < start or at > finish:
                    continue
                edges.append(
                    {
                        "from": "subway:" + a,
                        "to": "subway:" + b,
                        "departure": dt.isoformat(),
                        "arrival": at.isoformat(),
                        "mode": "subway",
                        "trip_id": trip,
                        "route_id": key[0],
                        "route_name": key[0],
                        "timetable_method": key[-2] or "official_train_matrix",
                        "boarding_seconds": 120,
                    }
                )
                nodes["subway:" + a] = dict(
                    stops[a], kind="stop", transit_mode="subway"
                )
                nodes["subway:" + b] = dict(
                    stops[b], kind="stop", transit_mode="subway"
                )
    report["edges"] = len(edges)
    report = dict(report)
    report["edges_by_line"] = dict(Counter(e["route_id"] for e in edges))
    report["edges_by_method"] = dict(Counter(e["timetable_method"] for e in edges))
    return Network(nodes=nodes, transit=edges), report


_subway_load_lock = threading.Lock()


def _cached_subway_groups(folder, signature, tags):
    with _subway_load_lock:
        return _load_subway_groups(folder, signature, tags)


@lru_cache(maxsize=1)
def _load_subway_groups(folder, signature, tags):
    folder = Path(folder)
    payload = json.loads((folder / "prepared.json").read_text(encoding="utf-8"))
    if payload.get("subway_rows_file"):
        from transit_files import normalized_subway_rows
        from extended_rail import read_extended_rows

        rows = chain(
            normalized_subway_rows(folder / payload["subway_rows_file"], set(tags)),
            read_extended_rows(folder, set(tags)),
        )
    else:
        rows = payload["subway_rows"]
    return _group_subway_rows(rows)


def build_timetable(
    folder, departure, horizon_minutes=180, holiday_dates=None, include_bus=True
):
    folder = Path(folder)
    payload = json.loads((folder / "prepared.json").read_text(encoding="utf-8"))
    start, finish, _ = service_days(departure, horizon_minutes)
    review_due = date.fromisoformat(payload["valid_from"]) + timedelta(days=120)
    if finish.date() > review_due:
        raise ValueError(
            f"시간표 재검토 기한({review_due})을 넘었습니다. 최신 운영사 자료로 갱신하세요. 다운로드만 다시 한다고 자료 기준일이 바뀌지는 않습니다."
        )
    if not (
        payload["valid_from"] <= start.date().isoformat()
        and (
            payload["valid_until"] is None
            or finish.date().isoformat() <= payload["valid_until"]
        )
    ):
        raise ValueError(
            "시간표의 사용 날짜 범위를 벗어났습니다. 시간표 수집 셀을 다시 실행하세요."
        )
    required = {
        day_type(d, holiday_dates) for d in service_days(departure, horizon_minutes)[2]
    }
    if not required <= set(payload["day_types"]):
        raise ValueError(
            "필요한 요일 시간표가 없습니다. 시간표 수집 셀을 다시 실행하세요."
        )
    if include_bus:
        bus, bus_report = reconstruct_bus(
            read_csv(folder / "bus_raw.zip"),
            read_csv(folder / "bus_route_nodes.dat"),
            payload["bus_stops"],
            departure,
            horizon_minutes,
            holiday_dates,
            payload.get("distance_mode", "segment"),
        )
    else:
        bus, bus_report = (
            Network(nodes={}),
            {"edges": 0, "skipped_for_subway_mode": True},
        )
    tags = tuple(
        sorted(dict(weekday="1", saturday="2", holiday="3")[kind] for kind in required)
    )
    sources = [folder / "prepared.json", folder / "extended_rail" / "normalized.csv"]
    if payload.get("subway_rows_file"):
        sources.append(folder / payload["subway_rows_file"])
    groups = _cached_subway_groups(str(folder.resolve()), file_signature(sources), tags)
    subway, subway_report = subway_edges(
        (),
        payload["subway_stops"],
        departure,
        horizon_minutes,
        holiday_dates,
        _prepared_groups=groups,
    )
    # One mode may legitimately be out of service (e.g. after the subway last
    # train). Keep the other mode and bicycle/walking rather than aborting.
    network = Network(
        nodes=dict(bus.nodes, **subway.nodes),
        transit=bus.transit + subway.transit,
        metadata={
            "valid_departure": departure,
            "valid_horizon_minutes": horizon_minutes,
            "transit_type": "static_subway_and_reconstructed_bus",
            "bus_report": bus_report,
            "subway_report": subway_report,
            "timetable_review_due": review_due.isoformat(),
            "coverage_complete": False,
            "data_gaps": [
                "일부 버스 노선은 정류장 좌표·순서 누락으로 제외",
                "신분당·우이신설·신림 일부 종착역 도착 구간 미확보",
                "모든 수도권 철도 노선을 포함하지 않음",
            ],
            "preparation_report": payload.get("report", {}),
        },
    )
    network.validate()
    return network
