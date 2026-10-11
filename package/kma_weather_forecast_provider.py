"""KMA village-forecast adapter for the bicycle ETA service.

Set ``KMA_API_KEY`` to the authentication key issued by the KMA API Hub.
The public API is free, but an approved key and network connection are required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pandas as pd
import requests


KST = ZoneInfo("Asia/Seoul")
ISSUE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
MODEL_FIELDS = {
    "TMP": "temperature_c",
    "REH": "humidity_pct",
    "PCP": "rainfall_mm",
    "SNO": "snowfall_cm",
    "WSD": "wind_speed_ms",
    "VEC": "wind_direction_deg",
}


def _local_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp
    return timestamp.tz_convert("Asia/Seoul").tz_localize(None)


def latitude_longitude_to_kma_grid(latitude: float, longitude: float) -> tuple[int, int]:
    """Convert WGS84 coordinates to the KMA 5 km Lambert grid."""
    re = 6371.00877 / 5.0
    slat1, slat2, olon, olat = map(math.radians, (30.0, 60.0, 126.0, 38.0))
    xo, yo = 43.0, 136.0
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5)
        / math.tan(math.pi * 0.25 + slat1 * 0.5)
    )
    sf = (
        math.tan(math.pi * 0.25 + slat1 * 0.5) ** sn
        * math.cos(slat1)
        / sn
    )
    ro = re * sf / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    ra = re * sf / math.tan(math.pi * 0.25 + math.radians(latitude) * 0.5) ** sn
    theta = math.radians(longitude) - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    nx = int(ra * math.sin(theta) + xo + 0.5)
    ny = int(ro - ra * math.cos(theta) + yo + 0.5)
    return nx, ny


def _amount(value, kind: str) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(" ", "")
    if not text or text in {"강수없음", "적설없음", "없음", "-"}:
        return 0.0
    unit = "mm" if kind == "rain" else "cm"
    text = text.replace(unit, "")
    if "미만" in text:
        try:
            return float(text.split("미만")[0]) / 2.0
        except ValueError:
            return None
    if "이상" in text:
        try:
            return float(text.split("이상")[0])
        except ValueError:
            return None
    if "~" in text:
        try:
            low, high = text.split("~", 1)
            return (float(low) + float(high)) / 2.0
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


class KMAWeatherForecastProvider:
    """Retrieve KMA forecasts by station coordinate with grid/run-level caching."""

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | None = None,
        cache_ttl_seconds: int = 6 * 60 * 60,
        timeout_seconds: int = 30,
        publication_delay_minutes: int = 20,
        session: requests.Session | None = None,
    ):
        raw_key = api_key or os.getenv("KMA_API_KEY") or os.getenv("KMA_AUTH_KEY")
        if not raw_key:
            raise ValueError(
                "KMA_API_KEY가 없습니다. 기상청 API허브에서 키를 발급받아 환경변수로 설정하세요."
            )
        # 발급 화면의 인코딩 키와 디코딩 키를 모두 받을 수 있게 한 번 원복합니다.
        self.api_key = unquote(raw_key.strip())
        self.endpoint = (
            "https://apihub.kma.go.kr/api/typ02/openApi/"
            "VilageFcstInfoService_2.0/getVilageFcst"
        )
        self.cache_dir = Path(
            cache_dir or os.getenv("BICYCLE_KMA_CACHE", "cache/kma_forecast")
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.timeout_seconds = int(timeout_seconds)
        self.publication_delay_minutes = int(publication_delay_minutes)
        self.session = session or requests.Session()

    def _latest_issue(self, now: datetime | None = None) -> datetime:
        reference = now or datetime.now(KST)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=KST)
        available_at = reference.astimezone(KST) - timedelta(
            minutes=self.publication_delay_minutes
        )
        candidates = [
            available_at.replace(hour=hour, minute=0, second=0, microsecond=0)
            for hour in ISSUE_HOURS
            if hour <= available_at.hour
        ]
        if candidates:
            return candidates[-1]
        previous = available_at - timedelta(days=1)
        return previous.replace(hour=23, minute=0, second=0, microsecond=0)

    def _cache_path(self, nx: int, ny: int, issue: datetime) -> Path:
        signature = hashlib.sha256(
            f"{nx}:{ny}:{issue:%Y%m%d%H%M}".encode()
        ).hexdigest()[:24]
        return self.cache_dir / f"{signature}.json"

    @staticmethod
    def _extract_items(payload: Mapping) -> list[dict]:
        response = payload.get("response", payload)
        header = response.get("header", {})
        code = str(header.get("resultCode", "00"))
        if code not in {"00", "0"}:
            raise RuntimeError(
                f"기상청 API 오류 {code}: {header.get('resultMsg', 'unknown error')}"
            )
        body = response.get("body", {})
        items = body.get("items", {}) if isinstance(body, Mapping) else {}
        rows = items.get("item", []) if isinstance(items, Mapping) else []
        if isinstance(rows, Mapping):
            rows = [rows]
        if not rows:
            raise ValueError("기상청 단기예보 응답에 예보 항목이 없습니다.")
        return list(rows)

    def _download_grid(self, nx: int, ny: int, issue: datetime) -> list[dict]:
        cache_path = self._cache_path(nx, ny, issue)
        if (
            cache_path.exists()
            and time.time() - cache_path.stat().st_mtime <= self.cache_ttl_seconds
        ):
            return json.loads(cache_path.read_text(encoding="utf-8"))["items"]
        params = {
            "pageNo": 1,
            "numOfRows": 2000,
            "dataType": "JSON",
            "base_date": issue.strftime("%Y%m%d"),
            "base_time": issue.strftime("%H00"),
            "nx": nx,
            "ny": ny,
            "authKey": self.api_key,
        }
        response = self.session.get(self.endpoint, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            sample = response.text[:300].replace("\n", " ")
            raise RuntimeError(f"기상청 API가 JSON 대신 다른 응답을 반환했습니다: {sample}") from exc
        rows = self._extract_items(payload)
        saved = {
            "issue": issue.isoformat(),
            "nx": nx,
            "ny": ny,
            "items": rows,
        }
        temporary = cache_path.with_suffix(".tmp.json")
        temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, cache_path)
        return rows

    @staticmethod
    def _at_departure(
        rows: list[dict], departure, nx: int, ny: int, issue: datetime
    ) -> dict:
        target = _local_timestamp(departure)
        table = pd.DataFrame(rows)
        required = {"fcstDate", "fcstTime", "category", "fcstValue"}
        if not required.issubset(table.columns):
            raise ValueError(f"기상청 응답 필드가 부족합니다: {required-set(table.columns)}")
        table["forecast_at"] = pd.to_datetime(
            table.fcstDate.astype(str) + table.fcstTime.astype(str).str.zfill(4),
            format="%Y%m%d%H%M",
            errors="coerce",
        )
        candidates = table.loc[table.category.eq("TMP"), "forecast_at"].dropna().unique()
        if not len(candidates):
            raise ValueError("기상청 응답에 시간별 기온 예보가 없습니다.")
        forecast_times = pd.DatetimeIndex(candidates)
        offsets = abs(forecast_times - target)
        selected = forecast_times[int(offsets.argmin())]
        if abs(selected - target) > pd.Timedelta("181min"):
            first, last = forecast_times.min(), forecast_times.max()
            raise ValueError(
                f"요청 시각이 단기예보 범위 밖입니다: {target}; 제공 범위 {first}~{last}"
            )
        values = (
            table.loc[table.forecast_at.eq(selected), ["category", "fcstValue"]]
            .drop_duplicates("category", keep="last")
            .set_index("category").fcstValue.to_dict()
        )
        result = {
            "temperature_c": pd.to_numeric(values.get("TMP"), errors="coerce"),
            "humidity_pct": pd.to_numeric(values.get("REH"), errors="coerce"),
            "rainfall_mm": _amount(values.get("PCP"), "rain"),
            "snowfall_cm": _amount(values.get("SNO"), "snow"),
            "wind_speed_ms": pd.to_numeric(values.get("WSD"), errors="coerce"),
            "wind_direction_deg": pd.to_numeric(values.get("VEC"), errors="coerce"),
            "forecast_valid_at": selected.isoformat(),
            "forecast_issued_at": issue.replace(tzinfo=None).isoformat(),
            "grid_nx": int(nx),
            "grid_ny": int(ny),
        }
        return {
            key: (None if pd.isna(value) else float(value))
            if key in set(MODEL_FIELDS.values())
            else value
            for key, value in result.items()
        }

    def for_route(
        self,
        origin_latitude: float,
        origin_longitude: float,
        destination_latitude: float,
        destination_longitude: float,
        departure,
        now: datetime | None = None,
    ) -> dict:
        coordinates = {
            "origin": (float(origin_latitude), float(origin_longitude)),
            "destination": (float(destination_latitude), float(destination_longitude)),
            "seoul": (37.5665, 126.9780),
        }
        grids = {
            name: latitude_longitude_to_kma_grid(latitude, longitude)
            for name, (latitude, longitude) in coordinates.items()
        }
        issue = self._latest_issue(now)
        # 같은 격자에 속한 출발·도착지는 한 번만 호출합니다.
        grid_rows = {
            grid: self._download_grid(*grid, issue) for grid in sorted(set(grids.values()))
        }
        result = {
            name: self._at_departure(grid_rows[grid], departure, *grid, issue)
            for name, grid in grids.items()
        }
        result.update(
            {
                "provider": "kma_vilage_forecast",
                "requested_departure": _local_timestamp(departure).isoformat(),
                "unique_grid_calls": len(grid_rows),
            }
        )
        return result

    @staticmethod
    def model_weather(payload: Mapping) -> dict:
        allowed = set(MODEL_FIELDS.values())
        return {
            location: {
                key: value
                for key, value in payload[location].items()
                if key in allowed
            }
            for location in ("origin", "destination", "seoul")
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin-lat", type=float, required=True)
    parser.add_argument("--origin-lon", type=float, required=True)
    parser.add_argument("--destination-lat", type=float, required=True)
    parser.add_argument("--destination-lon", type=float, required=True)
    parser.add_argument("--departure", required=True)
    args = parser.parse_args()
    provider = KMAWeatherForecastProvider()
    forecast = provider.for_route(
        args.origin_lat,
        args.origin_lon,
        args.destination_lat,
        args.destination_lon,
        args.departure,
    )
    print(json.dumps(forecast, ensure_ascii=False, indent=2))
