"""KMA AWS past observations for screenshot baselines, separate from forecasts."""

from __future__ import annotations
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import lru_cache
from contextlib import closing
import hashlib, json, math, os, re, sqlite3, threading, time
import requests
from project_paths import RUNTIME_CACHE_DIR, load_project_env

KST = timezone(timedelta(hours=9))
BASE = "https://apihub.kma.go.kr/api/typ01/url/"


class HistoricalWeatherUnavailable(RuntimeError):
    pass


def parse_table(text, required):
    """Read only named KMA columns. Never guess positional fields on schema changes."""
    columns = None
    rows = []
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        fields = re.split(r"[\s,]+", line.lstrip("#").strip())
        if line.startswith("#"):
            if set(required) <= set(fields):
                columns = fields
            continue
        if not line or not re.match(r"^\d", line):
            continue
        if columns is None:
            continue
        # Station names may contain spaces after the required numeric columns.
        if len(fields) < len(columns):
            continue
        rows.append(dict(zip(columns, fields)))
    if not rows:
        raise HistoricalWeatherUnavailable(
            "기상청 관측 응답의 항목 또는 자료를 확인할 수 없습니다."
        )
    return rows


def number(value, low, high):
    try:
        v = float(value)
    except (ValueError, TypeError):
        return None
    return v if math.isfinite(v) and low <= v <= high else None


def hour_at(value):
    t = datetime.fromisoformat(value) if isinstance(value, str) else value
    if t.utcoffset() is None:
        t = t.replace(tzinfo=KST)
    t = t.astimezone(KST).replace(minute=0, second=0, microsecond=0)
    if t > datetime.now(KST):
        raise HistoricalWeatherUnavailable("미래 시각의 관측자료는 조회할 수 없습니다.")
    return t.strftime("%Y%m%d%H00")


def distance_km(lat, lon, lat2, lon2):
    a, b = map(math.radians, (lat, lat2))
    dlat = b - a
    dlon = math.radians(lon2 - lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 12742 * math.asin(min(1, math.sqrt(h)))


class HistoricalWeatherProvider:
    def __init__(
        self, cache_path=None, session=None, api_key=None, max_distance_km=30.0
    ):
        self.cache_path = Path(
            cache_path or RUNTIME_CACHE_DIR / "historical_weather.sqlite"
        )
        self.session = session or requests.Session()
        self.api_key = api_key
        self.max_distance_km = max_distance_km
        self.lock = threading.RLock()
        self.failures = {}

    def _table(self, name, params, required):
        load_project_env()
        key = (
            self.api_key
            or os.getenv("KMA_HISTORY_API_KEY")
            or os.getenv("KMA_API_KEY")
            or os.getenv("KMA_AUTH_KEY")
        )
        identity = json.dumps([1, name, params], sort_keys=True, separators=(",", ":"))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.cache_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS tables_cache(identity TEXT PRIMARY KEY,rows_json TEXT NOT NULL)"
            )
            old = db.execute(
                "SELECT rows_json FROM tables_cache WHERE identity=?", (identity,)
            ).fetchone()
        if old:
            return json.loads(old[0]), True
        if not key:
            raise HistoricalWeatherUnavailable(
                "기상청 API허브 키가 없습니다. .env의 KMA_API_KEY를 설정해 주세요."
            )
        failed_key = (identity, hashlib.sha256(key.encode()).hexdigest())
        failed = self.failures.get(failed_key)
        if failed and failed[0] > time.monotonic():
            raise HistoricalWeatherUnavailable(failed[1])
        try:
            response = self.session.get(
                BASE + name, params=dict(params, authKey=key), timeout=(8, 20)
            )
            if response.status_code in (401, 403):
                raise HistoricalWeatherUnavailable(
                    "기상청 과거 관측자료 접근이 거절됐습니다. AWS 시간통계 정시자료와 AWS 지점정보 활용 권한을 확인해 주세요."
                )
            if response.status_code == 429:
                raise HistoricalWeatherUnavailable(
                    "기상청 호출 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."
                )
            if response.status_code != 200:
                raise HistoricalWeatherUnavailable(
                    "기상청 과거 관측자료 조회가 실패했습니다."
                )
            raw = response.content
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError:
                text = raw.decode("cp949", errors="replace")
            rows = parse_table(text, required)
            # Only parsed table values are cached; URLs/keys/error bodies are not stored.
            with closing(sqlite3.connect(self.cache_path)) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO tables_cache VALUES (?,?)",
                    (identity, json.dumps(rows, ensure_ascii=False)),
                )
            return rows, False
        except (HistoricalWeatherUnavailable, requests.RequestException) as exc:
            message = (
                str(exc)
                if isinstance(exc, HistoricalWeatherUnavailable)
                else "기상청 과거 날씨 통신에 실패했습니다. 다시 시도해 주세요."
            )
            self.failures[failed_key] = (time.monotonic() + 120, message)
            raise HistoricalWeatherUnavailable(message) from None

    def for_route(self, origin, destination, departure):
        """Same departure hour for both endpoints. No next-hour/return-time weather."""
        tm = hour_at(departure)
        with self.lock:
            stations, meta_hit = self._table(
                "stn_inf.php",
                dict(inf="AWS", tm=tm, help="1"),
                {"STN_ID", "LON", "LAT"},
            )
            observations, obs_hit = self._table(
                "awsh.php",
                dict(tm=tm, stn="0", help="1"),
                {"STN", "TA", "HM", "WD", "WS", "RN_HR1"},
            )
        obs = {
            str(int(r["STN"])): r
            for r in observations
            if r.get("STN", "").isdigit()
            and r.get("TM", r.get("YYMMDDHHMI", "")).replace("-", "").replace(":", "")
            in (tm, tm[:10])
        }
        points = []
        for s in stations:
            sid = s.get("STN_ID", "")
            if not sid.isdigit():
                continue
            lat, lon = number(s.get("LAT"), -90, 90), number(s.get("LON"), -180, 180)
            r = obs.get(str(int(sid)))
            if lat is None or lon is None or r is None:
                continue
            weather = dict(
                temperature_c=number(r.get("TA"), -80, 60),
                humidity_pct=number(r.get("HM"), 0, 100),
                wind_speed_ms=number(r.get("WS"), 0, 150),
                wind_direction_deg=number(r.get("WD"), 0, 360),
                rainfall_mm=number(r.get("RN_HR1"), 0, 1000),
                snowfall_cm=None,
            )
            if weather["temperature_c"] is not None:
                points.append((lat, lon, str(int(sid)), weather))
        if not points:
            raise HistoricalWeatherUnavailable(
                "해당 시각의 유효한 기상청 관측자료가 없습니다."
            )
        values = {}
        locations = {}
        for name, point in [
            ("origin", origin),
            ("destination", destination),
            ("seoul", (37.5665, 126.9780)),
        ]:
            lat, lon = map(float, point)
            best = min(points, key=lambda p: distance_km(lat, lon, p[0], p[1]))
            dist = distance_km(lat, lon, best[0], best[1])
            if dist > self.max_distance_km:
                raise HistoricalWeatherUnavailable(
                    "대여소 근처에 해당 시각의 유효한 관측소가 없습니다."
                )
            values[name] = best[3]
            locations[name] = dict(
                station_id=best[2],
                distance_km=dist,
                latitude=best[0],
                longitude=best[1],
                missing_fields=[k for k, v in best[3].items() if v is None],
            )
        return dict(
            weather=values,
            source="kma_aws_hourly_observation",
            observed_at=tm,
            requested_departure=str(departure),
            time_policy="departure_hour_floor_KST",
            locations=locations,
            cache_hits=dict(stations=meta_hit, observations=obs_hit),
            new_api_calls=int(not meta_hit) + int(not obs_hit),
            notes=[
                "서울 값은 시청 좌표 인근 관측소 기준이며 서울 전체 평균이 아닙니다.",
                "AWS 시간자료의 미제공 적설은 결측으로 유지합니다.",
            ],
        )


@lru_cache(maxsize=1)
def get_history_provider():
    return HistoricalWeatherProvider()
