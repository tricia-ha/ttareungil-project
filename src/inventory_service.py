from project_paths import INVENTORY_MODEL_DIR

"""현재 재고 + 학습한 시간별 대여/반납 강도 -> 도착시 재고 0 확률.

0에서는 대여가 막히고 반납은 허용되는 birth-death CTMC. 반납 상한 없음.
이 확률은 모형 가정하 추정치이며 실제 재고 이력으로 보정되지 않았다.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import csv, json, math, hashlib, io
import numpy as np
from copy import deepcopy
from types import MappingProxyType
from threading import RLock
from file_signature import file_signature

KST = timezone(timedelta(hours=9))


def parse_time(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("시각은 ISO 문자열 또는 datetime이어야 합니다.")
    t = datetime.fromisoformat(value) if isinstance(value, str) else value
    return t.replace(tzinfo=KST) if t.tzinfo is None else t.astimezone(KST)


def advance(p, rent, ret, hours):
    q = rent + ret
    if q == 0 or hours == 0:
        return p.copy()
    pieces = max(1, math.ceil(q * hours / 20))
    x = q * hours / pieces
    for _ in range(pieces):
        v = p.copy()
        w = math.exp(-x)
        out = w * v
        for k in range(1, math.ceil(x + 12 * math.sqrt(x) + 40)):
            nxt = np.zeros_like(v)
            nxt[:-1] += v[1:] * rent / q
            nxt[0] += v[0] * rent / q
            nxt[1:] += v[:-1] * ret / q
            nxt[-1] += v[-1] * ret / q
            v = nxt
            w *= x / k
            out += w * v
            if k + 2 > x and w * x / (k + 1) / (1 - x / (k + 2)) < 1e-13:
                break
        p = out
    return p / np.sum(p)


class InventoryPredictor:
    _loaded = {}
    _load_lock = RLock()

    def __init__(self, rates_path=None):
        rates_path = Path(
            rates_path or INVENTORY_MODEL_DIR / "weekly_rates.csv"
        ).resolve()
        key = (
            type(self),
            file_signature(
                [
                    rates_path,
                    rates_path.parent / "model_metadata.json",
                    rates_path.parent / "station_activity.csv",
                ]
            ),
        )
        with self._load_lock:
            cached = self._loaded.get(key)
            if cached is None:
                self._load(rates_path)
                self.rates = MappingProxyType(self.rates)
                self.station_ids = frozenset(self.station_ids)
                cached = (
                    deepcopy(self.metadata),
                    self.training_end,
                    self.training_date,
                    self.rates,
                    self.station_ids,
                )
                self._loaded.clear()  # One current model; no growing copy of 46만 rows per query.
                self._loaded[key] = cached
            self.metadata = deepcopy(cached[0])
            self.training_end, self.training_date, self.rates, self.station_ids = (
                cached[1:]
            )

    def _load(self, rates_path=None):
        rates_path = rates_path or INVENTORY_MODEL_DIR / "weekly_rates.csv"
        metadata_path = Path(rates_path).parent / "model_metadata.json"
        if not metadata_path.exists():
            raise ValueError(
                "model_metadata.json이 필요합니다. 모델 파일을 함께 복사하세요."
            )
        self.metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        content = Path(rates_path).read_bytes()
        if hashlib.sha256(content).hexdigest() != self.metadata.get(
            "weekly_rates_sha256"
        ):
            raise ValueError(
                "모델 강도 표와 메타데이터 버전이 다릅니다. 재학습 완료 후 다시 로드하세요."
            )
        self.training_end = self.metadata["production_training_end"]
        self.training_date = datetime.fromisoformat(self.training_end).date()
        self.rates = {}
        counts = {}
        with io.StringIO(content.decode("utf-8-sig"), newline="") as f:
            for row in csv.DictReader(f):
                key = (row["station_id"], int(row["dow"]), int(row["hour"]))
                values = (
                    float(row["rentals_per_hour"]),
                    float(row["returns_per_hour"]),
                )
                if key in self.rates or not (0 <= key[1] < 7 and 0 <= key[2] < 24):
                    raise ValueError(
                        "수요 모델에 중복 또는 잘못된 요일·시간 키가 있습니다."
                    )
                if not all((math.isfinite(x) and x >= 0 for x in values)):
                    raise ValueError("수요 강도는 0 이상 유한수여야 합니다.")
                evidence = (
                    tuple(
                        (
                            int(row[col])
                            for col in (
                                "observed_weekday_hours",
                                "positive_rental_observations",
                                "positive_return_observations",
                            )
                        )
                    )
                    if "observed_weekday_hours" in row
                    else ()
                )
                if evidence and (
                    not (
                        evidence[0] >= 0
                        and 0 <= evidence[1] <= evidence[0]
                        and (0 <= evidence[2] <= evidence[0])
                    )
                ):
                    raise ValueError("학습 관측 수가 잘못되었습니다.")
                self.rates[key] = values + evidence
                counts[key[0]] = counts.get(key[0], 0) + 1
        if not counts or any((n != 168 for n in counts.values())):
            raise ValueError(
                "모델 표가 비어 있거나 일부 대여소의 요일·시간 168칸이 누락되었습니다."
            )
        activity_path = Path(rates_path).parent / "station_activity.csv"
        if not activity_path.exists():
            raise ValueError(
                "station_activity.csv가 필요합니다. 모델 패키지를 함께 복사하세요."
            )
        with activity_path.open(encoding="utf-8-sig", newline="") as f:
            active = {
                row["station_id"]
                for row in csv.DictReader(f)
                if float(row["full_flow_total"]) > 0
            }
        self.station_ids = {k[0] for k in self.rates} & active
        if (
            self.station_ids != set(counts)
            or len(self.station_ids) != self.metadata["scope_stations"]
        ):
            raise ValueError(
                "최종 모델 범위와 전체 학습 거래 이력이 일치하지 않습니다."
            )
        if not self.station_ids:
            raise ValueError("학습 이력으로 확인된 지원 대여소가 없습니다.")

    def predict(
        self,
        station_id,
        current_bikes,
        eta_minutes,
        as_of,
        snapshot_at=None,
        epsilon=0.1,
        rental_multiplier=1.0,
        return_multiplier=1.0,
    ):
        if station_id not in self.station_ids:
            raise ValueError("모델 범위 밖 대여소입니다.")
        if (
            isinstance(current_bikes, bool)
            or not isinstance(current_bikes, (int, np.integer))
            or (not 0 <= current_bikes <= 500)
        ):
            raise ValueError("current_bikes는 0~500의 정수여야 합니다.")
        numeric = (
            lambda x: isinstance(x, (int, float, np.integer, np.floating))
            and (not isinstance(x, (bool, np.bool_)))
            and math.isfinite(float(x))
        )
        if not numeric(eta_minutes) or not 0 <= eta_minutes <= 60:
            raise ValueError("eta_minutes는 0~60분 유한수여야 합니다.")
        if not numeric(epsilon) or not 0 < epsilon < 1:
            raise ValueError("epsilon은 0과 1 사이 유한수여야 합니다.")
        if not all(
            (numeric(x) and 0 <= x <= 5 for x in [rental_multiplier, return_multiplier])
        ):
            raise ValueError("수요 배수는 0~5 유한수여야 합니다.")
        start = parse_time(as_of)
        if snapshot_at:
            age = (start - parse_time(snapshot_at)).total_seconds()
            if not 0 <= age <= 600:
                raise ValueError(
                    "재고 관측은 예측 기준시각보다 과거 10분 이내여야 합니다."
                )
        else:
            age = 0
        end = start + timedelta(minutes=float(eta_minutes))
        t = start - timedelta(seconds=age)
        segments = []
        mr = mt = 0.0
        evidence = []
        while t < end:
            boundary = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            finish = min(boundary, end)
            hours = (finish - t).total_seconds() / 3600
            values = self.rates[station_id, t.weekday(), t.hour]
            r, b = values[:2]
            r *= rental_multiplier
            b *= return_multiplier
            if len(values) == 5:
                evidence.append(
                    {
                        "dow": t.weekday(),
                        "hour": t.hour,
                        "observed_weekday_hours": values[2],
                        "positive_rental_observations": values[3],
                        "positive_return_observations": values[4],
                    }
                )
            segments.append((r, b, hours))
            mr += r * hours
            mt += b * hours
            t = finish
        n = current_bikes + max(
            80, math.ceil(1.5 * mt + 12 * math.sqrt(1.5 * mt + 1) + 30)
        )
        p = np.zeros(n + 1)
        p[current_bikes] = 1
        for r, b, h in segments:
            p = advance(p, r, b, h)
        cdf = np.cumsum(p)
        quant = lambda v: int(np.searchsorted(cdf, v))
        if current_bikes == 0:
            upper = 1.0
        else:
            term = math.exp(-mr)
            cdf_r = term
            for k in range(1, current_bikes):
                term *= mr / k
                cdf_r += term
            upper = max(0.0, min(1.0, 1 - cdf_r))
        risk = float(p[0])
        stresses = []
        for name, rf, bf in [
            ("lower_depletion", 0.5, 1.5),
            ("higher_depletion", 1.5, 0.5),
        ]:
            state = np.zeros(n + 1)
            state[current_bikes] = 1
            for r, b, h in segments:
                state = advance(state, r * rf, b * bf, h)
            stresses.append(
                {
                    "scenario": name,
                    "rental_intensity_factor": rf,
                    "return_intensity_factor": bf,
                    "p_empty_at_arrival": float(state[0]),
                }
            )
        low, high = (
            stresses[0]["p_empty_at_arrival"],
            stresses[1]["p_empty_at_arrival"],
        )
        return {
            "station_id": station_id,
            "as_of": start.isoformat(),
            "arrival_at": end.isoformat(),
            "current_bikes": int(current_bikes),
            "eta_minutes": float(eta_minutes),
            "rate_training_evidence": evidence,
            "rate_evidence_interpretation": "source hourly records; not independent sample size or uncertainty",
            "snapshot_age_seconds": age,
            "prediction_horizon_minutes": float(eta_minutes) + age / 60,
            "expected_rentals": mr,
            "expected_returns": mt,
            "expected_flow_interpretation": "integrated unconstrained flow intensity; blocked departures are not realized rentals",
            "p_empty_at_arrival": risk,
            "p_available_at_arrival": 1 - risk,
            "expected_inventory_at_arrival": float(np.dot(np.arange(len(p)), p)),
            "inventory_interval_90": [quant(0.05), quant(0.95)],
            "inventory_interval_interpretation": "conditional process interval at fixed rates; excludes parameter uncertainty",
            "no_return_risk": upper,
            "no_return_risk_interpretation": "upper comparison under the same rental intensity and no returns, not an unconditional guarantee",
            "epsilon": float(epsilon),
            "passes_risk_constraint": bool(risk <= epsilon),
            "stress_passes_risk_constraint": bool(high <= epsilon),
            "stress_risk_range": [low, high],
            "stress_scenarios": stresses,
            "stress_interpretation": "chosen +/-50% rate scenarios; not a confidence interval or calibrated bound",
            "probability_status": "assumption_based_uncalibrated",
            "demand_model": self.metadata.get("selected_model", "unspecified"),
            "demand_training_end": self.training_end,
            "days_since_demand_training": (start.date() - self.training_date).days,
            "station_in_evaluation_population": station_id
            not in self.metadata.get("production_only_station_ids", []),
            "exposure_policy": (self.metadata.get("selected_parameters") or {}).get(
                "exposure_policy", "calendar"
            ),
            "inventory_source": "provided_snapshot"
            if snapshot_at
            else "manual_scenario",
            "scope": "서울 전체 학습 이력 대여소",
            "model_assumptions": [
                "independent Poisson flows",
                "hourly constant intensity",
                "no redistribution",
                "no return capacity ceiling",
                "observed completed rentals proxy demand",
            ],
        }
