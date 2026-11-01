"""Collect published bus tables; never turn headways into claimed web schedules."""

from collections import defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit
from datetime import datetime, timezone
from functools import lru_cache

import requests
from bs4 import BeautifulSoup

from map_schedule_collection import atomic_json

HOST = "https://seoulairbus.co.kr"
CITY_URL = "https://scpm.seoul.go.kr/seoul-policy/evt0196"
ROUTES_URL = HOST + "/bus/6009"


def minutes(value):
    match = re.fullmatch(r"(\d{1,2}):([0-5]\d)", value.strip())
    if not match or int(match[1]) >= 48:
        raise ValueError("Unexpected published bus time")
    return int(match[1]) * 3600 + int(match[2]) * 60


def matrix(html, station_count):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.table")
    if table is None:
        raise ValueError("Bus timetable table missing")
    headers = [c.get_text(" ", strip=True) for c in table.select("thead th")]
    if len(headers) != station_count + 5 or headers[-4:] != [
        "T1 도착",
        "T2 도착",
        "T2 출발",
        "T1 출발",
    ]:
        raise ValueError("Bus timetable column layout changed")
    trips = []
    for row in table.select("tbody tr"):
        cells = [
            c.get_text(" ", strip=True) for c in row.find_all("td", recursive=False)
        ]
        if len(cells) != len(headers):
            raise ValueError("Incomplete published bus row")
        points = [minutes(v) if v else None for v in cells[1:]]
        # Arrival/outbound and departure/inbound columns are separate trips.
        # The displayed row number is not a vehicle identity linking both legs.
        outbound = points[: station_count + 2]
        observed = [v for v in outbound if v is not None]
        if observed and (
            observed != sorted(observed) or len(set(observed)) != len(observed)
        ):
            raise ValueError("Non-monotone published outbound trip")
        if len(observed) >= 2:
            trips.append(dict(number=cells[0], outbound=outbound))
    return dict(headers=headers[1:], trips=trips)


def city_tables(html):
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if len(tables) != 4:
        raise ValueError("City bus timetable layout changed")
    result = []
    for number, schedule_idx, stop_idx in [("동대문A01", 0, 1), ("서대문A01", 2, 3)]:
        departures = []
        for row in (
            tables[schedule_idx].select("tbody tr")
            or tables[schedule_idx].select("tr")[1:]
        ):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) < 2:
                continue
            match = re.search(
                r"(\d{2}:\d{2})\s*[~～]\s*(\d{2}:\d{2})",
                cells[1].get_text(" ", strip=True),
            )
            if match:
                departures.append((minutes(match[1]), minutes(match[2])))
        stops = []
        for row in tables[stop_idx].select("tr"):
            cells = [
                c.get_text(" ", strip=True)
                for c in row.find_all(["td", "th"], recursive=False)
            ]
            for offset in range(0, len(cells), 3):
                chunk = cells[offset : offset + 3]
                if (
                    len(chunk) == 3
                    and chunk[0].isdigit()
                    and re.fullmatch(r"\d{5}", chunk[2])
                ):
                    stops.append((int(chunk[0]), chunk[1], chunk[2]))
        stops.sort()
        if not departures or not stops:
            raise ValueError("No explicit city-bus departures or ordered stops")
        result.append(
            dict(
                route_number=number,
                source_url=CITY_URL,
                calendars=["weekday"],
                departures=departures,
                stops=stops,
            )
        )
    return result


def collect(cache_dir, output, max_requests=40, interval=1.5):
    cache_dir, output = Path(cache_dir), Path(output)
    cache_dir.mkdir(parents=True, exist_ok=True)
    calls, last = 0, 0.0
    session = requests.Session()

    def fetch(url):
        nonlocal calls, last
        if urlsplit(url).netloc not in {"seoulairbus.co.kr", "scpm.seoul.go.kr"}:
            raise ValueError("Unexpected timetable host")
        path = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".html.gz")
        if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
            return gzip.decompress(path.read_bytes())
        if calls >= max_requests:
            raise RuntimeError("bus_timetable_request_budget; rerun to resume")
        time.sleep(max(0, interval - (time.monotonic() - last)))
        last = time.monotonic()
        calls += 1
        response = session.get(url, timeout=(5, 30))
        response.raise_for_status()  # No retries or access workarounds.
        if "html" not in response.headers.get("content-type", "").lower():
            raise ValueError("Unexpected bus timetable response")
        temporary = path.with_suffix(".part")
        temporary.write_bytes(gzip.compress(response.content))
        temporary.replace(path)
        return response.content

    try:
        home = BeautifulSoup(fetch(ROUTES_URL), "html.parser")
        urls = sorted(
            {
                urljoin(HOST, a["href"]).replace("http:", "https:")
                for a in home.select("a[href]")
                if re.fullmatch(r"(?:https?://seoulairbus\.co\.kr)?/bus/\d+", a["href"])
            }
        )
        if not urls:
            raise ValueError("Published bus route links missing")
        airport = []
        for url in urls:
            page = BeautifulSoup(fetch(url), "html.parser")
            number = url.rsplit("/", 1)[1]
            stations = []
            for a in page.select('a[href^="/timetable/"]'):
                if a.find("em") and a.find("strong"):
                    stations.append(
                        dict(
                            ars=a.find("em").get_text(strip=True),
                            name=a.find("strong").get_text(" ", strip=True),
                        )
                    )
            sources = {}
            for a in page.select('[onclick*="showTimeTableAll"]'):
                match = re.search(
                    r"showTimeTableAll\(event,\s*'?(\d+)'?,\s*'?(\d+)'?", a["onclick"]
                )
                if match:
                    sources[match[2]] = (
                        HOST + "/timetableAll/" + match[1] + "/" + match[2],
                        a.get_text(" ", strip=True),
                    )
            if not stations or "0" not in sources:
                raise ValueError("Published full bus timetable missing")
            calendars = {}
            for kind, (source_url, label) in sources.items():
                profiles = (
                    ["weekday"]
                    if len(sources) > 1 and kind == "0"
                    else ["holiday"]
                    if kind == "1"
                    else ["weekday", "saturday", "holiday"]
                )
                calendars[kind] = dict(
                    profiles=profiles,
                    source_url=source_url,
                    label=label,
                    **matrix(fetch(source_url), len(stations)),
                )
            airport.append(
                dict(
                    route_number=number,
                    stations=stations,
                    calendars=calendars,
                    source_url=url,
                )
            )
        city = city_tables(fetch(CITY_URL))
        result = dict(
            schema=1,
            collected_at=datetime.now(timezone.utc).isoformat(),
            valid_from=datetime.now().astimezone().date().isoformat(),
            airport=airport,
            city=city,
            notes=[
                "Airport city-to-city travel is not permitted by this adapter.",
                "Separate weekday/holiday tables do not establish a Saturday calendar.",
            ],
        )
        # HTML parsers may return tuples; compare the stored JSON representation.
        result = json.loads(json.dumps(result, ensure_ascii=False))
        unchanged = False
        if output.exists():
            previous = json.loads(output.read_text(encoding="utf8"))
            unchanged = all(
                previous.get(k) == result[k] for k in ("airport", "city", "notes")
            )
        if not unchanged:
            atomic_json(output, result)
        return dict(
            airport_routes=len(airport),
            city_routes=len(city),
            new_requests=calls,
            output=str(output),
            unchanged=unchanged,
        )
    finally:
        session.close()


def mapped_city_dispatches(folder, payload, routes, sequences):
    """Only apply a posted departure table when all ordered ARS IDs agree."""

    folder = Path(folder)
    path = folder / "bus_web_timetables.json"
    if not path.exists():
        return {}, {}
    data = json.loads(path.read_text(encoding="utf8"))
    ars = station_ars(folder)
    matched, report = {}, {}
    for record in data["city"]:
        candidates = [r for r in routes if r["노선명"] == record["route_number"]]
        if len(candidates) != 1:
            report[record["route_number"]] = "ambiguous_route_id"
            continue
        route = candidates[0]
        sequence = sorted(
            sequences[route["노선ID"]], key=lambda r: int(r["정류장순번"])
        )
        actual = [ars.get(r["노드ID"]) for r in sequence]
        expected = [r[2] for r in record["stops"]]
        if actual != expected:
            report[record["route_number"]] = "ordered_stops_changed_not_applied"
            continue
        matched[route["노선ID"]] = dict(
            weekday=[r[0] for r in record["departures"]],
            runtime_seconds=record["departures"][0][1] - record["departures"][0][0],
            valid_from=data["valid_from"],
            source_url=record["source_url"],
        )
        report[record["route_number"]] = "published_origin_departures"
    return matched, report


def station_ars(folder):
    path = Path(folder) / "files/bus_coordinates.xlsx"
    return _station_ars(str(path.resolve()), path.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _station_ars(path, signature):
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = workbook.active.iter_rows(values_only=True)
        headers = next(rows)
        result = {}
        for values in rows:
            row = dict(zip(headers, values))
            if row.get("NODE_ID") is not None and row.get("ARS_ID") is not None:
                result[str(row["NODE_ID"])] = str(row["ARS_ID"]).zfill(5)
        return result
    finally:
        workbook.close()


def published_airport_network(
    folder, payload, routes, sequences, departure, horizon, holiday_dates=None
):
    from multimodal import Network
    from transit_schedule import service_days, day_type
    from datetime import timedelta
    from multimodal_data import KST

    folder = Path(folder)
    path = folder / "bus_web_timetables.json"
    if not path.exists():
        return Network(nodes={}), {}
    data, codes = json.loads(path.read_text(encoding="utf8")), station_ars(folder)
    start, finish, days = service_days(departure, horizon)
    all_stops = payload["bus_stops"]
    edges, nodes, report = [], {}, {}
    for posted in data["airport"]:
        candidates = [r for r in routes if r["노선명"] == posted["route_number"]]
        if len(candidates) != 1:
            report[posted["route_number"]] = "ambiguous_route_id"
            continue
        route = candidates[0]
        ids = {r["노드ID"] for r in sequences[route["노선ID"]]}
        by_ars = defaultdict(set)
        for sid in ids:
            if sid in all_stops and sid in codes:
                by_ars[codes[sid]].add(sid)
        selected = []
        for station in posted["stations"]:
            found = by_ars[station["ars"]]
            selected.append(next(iter(found)) if len(found) == 1 else None)
        # These IDs are selected only when present on this operator's route,
        # with explicitly matching terminal/floor names in the stop master.
        for terminal in ("T1-3층", "T2-3층"):
            found = {
                sid
                for sid in ids
                if sid in all_stops and terminal in all_stops[sid].get("name", "")
            }
            selected.append(next(iter(found)) if len(found) == 1 else None)
        count = 0
        for day in days:
            if day.isoformat() < data["valid_from"]:
                continue
            profile = day_type(day, holiday_dates)
            midnight = datetime.combine(day, datetime.min.time(), KST)
            for calendar in posted["calendars"].values():
                if profile not in calendar["profiles"]:
                    continue
                for trip in calendar["trips"]:
                    points = [
                        (i, sid, when)
                        for i, (sid, when) in enumerate(zip(selected, trip["outbound"]))
                        if sid and when is not None
                    ]
                    for a, b in zip(points, points[1:]):
                        dep, arr = (
                            midnight + timedelta(seconds=a[2]),
                            midnight + timedelta(seconds=b[2]),
                        )
                        if arr <= dep or dep < start or arr > finish:
                            continue
                        edge = dict(
                            **{"from": "bus:" + a[1], "to": "bus:" + b[1]},
                            mode="bus",
                            departure=dep.isoformat(),
                            arrival=arr.isoformat(),
                            trip_id=f"publishedbus:{day}:{route['노선ID']}:{trip['number']}",
                            route_id=route["노선ID"],
                            route_name=route["노선명"],
                            boarding_seconds=15,
                            pickup_allowed=a[0] < len(posted["stations"]),
                            dropoff_allowed=b[0] >= len(posted["stations"]),
                            timetable_method="published_operator_stop_matrix",
                            source_url=calendar["source_url"],
                        )
                        edges.append(edge)
                        for sid in (a[1], b[1]):
                            nodes["bus:" + sid] = dict(
                                all_stops[sid], kind="stop", transit_mode="bus"
                            )
                        count += 1
        report[posted["route_number"]] = dict(
            edges=count,
            unmapped_city_stops=sum(s is None for s in selected[:-2]),
            mapped_airport_arrivals=sum(s is not None for s in selected[-2:]),
        )
    return Network(nodes=nodes, transit=edges), report
