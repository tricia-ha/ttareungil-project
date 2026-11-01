"""Normalize collected station schedules into separate service-day tables."""

from collections import Counter, defaultdict
import csv
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from map_schedule_collection import atomic_json, read_json, validate_schedule

LINE_NAMES = {
    **{200 + n: f"{n}호선" for n in range(1, 10)},
    210: "공항철도",
    211: "경의중앙선",
    212: "경춘선",
    213: "수인분당선",
    214: "신분당선",
    215: "경강선",
    216: "서해선",
    217: "인천1호선",
    218: "인천2호선",
    219: "에버라인",
    220: "의정부경전철",
    221: "우이신설선",
    222: "김포골드라인",
    223: "신림선",
    224: "GTX-A",
}
PROFILE_TAGS = {"weekday": "1", "saturday": "2", "holiday": "3"}
FIELDS = (
    "LINE_NUM",
    "STATION_CD",
    "STATION_NM",
    "TRAIN_NO",
    "WEEK_TAG",
    "INOUT_TAG",
    "BRANCH_LINE",
    "ORIGINSTATION",
    "DESTSTATION",
    "ARRIVETIME",
    "LEFTTIME",
    "VALID_FROM",
    "VALID_UNTIL",
    "TIMETABLE_METHOD",
    "ARRIVAL_INFERRED",
)


def station_name(value):
    value = re.sub(r"\([^)]*\)", "", str(value)).replace(" ", "").replace("·", ".")
    return {
        "서울": "서울역",
        "당고개": "불암산",
        "신길온천": "능길",
        "4.19민주묘지": "419민주묘지",
        "4ㆍ19민주묘지": "419민주묘지",
    }.get(value, value)


def calendar_profiles(calendar):
    name = calendar.get("name", "").replace(" ", "")
    known = {
        "평일": ("weekday",),
        "토요일": ("saturday",),
        "공휴일": ("holiday",),
        "일요일": ("holiday",),
        "일요일/공휴일": ("holiday",),
        "주말및공휴일": ("saturday", "holiday"),
    }
    # Do not guess special-date/seasonal calendars from an unfamiliar label.
    return known.get(name, ())


def _hms(seconds):
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def station_registry(lanes, existing):
    index = defaultdict(list)
    for key, row in existing.items():
        prefix = key.split(":", 1)[0]
        line = prefix + "호선" if prefix.isdigit() else prefix
        index[(line, station_name(row["name"]))].append((key, row))
    mapped, stops, station_lines = {}, {}, {}
    for lane in lanes:
        line = LINE_NAMES[int(lane["type"]["id"])]
        for row in lane["stations"]:
            sid = str(row["id"])
            matches = index[(line, station_name(row["name"]))]
            key = (
                matches[0][0]
                if len(matches) == 1
                else f"naver:{lane['type']['id']}:{sid}"
            )
            previous = matches[0][1] if len(matches) == 1 else {}
            point = dict(
                name=row["name"],
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                coordinate_source="naver_station_catalog",
            )
            if previous.get("coordinate_source"):
                point = dict(
                    previous
                )  # Preserve reviewed line-specific map corrections.
            mapped[sid], stops[key], station_lines[sid] = key, point, line
    return mapped, stops, station_lines


def normalize(lanes, schedules, existing):
    """Join exact train identities; never jump a missing intervening scheduled stop."""
    lanes = [r for r in lanes if int(r["type"]["id"]) in LINE_NAMES]
    routes = {str(r["id"]): r for r in lanes}
    mapped, stops, lines = station_registry(lanes, existing)
    neighbors = {}
    expected = defaultdict(set)
    for rid, route in routes.items():
        seq = [str(s["id"]) for s in route["stations"]]
        neighbors[rid] = {(a, b) for a, b in zip(seq, seq[1:])} | {
            (b, a) for a, b in zip(seq, seq[1:])
        }
        expected[LINE_NAMES[int(route["type"]["id"])]] |= set(seq)
    report, groups, profiles_seen = Counter(), defaultdict(list), defaultdict(set)
    snapshot_dates = []
    declared_profiles, unsupported_calendars = {}, set()
    fetched = set(schedules)
    complete_lines = {line for line, ids in expected.items() if ids <= fetched}
    for sid, data in schedules.items():
        if sid not in mapped:
            continue
        validate_schedule(data, sid)
        calendars = {c["id"]: calendar_profiles(c) for c in data["calendars"]}
        declared_profiles[sid] = {
            p for profiles in calendars.values() for p in profiles
        }
        if any(not profiles for profiles in calendars.values()):
            unsupported_calendars.add(sid)
        times = []
        for schedule in data["schedules"]:
            for direction in ("up", "down"):
                for record in schedule[direction]:
                    for k in ("arrivalTime", "departureTime"):
                        if record.get(k):
                            try:
                                times.append(datetime.fromisoformat(record[k]))
                            except (ValueError, TypeError):
                                report["invalid_timestamp"] += 1
        if not times:
            report["stations_without_events"] += 1
            continue
        anchor = min(t.date() for t in times)
        # Ignore the next-day part of overnight service when dating the snapshot.
        service_dates = [t.date() for t in times if t.hour >= 3]
        snapshot_dates.append(min(service_dates) if service_dates else anchor)
        for schedule in data["schedules"]:
            for profile in calendars[schedule["calendarId"]]:
                profiles_seen[lines[sid]].add(profile)
                profiles_seen[(lines[sid], sid)].add(profile)
                for direction in ("up", "down"):
                    for record in schedule[direction]:
                        rid, plate = (
                            str(record.get("routeId")),
                            str(record.get("plateNo") or "").strip(),
                        )
                        if rid not in routes or not plate:
                            report["unknown_route_or_train"] += 1
                            continue
                        try:
                            dep = datetime.fromisoformat(record["departureTime"])
                            arr = datetime.fromisoformat(
                                record.get("arrivalTime") or record["departureTime"]
                            )
                            midnight = datetime.combine(
                                anchor, datetime.min.time(), tzinfo=dep.tzinfo
                            )
                            a, d = (
                                int((arr - midnight).total_seconds()),
                                int((dep - midnight).total_seconds()),
                            )
                            if not 0 <= a <= d < 48 * 3600:
                                raise ValueError()
                            # The service day starts before the first morning train.
                            if d < 3 * 3600:
                                a, d = a + 86400, d + 86400
                            start, goal = (
                                str(record["startStopId"]),
                                str(record["headsignStopId"]),
                            )
                            if start not in mapped or goal not in mapped:
                                raise ValueError()
                        except (KeyError, ValueError, TypeError):
                            report["invalid_event"] += 1
                            continue
                        key = (lines[sid], profile, rid, plate, direction, start, goal)
                        groups[key].append((a, d, sid))
    inferred_groups, inference_report = complete_constant_arrivals(groups, neighbors)
    report.update(inference_report)
    rows, segment_counts = [], Counter()
    for (line, profile, rid, plate, direction, start, goal), records in groups.items():
        if line not in complete_lines:
            continue  # No publication of a half-collected line.
        events = set(records)
        if len({(a, sid) for a, d, sid in events}) != len(events):
            report["ambiguous_train_events"] += 1
            continue
        part, previous = 0, None
        for arrival, departure, sid in sorted(events):
            if previous is not None:
                prev_arrival, prev_departure, prev_sid = previous
                if (prev_sid, sid) not in neighbors[
                    rid
                ] or not 0 < arrival - prev_departure <= 1200:
                    part += 1
                    report["split_non_contiguous_trip"] += 1
                else:
                    segment_counts[(line, profile)] += 1
            rows.append(
                dict(
                    LINE_NUM=line,
                    STATION_CD=mapped[sid],
                    STATION_NM=stops[mapped[sid]]["name"],
                    TRAIN_NO=plate,
                    WEEK_TAG=PROFILE_TAGS[profile],
                    INOUT_TAG=direction,
                    BRANCH_LINE=f"naver:{rid}:{part}",
                    ORIGINSTATION=mapped[start],
                    DESTSTATION=mapped[goal],
                    ARRIVETIME=_hms(arrival),
                    LEFTTIME=_hms(departure),
                    VALID_FROM="",
                    VALID_UNTIL="",
                    TIMETABLE_METHOD=(
                        "collected_train_with_inferred_terminal_arrival"
                        if (line, profile, rid, plate, direction, start, goal)
                        in inferred_groups
                        else "collected_map_train_timetable"
                    ),
                    ARRIVAL_INFERRED=(
                        "1"
                        if sid == goal
                        and (line, profile, rid, plate, direction, start, goal)
                        in inferred_groups
                        else "0"
                    ),
                )
            )
            previous = arrival, departure, sid
    # A station-specific branch can explicitly operate on weekdays only. Its
    # absent weekend calendar is not fabricated into a weekend train service.
    complete_lines = {
        line
        for line in complete_lines
        if profiles_seen[line] == set(PROFILE_TAGS)
        and all(
            sid not in unsupported_calendars
            and declared_profiles.get(sid)
            and declared_profiles[sid] <= profiles_seen[(line, sid)]
            for sid in expected[line]
        )
    }
    usable_profiles = {
        line: [p for p in PROFILE_TAGS if segment_counts[(line, p)] > 0]
        for line in complete_lines
    }
    usable_profiles = {
        line: profiles for line, profiles in usable_profiles.items() if profiles
    }
    full_lines = sorted(
        line for line, profiles in usable_profiles.items() if len(profiles) == 3
    )
    available_lines = sorted(usable_profiles)
    rows = [
        r
        for r in rows
        if r["LINE_NUM"] in usable_profiles
        and r["WEEK_TAG"] in {PROFILE_TAGS[p] for p in usable_profiles[r["LINE_NUM"]]}
    ]
    report.update(rows=len(rows), complete_lines=len(full_lines))
    result = dict(report)
    result["complete_line_names"] = full_lines
    result["usable_profiles"] = usable_profiles
    result["inferred_arrivals_by_line"] = {
        line: {
            p: sum(k[0] == line and k[1] == p for k in inferred_groups)
            for p in PROFILE_TAGS
        }
        for line in available_lines
    }
    result["segments_by_line"] = {
        line: {p: segment_counts[(line, p)] for p in PROFILE_TAGS}
        for line in available_lines
    }
    result["snapshot_from"] = max(snapshot_dates).isoformat() if snapshot_dates else ""
    for row in rows:
        row["VALID_FROM"] = result["snapshot_from"]
    return rows, stops, available_lines, result


def complete_constant_arrivals(groups, neighbors):
    """Estimate only the last adjacent stop using invariant observed runtimes.

    No other train's event is joined to this train. Missing train identities,
    variable runtimes and unobserved final segments remain unavailable.
    """
    samples = defaultdict(lambda: defaultdict(set))
    for key, records in groups.items():
        line, profile, rid, plate, direction, start, goal = key
        events = sorted(set(records))
        if len({(a, sid) for a, d, sid in events}) != len(events):
            continue
        for a, b in zip(events, events[1:]):
            duration = b[0] - a[1]
            if (a[2], b[2]) in neighbors[rid] and 0 < duration <= 1200:
                arc = line, profile, rid, direction, a[2], b[2]
                samples[arc][duration].add(key)
    inferred, report = set(), Counter()
    for key, records in groups.items():
        line, profile, rid, plate, direction, start, goal = key
        events = sorted(set(records))
        if not events or goal in {r[2] for r in events}:
            continue
        last = events[-1]
        if (last[2], goal) not in neighbors[rid]:
            continue
        arc = line, profile, rid, direction, last[2], goal
        options = samples[arc]
        if len(options) != 1 or len(next(iter(options.values()))) < 2:
            report["terminal_arrivals_without_constant_evidence"] += 1
            continue
        arrival = last[1] + next(iter(options))
        if arrival >= 48 * 3600:
            continue
        records.append((arrival, arrival, goal))
        inferred.add(key)
    report["inferred_terminal_arrivals"] = len(inferred)
    return inferred, report


def compile_collected(folder):
    folder = Path(folder)
    root = folder / "map_timetables"
    checkpoint = root / "collection.json"
    if checkpoint.exists() and not read_json(checkpoint).get("complete"):
        raise ValueError("수집이 완료되지 않았습니다. 기존 사용 시간표는 유지됩니다.")
    lanes = read_json(root / "lanes.json.gz")
    selected = [r for r in lanes if int(r["type"]["id"]) in LINE_NAMES]
    ids = {str(s["id"]) for r in selected for s in r["stations"]}
    schedules, digest = {}, hashlib.sha256()
    digest.update((root / "lanes.json.gz").read_bytes())
    for sid in sorted(ids, key=int):
        path = root / "stops" / (sid + ".json.gz")
        if path.exists():
            digest.update(sid.encode())
            digest.update(path.read_bytes())
            schedules[sid] = path
    if len(schedules) != len(ids):
        raise ValueError("일부 역 시간표가 없습니다. 수집을 이어서 실행하세요.")
    prepared = read_json(folder / "prepared.json")
    from transit_coordinates import corrected_subway_stops

    existing = corrected_subway_stops(prepared["subway_stops"], folder)
    digest.update(json.dumps(existing, sort_keys=True).encode())
    digest.update(Path(__file__).read_bytes())
    signature = digest.hexdigest()[:20]
    version = root / "compiled" / signature
    if version.exists():
        saved = read_json(version / "snapshot.json")
        expected_files = ["stops.json"] + [
            value for tables in saved["tables"].values() for value in tables.values()
        ]
        if saved["signature"] != signature or not all(
            (version / name).is_file() for name in expected_files
        ):
            raise ValueError("기존 시간표 버전의 파일이 불완전합니다.")
        atomic_json(root / "manifest.json", saved)
        return saved
    schedules = {sid: read_json(path) for sid, path in schedules.items()}
    rows, stops, lines, report = normalize(selected, schedules, existing)
    staging = version.parent / ("_building-" + signature + "-" + uuid4().hex)
    staging.mkdir(parents=True)
    counts, tables = {}, {}
    ids_by_line = {name: code for code, name in LINE_NAMES.items()}
    for profile, tag in PROFILE_TAGS.items():
        grouped = defaultdict(list)
        for row in rows:
            if row["WEEK_TAG"] == tag:
                grouped[row["LINE_NUM"]].append(row)
        tables[profile] = {}
        for line in lines:
            relative = profile + "/" + str(ids_by_line[line]) + ".csv.gz"
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "wt", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(grouped[line])
            tables[profile][line] = relative
        counts[profile] = sum(len(v) for v in grouped.values())
    existing_lines = {
        k.split(":", 1)[0] + "호선"
        if k.split(":", 1)[0].isdigit()
        else k.split(":", 1)[0]
        for k in existing
    }
    # Existing full operator matrices include terminal arrivals omitted by the
    # map's station-departure view. Keep those complete sources, replace only
    # the three inferred services and add newly collected lines.
    active = sorted(
        set(lines)
        & ((set(lines) - existing_lines) | {"신분당선", "우이신설선", "신림선"})
    )
    active_stops = {r["STATION_CD"] for r in rows if r["LINE_NUM"] in active}
    atomic_json(staging / "stops.json", {k: stops[k] for k in active_stops})
    manifest = dict(
        schema=1,
        signature=signature,
        created_at=datetime.now().astimezone().isoformat(),
        version="compiled/" + signature,
        complete_lines=report["complete_line_names"],
        available_lines=lines,
        usable_profiles=report["usable_profiles"],
        active_lines=active,
        collected_stations=len(schedules),
        expected_stations=len(ids),
        rows_by_profile=counts,
        tables=tables,
        report=report,
        source="naver_public_station_timetable",
        terminal_arrivals_complete=False,
        snapshot_from=report["snapshot_from"],
    )
    atomic_json(staging / "snapshot.json", manifest)
    staging.rename(version)
    atomic_json(root / "manifest.json", manifest)
    return manifest


def collected_manifest(folder):
    root = Path(folder) / "map_timetables"
    path = root / "manifest.json"
    if not path.exists():
        return None
    manifest = read_json(path)
    version = (root / manifest["version"]).resolve()
    if not version.is_relative_to(root.resolve()):
        raise ValueError("Invalid timetable version path")
    manifest = dict(manifest, root=root, version_path=version)
    return manifest


def collected_rows(manifest, tags):
    if manifest is None or not manifest["active_lines"]:
        return
    for path in collected_table_paths(manifest, tags):
        with gzip.open(
            path,
            "rt",
            encoding="utf-8-sig",
            newline="",
        ) as stream:
            yield from csv.DictReader(stream)


def collected_table_paths(manifest, tags=None):
    tags = set(PROFILE_TAGS.values()) if tags is None else set(tags)
    for profile, tag in PROFILE_TAGS.items():
        if tag not in tags:
            continue
        for line in manifest["active_lines"]:
            path = (
                manifest["version_path"] / manifest["tables"][profile][line]
            ).resolve()
            if not path.is_relative_to(manifest["version_path"]):
                raise ValueError("Invalid timetable table path")
            yield path
