"""Past weather with the training dataset's exact query points and preprocessing."""

from contextlib import closing
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from pathlib import Path
import json, math, sqlite3, threading, time
import requests
from project_paths import PROJECT_ROOT, RUNTIME_CACHE_DIR
from historical_weather import HistoricalWeatherUnavailable

KST = timezone(timedelta(hours=9))
URL = "https://archive-api.open-meteo.com/v1/archive"
FIELDS = dict(
    temperature_2m="temperature_c",
    relative_humidity_2m="humidity_pct",
    precipitation="rainfall_mm",
    snowfall="snowfall_cm",
    wind_speed_10m="wind_speed_ms",
    wind_direction_10m="wind_direction_deg",
)
UNITS = dict(
    temperature_2m="°C",
    relative_humidity_2m="%",
    precipitation="mm",
    snowfall="cm",
    wind_speed_10m="m/s",
    wind_direction_10m="°",
)


def local_hour(value):
    t = datetime.fromisoformat(value) if isinstance(value, str) else value
    if t.utcoffset() is None:
        t = t.replace(tzinfo=KST)
    t = t.astimezone(KST)
    if t > datetime.now(KST):
        raise HistoricalWeatherUnavailable(
            "미래 시각은 과거 날씨로 조회할 수 없습니다."
        )
    return t.replace(minute=0, second=0, microsecond=0, tzinfo=None)


def clean(value, field):
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise HistoricalWeatherUnavailable(
            "과거 날씨의 숫자 형식을 확인할 수 없습니다."
        ) from None
    ranges = dict(
        temperature_c=(-80, 60),
        humidity_pct=(0, 100),
        rainfall_mm=(0, 1000),
        snowfall_cm=(0, 1000),
        wind_speed_ms=(0, 150),
        wind_direction_deg=(0, 360),
    )
    lo, hi = ranges[field]
    return v if math.isfinite(v) and lo <= v <= hi else None


class OpenMeteoHistoryProvider:
    def __init__(self, cache_path=None, session=None, locations=None, local_path=None):
        self.cache_path = Path(
            cache_path or RUNTIME_CACHE_DIR / "open_meteo_history.sqlite"
        )
        self.session = session or requests.Session()
        self.lock = threading.RLock()
        self.failures = {}
        self.local_path = (
            Path(local_path)
            if local_path is not None
            else PROJECT_ROOT / "data/weather/training_weather.parquet"
        )
        if locations is None:
            path = PROJECT_ROOT / "data/weather/training_weather_locations.json"
            if not path.exists():
                raise HistoricalWeatherUnavailable(
                    "학습 날씨의 대표 좌표 파일이 없습니다."
                )
            locations = json.loads(path.read_text(encoding="utf-8"))["locations"]
        self.locations = locations
        if not locations:
            raise ValueError("날씨 조회 지점이 필요합니다.")

    def _select_hour(self, rows, hour):
        selected = {}
        for row in rows:
            t = row["valid_at"]
            if isinstance(t, str):
                t = datetime.fromisoformat(t)
            if t.utcoffset() is not None:
                t = t.astimezone(KST).replace(tzinfo=None)
            if not hour - timedelta(hours=2) <= t <= hour:
                continue
            loc = row["location"]
            if loc not in selected or t > selected[loc]["valid_at"]:
                r = dict(row, valid_at=t)
                for field in FIELDS.values():
                    r[field] = clean(r.get(field), field)
                selected[loc] = r
        return list(selected.values())

    def _local(self, hour):
        if not self.local_path.exists():
            return []
        import pyarrow.parquet as pq

        try:
            rows = pq.read_table(
                self.local_path,
                filters=[
                    ("valid_at", ">=", hour - timedelta(hours=2)),
                    ("valid_at", "<=", hour),
                    ("available_at", "<=", hour),
                ],
            ).to_pylist()
        except (OSError, ValueError):
            raise HistoricalWeatherUnavailable(
                "보유 학습 날씨 파일을 읽지 못했습니다."
            ) from None
        return self._select_hour(rows, hour)

    def _day(self, day):
        params = dict(
            latitude=",".join(str(p["latitude"]) for p in self.locations),
            longitude=",".join(str(p["longitude"]) for p in self.locations),
            start_date=day,
            end_date=day,
            timezone="Asia/Seoul",
            wind_speed_unit="ms",
            hourly=",".join(FIELDS),
        )
        identity = json.dumps([2, URL, params], sort_keys=True, separators=(",", ":"))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.cache_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS archive_days(identity TEXT PRIMARY KEY,payload TEXT NOT NULL)"
            )
            cached = db.execute(
                "SELECT payload FROM archive_days WHERE identity=?", (identity,)
            ).fetchone()
        if cached:
            saved = json.loads(cached[0])
            recent = (
                datetime.now(KST).date() - datetime.fromisoformat(day).date()
            ).days <= 10
            # Recent archive days may still be updated. Never freeze null/not-yet-ready data forever.
            if not recent or time.time() - saved.get("fetched_at", 0) < 3600:
                return saved, True
        if self.failures.get(identity, (0, ""))[0] > time.monotonic():
            raise HistoricalWeatherUnavailable(self.failures[identity][1])
        try:
            response = self.session.get(URL, params=params, timeout=(10, 45))
            if response.status_code == 429:
                raise HistoricalWeatherUnavailable(
                    "Open-Meteo 호출 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."
                )
            if response.status_code != 200:
                raise HistoricalWeatherUnavailable(
                    "Open-Meteo에서 해당 날짜 자료를 받지 못했습니다. 최근 날짜는 아직 제공되지 않을 수 있습니다."
                )
            payload = response.json()
            items = payload if isinstance(payload, list) else [payload]
            if len(items) != len(self.locations):
                raise HistoricalWeatherUnavailable(
                    "날씨 조회 지점 수가 요청과 다릅니다."
                )
            rows = []
            grids = []
            for point, item in zip(self.locations, items):
                if item.get("utc_offset_seconds") != 32400:
                    raise HistoricalWeatherUnavailable(
                        "날씨 응답의 한국시간 설정이 맞지 않습니다."
                    )
                hours = item.get("hourly", {})
                stamps = hours.get("time", [])
                if not stamps or any(
                    len(hours.get(k, [])) != len(stamps) for k in FIELDS
                ):
                    raise HistoricalWeatherUnavailable(
                        "날씨 응답에 필수 항목이 누락됐습니다."
                    )
                if any(
                    item.get("hourly_units", {}).get(k) != unit
                    for k, unit in UNITS.items()
                ):
                    raise HistoricalWeatherUnavailable(
                        "학습 때 사용한 날씨 단위와 응답 단위가 다릅니다."
                    )
                grids.append(
                    dict(
                        location=point["location"],
                        latitude=item.get("latitude"),
                        longitude=item.get("longitude"),
                    )
                )
                for i, stamp in enumerate(stamps):
                    try:
                        t = datetime.fromisoformat(stamp)
                    except (TypeError, ValueError):
                        raise HistoricalWeatherUnavailable(
                            "날씨 응답 시각 형식이 올바르지 않습니다."
                        ) from None
                    if t.tzinfo is not None or t.date().isoformat() != day:
                        raise HistoricalWeatherUnavailable(
                            "요청과 다른 날짜의 날씨가 반환됐습니다."
                        )
                    rows.append(
                        dict(
                            point,
                            valid_at=stamp,
                            **{f: clean(hours[k][i], f) for k, f in FIELDS.items()},
                        )
                    )
            if not any(r["temperature_c"] is not None for r in rows):
                raise HistoricalWeatherUnavailable(
                    "해당 날짜의 과거 날씨가 아직 준비되지 않았습니다. 나중에 다시 등록해 주세요."
                )
            result = dict(rows=rows, grids=grids, fetched_at=time.time())
            with closing(sqlite3.connect(self.cache_path)) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO archive_days VALUES (?,?)",
                    (identity, json.dumps(result, allow_nan=False)),
                )
            return result, False
        except (
            requests.RequestException,
            ValueError,
            HistoricalWeatherUnavailable,
        ) as exc:
            message = (
                str(exc)
                if isinstance(exc, HistoricalWeatherUnavailable)
                else "Open-Meteo 과거 날씨 조회에 실패했습니다."
            )
            self.failures[identity] = (time.monotonic() + 120, message)
            raise HistoricalWeatherUnavailable(message) from None

    def for_route(self, origin, destination, departure):
        hour = local_hour(departure)
        with self.lock:
            rows = self._local(hour)
            grids = []
            hit = True
            calls = 0
            source = "open_meteo_training_archive"
            if len(rows) != len(self.locations):
                day_result, hit = self._day(hour.date().isoformat())
                rows = self._select_hour(day_result["rows"], hour)
                grids = day_result["grids"]
                source = "open_meteo_historical_archive"
                calls = int(not hit)
        if len(rows) != len(self.locations) or not any(
            r["temperature_c"] is not None for r in rows
        ):
            raise HistoricalWeatherUnavailable(
                "해당 출발시각의 충분한 과거 날씨 자료가 없습니다. 자료가 제공된 뒤 다시 등록해 주세요."
            )
        values = {}
        selection = {}
        for name, (lat, lon) in [("origin", origin), ("destination", destination)]:
            lat, lon = float(lat), float(lon)
            r = min(
                rows,
                key=lambda r: (float(r["latitude"]) - lat) ** 2
                + (float(r["longitude"]) - lon) ** 2,
            )
            values[name] = {f: r[f] for f in FIELDS.values()}
            selection[name] = dict(
                location=r["location"],
                latitude=r["latitude"],
                longitude=r["longitude"],
                valid_at=r["valid_at"].isoformat(),
                missing_fields=[f for f, v in values[name].items() if v is None],
            )
        values["seoul"] = {}
        for field in FIELDS.values():
            available = [r[field] for r in rows if r[field] is not None]
            values["seoul"][field] = (
                sum(available) / len(available) if available else None
            )
        return dict(
            weather=values,
            source=source,
            observed_at=hour.isoformat(),
            requested_departure=str(departure),
            time_policy="training_hour_floor_KST_latest_within_2h",
            locations=selection,
            seoul_policy="training_mean_of_26_query_points",
            location_count=len(rows),
            cache_hit=hit,
            new_api_calls=calls,
            requested_location_count=0 if not calls else len(self.locations),
            response_grids=grids,
            models_parameter=None,
            notes=[
                "관측소 실측이 아닌 Open-Meteo 과거 재분석·분석 자료입니다.",
                "학습 요청은 기상모델을 고정하지 않아 과거 서비스 변경까지 완전히 재현하지는 못합니다.",
            ],
        )


@lru_cache(maxsize=1)
def get_history_provider():
    return OpenMeteoHistoryProvider()
