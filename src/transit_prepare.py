"""Resumable Seoul Open Data collection. Called by the notebook, never on import."""

from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
import hashlib
import json
import math
import re
import time
from urllib.parse import quote

import requests
from multimodal_data import KST
from transit_schedule import clean, day_type, service_days


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    temp.replace(path)


class SeoulStaticClient:
    """Page cache + explicit per-run request budget, no retries or key in logs."""

    def __init__(self, key, cache, max_calls=2000, session=None, progress=print):
        if not clean(key):
            raise ValueError("SEOUL_API_KEY를 입력하세요.")
        if max_calls <= 0:
            raise ValueError("서울시 API 호출 한도는 양수여야 합니다.")
        self.key, self.cache, self.max_calls = clean(key), Path(cache), max_calls
        self.cache.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.progress, self.calls, self.hits = progress, 0, 0
        self.last_request = 0.0

    def close(self):
        self.session.close()

    def rows(self, service, *args):
        result, first = [], 1
        while True:
            identity = json.dumps([service, args, first], ensure_ascii=False)
            path = self.cache / (
                hashlib.sha256(identity.encode()).hexdigest() + ".json"
            )
            if path.is_file():
                body = json.loads(path.read_text(encoding="utf-8"))
                self.hits += 1
            else:
                if self.calls >= self.max_calls:
                    raise RuntimeError(
                        "이번 실행의 서울시 API 호출 한도에 도달했습니다. 완료된 페이지는 저장됐습니다. 같은 셀을 재실행하면 이어집니다."
                    )
                time.sleep(max(0, 0.25 - (time.monotonic() - self.last_request)))
                self.calls += 1
                self.last_request = time.monotonic()
                # The official Seoul Open Data API documents the HTTP :8088 endpoint.
                parts = [
                    self.key,
                    "json",
                    service,
                    str(first),
                    str(first + 999),
                    *map(str, args),
                ]
                url = (
                    "http://openapi.seoul.go.kr:8088/"
                    + "/".join(quote(p, safe="") for p in parts)
                    + "/"
                )
                try:
                    response = self.session.get(url, timeout=40)
                except requests.RequestException:
                    raise RuntimeError(
                        f"{service}: 연결 실패. 재실행 시 성공한 페이지를 재사용합니다."
                    ) from None
                if response.status_code != 200:
                    raise RuntimeError(
                        f"{service}: HTTP {response.status_code}. 인증/한도를 확인하세요."
                    )
                try:
                    payload = response.json()
                except ValueError:
                    raise RuntimeError(
                        f"{service}: JSON 응답이 아닙니다. 재실행하세요."
                    ) from None
                body = payload.get(service, payload)
                code = body.get("RESULT", {}).get("CODE")
                if code == "INFO-200":
                    body = {"row": [], "list_total_count": 0}
                elif code not in (None, "INFO-000"):
                    # Do not echo MESSAGE or request URL (could include the key).
                    raise RuntimeError(
                        f"{service}: {code}. 서울시 API 인증·서비스 권한을 확인하세요."
                    )
                if "row" not in body:
                    raise RuntimeError(
                        f"{service}: row가 없는 응답입니다. 캐시에 저장하지 않았습니다."
                    )
                save_json(path, body)
            batch = body.get("row", [])
            if not isinstance(batch, list):
                raise ValueError(service + ": row 형식이 변경되었습니다.")
            result.extend({clean(k): clean(v) for k, v in row.items()} for row in batch)
            total = int(body.get("list_total_count", len(result)))
            if len(result) >= total:
                return result
            if not batch:
                raise RuntimeError(
                    service + ": 페이지 일부가 비었습니다. 수집을 다시 실행하세요."
                )
            first += 1000


def pick(row, *keys):
    for key in keys:
        if clean(row.get(key)):
            return clean(row[key])
    raise ValueError(
        "필요한 열이 없습니다: " + ", ".join(keys) + " / 실제 열: " + ", ".join(row)
    )


def coordinate(row):
    x = float(
        pick(
            row,
            "LOT",
            "CRDNT_X",
            "X_CRDNT",
            "STTN_CRDNT_X",
            "X",
            "X좌표",
            "경도",
            "longitude",
        )
    )
    y = float(
        pick(
            row,
            "LAT",
            "CRDNT_Y",
            "Y_CRDNT",
            "STTN_CRDNT_Y",
            "Y",
            "Y좌표",
            "위도",
            "latitude",
        )
    )
    # Some Seoul services call latitude X; validate the numerical ranges.
    if 33 <= x <= 39 and 124 <= y <= 132:
        x, y = y, x
    if not (math.isfinite(x + y) and 124 <= x <= 132 and 33 <= y <= 39):
        raise ValueError("WGS84 좌표가 아닙니다.")
    return {"latitude": y, "longitude": x}


def normalized_name(value):
    return re.sub(r"\([^)]*\)|[\s·]", "", clean(value))


def normalized_line(value):
    match = re.fullmatch(r"0?([1-9])호선", clean(value))
    return match.group(1) if match else clean(value)


def prepare_transit(
    folder,
    departure,
    horizon_minutes=180,
    api_key=None,
    max_calls=2000,
    progress=print,
    holiday_dates=None,
):
    """Prepare all stops for required day types, with daily resumable raw pages.

    Subway station codes and coordinate master IDs differ; match by both line and
    station name, only when unambiguous. Persist unmatched station coverage.
    """
    import os

    folder = Path(folder)
    for name in ("bus_raw.zip", "bus_route_nodes.dat"):
        if not (folder / name).is_file():
            raise FileNotFoundError(
                "서울시 버스 원자료가 필요합니다: " + str(folder / name)
            )
    start, finish, days = service_days(departure, horizon_minutes)
    required = sorted({day_type(day, holiday_dates) for day in days})
    now = datetime.now(KST)
    if start.date() < now.date() or finish.date() > now.date() + timedelta(days=14):
        raise ValueError("현재 시간표는 오늘부터 14일 이내 출발에 사용하세요.")
    path = folder / "prepared.json"
    fingerprints = {
        name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
        for name in ("bus_raw.zip", "bus_route_nodes.dat")
    }
    if path.is_file():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if (
            cached.get("schema") == 1
            and cached["valid_from"] <= start.date().isoformat()
            and finish.date().isoformat() <= cached["valid_until"]
            and set(required) <= set(cached["day_types"])
            and cached.get("bus_fingerprints") == fingerprints
        ):
            if progress:
                progress("저장된 지하철 시간표·정류장 좌표 재사용", flush=True)
            return cached["report"]
    client = SeoulStaticClient(
        api_key or os.getenv("SEOUL_API_KEY"),
        folder / "raw_cache" / now.date().isoformat(),
        max_calls,
        progress=progress,
    )
    try:
        bus_rows = client.rows("tbisMasterStation")
        bus_stops, invalid_bus = {}, 0
        for row in bus_rows:
            try:
                sid = pick(
                    row,
                    "CRTR_ID",
                    "STTN_ID",
                    "STATION_ID",
                    "NODE_ID",
                    "정류장_ID",
                    "정류장ID",
                )
                if len(sid) != 9 or not sid.isdigit():
                    raise ValueError()
                bus_stops[sid] = dict(
                    coordinate(row),
                    name=pick(row, "CRTR_NM", "STTN_NM", "STATION_NM", "정류장명"),
                )
            except ValueError:
                invalid_bus += 1
        if not bus_stops:
            raise ValueError(
                "버스 정류장 좌표를 해석하지 못했습니다. 실제 열: "
                + ", ".join(bus_rows[0] if bus_rows else [])
            )
        stations = client.rows("SearchInfoBySubwayNameService")
        master = client.rows("subwayStationMaster")
        coords = defaultdict(list)
        for row in master:
            try:
                key = (
                    normalized_line(pick(row, "ROUTE", "LINE_NUM")),
                    normalized_name(pick(row, "BLDN_NM", "STATN_NM", "STATION_NM")),
                )
                coords[key].append(coordinate(row))
            except ValueError:
                continue
        subway_stops, unmatched = {}, []
        for row in stations:
            code = row["STATION_CD"]
            key = (normalized_line(row["LINE_NUM"]), normalized_name(row["STATION_NM"]))
            options = coords.get(key, [])
            unique = {(v["latitude"], v["longitude"]) for v in options}
            if len(unique) != 1:
                unmatched.append(
                    {
                        "station_code": code,
                        "name": row["STATION_NM"],
                        "line": row["LINE_NUM"],
                    }
                )
                continue
            lat, lon = next(iter(unique))
            subway_stops[code] = {
                "name": row["STATION_NM"],
                "latitude": lat,
                "longitude": lon,
            }
        if not subway_stops:
            raise ValueError(
                "지하철 역코드와 좌표를 연결하지 못했습니다. 원자료 열을 확인하세요."
            )
        # Collect even unmatched-coordinate stations so train joins cannot bridge
        # over an unlocated intermediate stop.
        rows, count = [], 0
        tags = {"weekday": "1", "saturday": "2", "holiday": "3"}
        for row in stations:
            if normalized_line(row["LINE_NUM"]) not in set("123456789"):
                continue  # official timetable's documented service coverage
            for kind in required:
                for direction in ("1", "2"):
                    batch = client.rows(
                        "SearchSTNTimeTableByIDService",
                        row["STATION_CD"],
                        tags[kind],
                        direction,
                    )
                    for item in batch:
                        item.update(
                            STATION_CD=row["STATION_CD"],
                            WEEK_TAG=tags[kind],
                            INOUT_TAG=direction,
                        )
                    rows.extend(batch)
            count += 1
            if progress and (count % 10 == 0 or count == len(stations)):
                progress(
                    f"지하철 {count}/{len(stations)}역 / 새 호출 {client.calls} / 재사용 {client.hits}",
                    flush=True,
                )
        if not rows:
            raise ValueError(
                "지하철 시간표가 비었습니다. API 서비스 권한을 확인하세요."
            )
        report = {
            "bus_stops": len(bus_stops),
            "bus_coordinates_rejected": invalid_bus,
            "subway_stops": len(subway_stops),
            "subway_timetable_rows": len(rows),
            "subway_unmatched_stations": unmatched,
            "new_api_calls": client.calls,
            "cache_hits": client.hits,
            "day_types": required,
            "sources": ["OA-21231", "OA-21232", "OA-121", "OA-101"],
            "bus_source_note": "local TOPIS files; file download date is not schedule effective date",
        }
        save_json(
            path,
            {
                "schema": 1,
                "created_at": now.isoformat(),
                "valid_from": now.date().isoformat(),
                "valid_until": (now.date() + timedelta(days=14)).isoformat(),
                "day_types": required,
                "distance_mode": "segment",
                "report": report,
                "bus_fingerprints": fingerprints,
                "bus_stops": bus_stops,
                "subway_stops": subway_stops,
                "subway_rows": rows,
            },
        )
        save_json(folder / "preparation_report.json", report)
        if progress:
            progress(
                f"시간표 준비 완료: 버스 정류장 {len(bus_stops):,}, 지하철 원자료 {len(rows):,}행",
                flush=True,
            )
        return report
    finally:
        client.close()
