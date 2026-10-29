"""Kakao official routing REST API. Keys never enter checkpoints or results."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from weakref import WeakValueDictionary
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

_request_locks = WeakValueDictionary()
_request_guard = threading.Lock()


def _request_lock(key):
    with _request_guard:
        lock = _request_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _request_locks[key] = lock
        return lock


class KakaoError(RuntimeError):
    pass


class CallLimit(KakaoError):
    pass


class WalkingDeadline(CallLimit):
    pass


def record_walking_error(metadata, error, start, end):
    """Keep diagnostics without leaking request URLs, credentials or payloads."""
    metadata["walking_api_errors"] = metadata.get("walking_api_errors", 0) + 1
    examples = metadata.setdefault("walking_error_examples", [])
    if len(examples) < 10:
        examples.append({"from": start, "to": end, "type": type(error).__name__})


def point(value):
    lat, lon = float(value["latitude"]), float(value["longitude"])
    if not math.isfinite(lat + lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("잘못된 위도·경도입니다.")
    return dict(latitude=lat, longitude=lon, name=str(value.get("name", "")))


def distance(a, b):
    lat1, lat2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    dlat = lat2 - lat1
    dlon = math.radians(b["longitude"] - a["longitude"])
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 12742000 * math.asin(min(1, math.sqrt(max(0, h))))


class KakaoClient:
    """A request attempt consumes both run and local daily budgets, even on failure.

    Local daily accounting is not the app's official quota (other machines and
    programs are not counted). No automatic retries or paid-quota changes.
    """

    def __init__(
        self,
        cache_path,
        api_key=None,
        max_calls=40,
        daily_limit=1000,
        interval_seconds=0.35,
        timeout_seconds=15,
        progress=None,
    ):
        self._key = (api_key or os.getenv("KAKAO_REST_API_KEY", "")).strip()
        if not self._key:
            raise ValueError(
                "KAKAO_REST_API_KEY를 설정하거나 노트북의 비공개 입력란에 넣으세요."
            )
        if not 1 <= max_calls <= 500 or not 1 <= daily_limit <= 10000:
            raise ValueError("API 호출 한도가 범위를 벗어났습니다.")
        if interval_seconds < 0 or timeout_seconds <= 0:
            raise ValueError("잘못된 API 대기시간 설정입니다.")
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(cache_path), timeout=30)
        self._cache_namespace = os.path.normcase(str(Path(cache_path).resolve()))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS kakao_cache (key TEXT PRIMARY KEY, at REAL, ttl REAL, payload TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS kakao_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL)"
        )
        self.session = requests.Session()
        self.max_calls, self.daily_limit = max_calls, daily_limit
        self.interval, self.timeout, self.progress = (
            interval_seconds,
            timeout_seconds,
            progress,
        )
        self.calls = self.hits = 0
        self.last_call = 0.0
        self.stopped = None
        self.deadline = None
        self._transport = None
        self._inflight = None

    def stats(self):
        day = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        row = self.db.execute(
            "SELECT calls FROM kakao_usage WHERE day=?", (day,)
        ).fetchone()
        return dict(
            new_api_calls=self.calls,
            cache_hits=self.hits,
            local_daily_calls=row[0] if row else 0,
            max_calls=self.max_calls,
            local_daily_limit=self.daily_limit,
            stopped=self.stopped,
        )

    def _reserve(self):
        if self.stopped:
            raise CallLimit(self.stopped)
        if self.calls >= self.max_calls:
            raise CallLimit(
                "이번 실행의 API 호출 한도에 도달했습니다. 저장된 결과는 유지됩니다."
            )
        day = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT calls FROM kakao_usage WHERE day=?", (day,)
            ).fetchone()
            count = row[0] if row else 0
            if count >= self.daily_limit:
                self.stopped = "이 캐시 파일 기준 하루 호출 한도에 도달했습니다."
                raise CallLimit(self.stopped)
            self.db.execute(
                "INSERT OR REPLACE INTO kakao_usage VALUES (?,?)", (day, count + 1)
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.calls += 1

    def get(self, kind, origin, destination):
        cached = self.get_cached(kind, origin, destination)
        if cached is not None:
            return cached
        a, b = point(origin), point(destination)
        key = (
            self._cache_namespace,
            kind,
            a["latitude"],
            a["longitude"],
            b["latitude"],
            b["longitude"],
        )
        lock = _request_lock(key)
        if self.deadline is None:
            acquired = lock.acquire()
        else:
            acquired = lock.acquire(timeout=max(0, self.deadline - time.monotonic()))
        if not acquired:
            raise WalkingDeadline("도보 조회의 탐색시간이 끝났습니다.")
        try:
            # Another search may have filled this exact cache entry while waiting.
            return self._get(kind, origin, destination)
        finally:
            lock.release()

    def _get(self, kind, origin, destination):
        if kind not in ("walk", "publictraffic"):
            raise ValueError("지원하지 않는 카카오 API 종류입니다.")
        a, b = point(origin), point(destination)
        params = dict(
            start_x=a["longitude"],
            start_y=a["latitude"],
            end_x=b["longitude"],
            end_y=b["latitude"],
            input_coord="WGS84",
            output_coord="WGS84",
        )
        key = hashlib.sha256(
            json.dumps([kind, params], sort_keys=True).encode()
        ).hexdigest()
        cached = self.db.execute(
            "SELECT at,ttl,payload FROM kakao_cache WHERE key=?", (key,)
        ).fetchone()
        if cached and time.time() - cached[0] <= cached[1]:
            self.hits += 1
            return json.loads(cached[2]), datetime.fromtimestamp(
                cached[0], timezone.utc
            ).isoformat()
        if self.stopped:
            raise CallLimit(self.stopped)
        if self.calls >= self.max_calls:
            raise CallLimit(
                "이번 실행의 API 호출 한도에 도달했습니다. 저장된 결과는 유지됩니다."
            )
        delay = max(0, self.interval - (time.monotonic() - self.last_call))
        if self.deadline is not None and self.deadline - time.monotonic() <= delay:
            raise WalkingDeadline(
                "남은 탐색시간 안에 새 도보 조회를 시작할 수 없습니다."
            )
        time.sleep(delay)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise WalkingDeadline("도보 조회의 탐색시간이 끝났습니다.")
        self._reserve()
        self.last_call = time.monotonic()
        if self.progress:
            self.progress(
                f"카카오 {kind}: 새 호출 {self.calls}/{self.max_calls}", flush=True
            )
        try:
            options = dict(
                params=params, headers={"Authorization": "KakaoAK " + self._key}
            )
            url = "https://dapi.kakao.com/v2/routing/" + kind
            if self.deadline is None:
                response = self.session.get(url, timeout=self.timeout, **options)
            else:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise WalkingDeadline("도보 조회의 탐색시간이 끝났습니다.")
                if self._transport is None:
                    self._transport = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="kakao-http"
                    )
                self._inflight = self._transport.submit(
                    self.session.get,
                    url,
                    timeout=min(self.timeout, remaining),
                    **options,
                )
                try:
                    response = self._inflight.result(timeout=remaining)
                except FutureTimeout:
                    self._inflight.cancel()
                    raise WalkingDeadline(
                        "도보 조회 대기를 종료하고 확보한 경로를 반환합니다."
                    ) from None
        except requests.RequestException:
            raise KakaoError(
                "카카오 연결 실패 또는 응답시간 초과. 재실행하면 성공한 조회는 재사용합니다."
            ) from None
        if response.status_code in (401, 403, 429):
            self.stopped = {
                401: "HTTP 401: REST API 키를 확인하세요.",
                403: "HTTP 403: 카카오맵 활성화, REST API 키 상태, 호출 허용 IP 및 앱 사용 권한을 확인하세요.",
                429: "HTTP 429: 카카오 호출 제한에 도달했습니다. 추가 요청을 중단했습니다.",
            }[response.status_code]
            raise KakaoError(self.stopped)
        if response.status_code != 200:
            raise KakaoError(
                f"카카오 HTTP {response.status_code}: 해당 조회를 저장하지 않았습니다."
            )
        try:
            payload = response.json()
        except ValueError:
            raise KakaoError("카카오 응답이 JSON 형식이 아닙니다.") from None
        if not isinstance(payload, dict):
            raise KakaoError("카카오 응답 구조가 올바르지 않습니다.")
        status = payload.get("status")
        no_route = (
            "NO_RESULTS",
            "STARTNODES_NULL",
            "ENDNODES_NULL",
            "EQUAL_POINTS",
            "SAME_POINT",
            "START_LINK_NOT_FOUND",
            "END_LINK_NOT_FOUND",
            "TOO_FAR_AWAY",
            "ROUTE_RESULT_NOT_FOUND",
        )
        if status != "OK" and status not in no_route:
            raise KakaoError("카카오 경로 조회 실패: " + str(status)[:80])
        now = time.time()
        ttl = (300 if kind == "publictraffic" else 86400 * 7) if status == "OK" else 60
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO kakao_cache VALUES (?,?,?,?)",
                (
                    key,
                    now,
                    ttl,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                ),
            )
        return payload, datetime.fromtimestamp(now, timezone.utc).isoformat()

    def get_cached(self, kind, origin, destination):
        """Read a valid exact-coordinate cache entry; never reserve/call HTTP."""
        if kind not in ("walk", "publictraffic"):
            raise ValueError("지원하지 않는 카카오 API 종류입니다.")
        a, b = point(origin), point(destination)
        params = dict(
            start_x=a["longitude"],
            start_y=a["latitude"],
            end_x=b["longitude"],
            end_y=b["latitude"],
            input_coord="WGS84",
            output_coord="WGS84",
        )
        key = hashlib.sha256(
            json.dumps([kind, params], sort_keys=True).encode()
        ).hexdigest()
        row = self.db.execute(
            "SELECT at,ttl,payload FROM kakao_cache WHERE key=?", (key,)
        ).fetchone()
        if row and time.time() - row[0] <= row[1]:
            self.hits += 1
            return json.loads(row[2]), datetime.fromtimestamp(
                row[0], timezone.utc
            ).isoformat()
        return None

    def close(self):
        if self._inflight is not None and not self._inflight.done():
            # A late transport response cannot touch SQLite or change results.
            # Close its connection after it finishes, without blocking search.
            self._inflight.add_done_callback(lambda _: self.session.close())
        else:
            self.session.close()
        if self._transport is not None:
            self._transport.shutdown(wait=False, cancel_futures=True)
        self.db.close()


def _minutes(seconds):
    result = float(seconds) / 60
    if not math.isfinite(result) or result <= 0:
        raise KakaoError("카카오 경로의 이동시간이 양수가 아닙니다.")
    return result


def _step(step):
    props = step["properties"]
    mode = {"WALKING": "walk", "BUS": "bus", "SUBWAY": "subway"}.get(props.get("type"))
    if mode is None:
        raise KakaoError("지원하지 않는 대중교통 구간 종류입니다.")
    seconds = float(props.get("time", 0))
    if not math.isfinite(seconds) or seconds < 0:
        raise KakaoError("잘못된 대중교통 구간 시간입니다.")
    return dict(
        mode=mode,
        minutes=seconds / 60,
        distance_m=props.get("distance"),
        guidance=props.get("guidance", ""),
        vehicles=props.get("vehicles", []),
        stops=props.get("stops", []),
        geometry=dict(
            type="LineString", coordinates=step.get("path", {}).get("points", [])
        ),
    )


def walking_edge(payload, fetched_at, a, b):
    try:
        return _walking_edge(payload, fetched_at, a, b)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise KakaoError("카카오 도보 응답의 경로 형식이 올바르지 않습니다.") from None


def _walking_edge(payload, fetched_at, a, b):
    if payload.get("status") != "OK":
        return None
    route = payload["route"]
    points = [
        xy
        for leg in route.get("legs", [])
        for step in leg.get("steps", [])
        for xy in step.get("path", {}).get("points", [])
    ]
    return dict(
        mode="walk",
        **{"from": a, "to": b},
        minutes=_minutes(route["properties"]["totalTime"]),
        distance_m=route["properties"].get("totalDistance"),
        source="kakao_walk",
        fetched_at=fetched_at,
        geometry=dict(type="LineString", coordinates=points),
    )


def transit_edges(payload, fetched_at, a, b, alternatives=3):
    if payload.get("status") != "OK":
        return []
    result = []
    for i, route in enumerate(payload.get("routes", [])):
        steps = [_step(s) for s in route.get("steps", [])]
        if not any(s["mode"] in ("bus", "subway") for s in steps):
            continue
        props = route["properties"]
        total = _minutes(props["totalTime"])
        step_total = sum(s["minutes"] for s in steps)
        # Total is authoritative. Never treat its unexplained remainder as a
        # measured waiting time or count access walking a second time.
        result.append(
            dict(
                mode="transit",
                **{"from": a, "to": b},
                minutes=total,
                distance_m=props.get("totalDistance"),
                steps=steps,
                source="kakao_publictraffic",
                fetched_at=fetched_at,
                route_option=i,
                fare=props.get("fare"),
                boardings=sum(s["mode"] in ("bus", "subway") for s in steps),
                step_minutes_sum=step_total,
                unexplained_minutes=total - step_total,
                departure_time_supported=False,
            )
        )
    return sorted(result, key=lambda e: e["minutes"])[:alternatives]
