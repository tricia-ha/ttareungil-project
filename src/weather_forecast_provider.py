"""Future weather adapter for bicycle ETA optimization and web services.

The provider batches origin, destination and central-Seoul coordinates into one
Open-Meteo request, caches the response, and returns exactly the feature names
expected by the V5.1/V5.2 ETA service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Mapping

import pandas as pd
import requests


HOURLY_VARIABLES = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "snowfall",
    "wind_speed_10m",
    "wind_direction_10m",
]
MODEL_NAMES = {
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "humidity_pct",
    "precipitation": "rainfall_mm",
    "snowfall": "snowfall_cm",
    "wind_speed_10m": "wind_speed_ms",
    "wind_direction_10m": "wind_direction_deg",
}


def _local_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp
    return timestamp.tz_convert("Asia/Seoul").tz_localize(None)


class WeatherForecastProvider:
    """Retrieve and cache route weather without exposing provider details to the model."""

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        cache_ttl_seconds: int = 900,
        timeout_seconds: int = 30,
        session: requests.Session | None = None,
    ):
        from project_paths import resolve_project_path

        self.cache_dir = resolve_project_path(
            cache_dir or os.getenv("BICYCLE_WEATHER_CACHE", "cache/weather_forecast")
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.timeout_seconds = int(timeout_seconds)
        self.session = session or requests.Session()
        self.endpoint = "https://api.open-meteo.com/v1/forecast"

    @staticmethod
    def _coordinate(latitude: float, longitude: float) -> tuple[float, float]:
        return round(float(latitude), 4), round(float(longitude), 4)

    def _cache_path(self, coordinates: list[tuple[float, float]]) -> Path:
        signature = hashlib.sha256(
            json.dumps(coordinates, separators=(",", ":")).encode()
        ).hexdigest()[:24]
        return self.cache_dir / f"{signature}.json"

    def _download(self, coordinates: list[tuple[float, float]]) -> list[dict]:
        cache_path = self._cache_path(coordinates)
        if (
            cache_path.exists()
            and time.time() - cache_path.stat().st_mtime <= self.cache_ttl_seconds
        ):
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else [payload]
        params = {
            "latitude": ",".join(str(value[0]) for value in coordinates),
            "longitude": ",".join(str(value[1]) for value in coordinates),
            "hourly": ",".join(HOURLY_VARIABLES),
            "forecast_days": 16,
            "timezone": "Asia/Seoul",
            "wind_speed_unit": "ms",
        }
        response = self.session.get(
            self.endpoint, params=params, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        payload = response.json()
        payload = payload if isinstance(payload, list) else [payload]
        temporary = cache_path.with_suffix(".tmp.json")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, cache_path)
        return payload

    @staticmethod
    def _at_departure(payload: Mapping, departure) -> dict:
        hourly = payload.get("hourly", {})
        times = pd.to_datetime(hourly.get("time", []), errors="coerce")
        if len(times) == 0:
            raise ValueError("Weather API response has no hourly forecast")
        target = _local_timestamp(departure)
        offsets = abs(times - target)
        index = int(offsets.argmin())
        if offsets[index] > pd.Timedelta("90min"):
            raise ValueError(f"No weather forecast near departure time: {target}")
        result = {}
        for source, target_name in MODEL_NAMES.items():
            values = hourly.get(source, [])
            result[target_name] = None if index >= len(values) else values[index]
        result["forecast_valid_at"] = pd.Timestamp(times[index]).isoformat()
        return result

    def for_route(
        self,
        origin_latitude: float,
        origin_longitude: float,
        destination_latitude: float,
        destination_longitude: float,
        departure,
    ) -> dict:
        coordinates = [
            self._coordinate(origin_latitude, origin_longitude),
            self._coordinate(destination_latitude, destination_longitude),
            self._coordinate(37.5665, 126.9780),
        ]
        payloads = self._download(coordinates)
        if len(payloads) != 3:
            raise ValueError(
                f"Expected three weather locations, received {len(payloads)}"
            )
        return {
            "origin": self._at_departure(payloads[0], departure),
            "destination": self._at_departure(payloads[1], departure),
            "seoul": self._at_departure(payloads[2], departure),
            "provider": "open-meteo",
            "requested_departure": _local_timestamp(departure).isoformat(),
        }

    @staticmethod
    def model_weather(payload: Mapping) -> dict:
        """Remove metadata and return the dictionary accepted by ETA predict methods."""
        allowed = set(MODEL_NAMES.values())
        return {
            location: {
                key: value for key, value in payload[location].items() if key in allowed
            }
            for location in ["origin", "destination", "seoul"]
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin-lat", type=float, required=True)
    parser.add_argument("--origin-lon", type=float, required=True)
    parser.add_argument("--destination-lat", type=float, required=True)
    parser.add_argument("--destination-lon", type=float, required=True)
    parser.add_argument("--departure", required=True)
    args = parser.parse_args()
    provider = WeatherForecastProvider()
    weather = provider.for_route(
        args.origin_lat,
        args.origin_lon,
        args.destination_lat,
        args.destination_lon,
        args.departure,
    )
    print(json.dumps(weather, ensure_ascii=False, indent=2))
