"""Optional P50 personalization. Real observations only; base ETA cache is shared."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import os
from decimal import Decimal, ROUND_CEILING
from functools import lru_cache
from statistics import fmean, stdev

from project_paths import RUNTIME_CACHE_DIR, ETA_MODEL_DIR
from personal_affine import fit_affine, personalized_minutes


@dataclass(frozen=True)
class PersonalizationPolicy:
    # Provisional regularization, not fitted/validated on real personal data.
    prior_strength: float = 10.0
    instability_scale: float = 0.3
    max_abs_log_ratio: float = math.log(2.0)
    minimum_factor: float = 0.7
    maximum_factor: float = 1.5
    # Requested prototype policy: enforce a one-sided 95% prediction bound.
    # This nominal risk level is configurable, not fitted on real user data.
    history_filter_mode: str = "active"
    history_miscoverage: float | None = 0.05
    history_bound_method: str = "student_t"

    def __post_init__(self):
        if self.history_bound_method not in ("rank", "student_t"):
            raise ValueError("개인 예측 상한 방식은 rank 또는 student_t여야 합니다.")
        if self.history_filter_mode not in ("active", "shadow", "experimental"):
            raise ValueError(
                "개인 이력 필터는 active, shadow 또는 experimental 모드여야 합니다."
            )
        if (
            any(
                not math.isfinite(v) or v <= 0
                for v in (
                    self.prior_strength,
                    self.instability_scale,
                    self.max_abs_log_ratio,
                )
            )
            or not 0 < self.minimum_factor <= 1 <= self.maximum_factor
            or not math.isfinite(self.maximum_factor)
        ):
            raise ValueError("개인화 정책값이 올바르지 않습니다.")
        if self.history_miscoverage is not None and (
            not math.isfinite(self.history_miscoverage)
            or not 0 < self.history_miscoverage < 0.5
        ):
            raise ValueError(
                "개인 예측 상한의 명목 초과율은 0보다 크고 0.5보다 작아야 합니다."
            )


def _time(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("이용시각에는 시간대가 필요합니다.")
    return value.timestamp()


def _owner(profile_id):
    if not isinstance(profile_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{32,100}", profile_id
    ):
        raise ValueError("올바른 개인화 ID가 필요합니다.")
    return hashlib.sha256(profile_id.encode()).hexdigest()


def _station(value):
    value = str(value).strip()
    match = re.fullmatch(r"([0-9]+)(?:\.0)?", value)
    return str(int(match.group(1))) if match else value


def _weighted_median(values, weights):
    pairs = sorted(zip(values, weights))
    half = sum(weights) / 2
    total = 0.0
    for i, (value, weight) in enumerate(pairs):
        total += weight
        if total > half:
            return value
        if total == half and i + 1 < len(pairs):
            return (value + pairs[i + 1][0]) / 2
    return pairs[-1][0]


def _rank_upper_bound(scores, alpha):
    """Finite-sample one-sided order statistic; None denotes +infinity."""
    k = int(
        ((Decimal(len(scores)) + 1) * (1 - Decimal(str(alpha)))).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return k, sorted(scores)[k - 1] if k <= len(scores) else None


@lru_cache(maxsize=4096)
def _student_t_critical(alpha, df):
    from scipy.stats import t

    return float(t.isf(alpha, df))


def _student_t_upper_bound(scores, alpha):
    """Single future observation: mean + t*s*sqrt(1+1/n), not mean CI."""
    n = len(scores)
    details = dict(
        degrees_of_freedom=n - 1 if n else None,
        mean_log_ratio=None,
        sample_sd_log_ratio=None,
        mean_standard_error=None,
        prediction_standard_error=None,
        t_critical=None,
    )
    if n < 2:
        return None, dict(details, status="insufficient_history")
    mean = fmean(scores)
    sd = stdev(scores)
    details.update(
        mean_log_ratio=mean,
        sample_sd_log_ratio=sd,
        mean_standard_error=sd / math.sqrt(n),
        prediction_standard_error=sd * math.sqrt(1 + 1 / n),
    )
    # Rounded/identical records cannot support a nondegenerate Gaussian model.
    if sd <= 8 * math.ulp(max(1.0, abs(mean))):
        return None, dict(details, status="degenerate_variance")
    critical = _student_t_critical(alpha, n - 1)
    if not math.isfinite(critical):
        return None, dict(details, status="numerical_unbounded")
    details["t_critical"] = critical
    upper = mean + critical * details["prediction_standard_error"]
    if not math.isfinite(upper):
        return None, dict(details, status="numerical_unbounded")
    return upper, details


def baseline_context(model_version=None, model_dir=None):
    """Reset learned correction on changed ETA artifacts, never on weather changes."""
    from prediction_identity import prediction_code_identity

    folder = Path(model_dir or ETA_MODEL_DIR)
    if model_version is None:
        model_version = json.loads(
            (folder / "manifest.json").read_text(encoding="utf-8")
        )["model_version"]
    files = [
        (str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    ]
    digest = hashlib.sha256(
        json.dumps(
            [model_version, prediction_code_identity(), files], sort_keys=True
        ).encode()
    ).hexdigest()
    return str(model_version) + ":" + digest


class PersonalizationStore:
    def __init__(self, path=None, policy=None):
        raw_alpha = os.getenv("PERSONALIZATION_HISTORY_ALPHA", "").strip()
        self.policy = (
            policy
            if policy is not None
            else PersonalizationPolicy(
                history_miscoverage=float(raw_alpha) if raw_alpha else 0.05,
                history_filter_mode=os.getenv(
                    "PERSONALIZATION_HISTORY_MODE", "active"
                ).strip()
                or "active",
                history_bound_method=os.getenv(
                    "PERSONALIZATION_HISTORY_METHOD", "student_t"
                ).strip()
                or "student_t",
            )
        )
        path = Path(path or RUNTIME_CACHE_DIR / "personalization.sqlite")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS personal_profiles(owner TEXT PRIMARY KEY,created_ts REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS personal_rides(
                receipt TEXT PRIMARY KEY,owner TEXT NOT NULL REFERENCES personal_profiles(owner) ON DELETE CASCADE,
                start_id TEXT NOT NULL,end_id TEXT NOT NULL,started_ts REAL NOT NULL,
                base_p50 REAL NOT NULL,applied_factor REAL NOT NULL,context TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS personal_observations(
                receipt TEXT PRIMARY KEY REFERENCES personal_rides(receipt) ON DELETE CASCADE,
                returned_ts REAL NOT NULL,actual_end_id TEXT NOT NULL,actual_minutes REAL NOT NULL,
                usable INTEGER NOT NULL,reason TEXT,label_source TEXT NOT NULL DEFAULT 'self_reported');
            CREATE INDEX IF NOT EXISTS personal_rides_owner ON personal_rides(owner,context);
        """)
        # Additive migration: retain existing profiles, receipts and decisions.
        if "auto_filter_json" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(personal_observations)")
        }:
            self.db.execute(
                "ALTER TABLE personal_observations ADD COLUMN auto_filter_json TEXT"
            )
            self.db.commit()
        if self.policy.history_filter_mode == "shadow":
            self._restore_unvalidated_exclusions()

    def _restore_unvalidated_exclusions(self):
        """Undo earlier unvalidated exclusions, preserving the original decision."""
        rows = self.db.execute("""SELECT receipt,auto_filter_json FROM personal_observations
            WHERE reason='personal_history_long_outlier' AND usable=0""").fetchall()
        with self.db:
            for receipt, raw in rows:
                previous = json.loads(raw) if raw else {}
                if previous.get("parameters_validated") is True:
                    continue
                decision = dict(
                    algorithm="personal_history_filter_restoration_v1",
                    mode="shadow",
                    status="previous_unvalidated_exclusion_restored",
                    excluded=False,
                    suspected=True,
                    parameters_validated=False,
                    previous_decision=previous,
                )
                self.db.execute(
                    """UPDATE personal_observations SET usable=1,reason=NULL,
                    auto_filter_json=? WHERE receipt=?""",
                    (json.dumps(decision, allow_nan=False), receipt),
                )

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _require(self, profile_id):
        owner = _owner(profile_id)
        if not self.db.execute(
            "SELECT 1 FROM personal_profiles WHERE owner=?", (owner,)
        ).fetchone():
            raise ValueError("개인화 ID가 없거나 삭제되었습니다.")
        return owner

    def create_profile(self, now=None):
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute(
                "INSERT INTO personal_profiles VALUES (?,?)",
                (_owner(token), _time(now or datetime.now(timezone.utc))),
            )
        return token

    def profile(self, profile_id, context, as_of=None):
        owner = self._require(profile_id)
        cutoff = _time(as_of or datetime.now(timezone.utc))
        rows = self.db.execute(
            """SELECT r.base_p50,o.actual_minutes,r.start_id,r.end_id
            FROM personal_rides r JOIN personal_observations o ON r.receipt=o.receipt
            WHERE r.owner=? AND r.context=? AND o.usable=1 AND o.returned_ts<=?
            ORDER BY o.returned_ts,r.receipt""",
            (owner, context, cutoff),
        ).fetchall()
        p = self.policy
        n = len(rows)
        raw = [math.log(actual) - math.log(base) for base, actual, _, _ in rows]
        values = [max(-p.max_abs_log_ratio, min(p.max_abs_log_ratio, r)) for r in raw]
        # In |actual - factor*base| = base*|actual/base - factor|,
        # the base duration is the natural weight for minute-scale MAE.
        maximum_base = max((r[0] for r in rows), default=1.0)
        weights = [row[0] / maximum_base for row in rows]
        center = _weighted_median(values, weights) if n else 0.0
        mad = _weighted_median([abs(r - center) for r in values], weights) if n else 0.0
        information = (sum(weights) ** 2 / sum(w * w for w in weights)) if n else 0.0
        effective = information / (1 + (1.4826 * mad / p.instability_scale) ** 2)
        weight = effective / (effective + p.prior_strength)
        factor = max(p.minimum_factor, min(p.maximum_factor, math.exp(weight * center)))
        affine = fit_affine(rows, p)
        return dict(
            enabled=True,
            algorithm="robust_affine_shrinkage_v1",
            basis="base_p50",
            **affine,
            legacy_factor=factor,
            usable_records=n,
            distinct_od=len({(a, b) for _, _, a, b in rows}),
            shrinkage_weight=affine["fit_shrinkage_weight"],
            effective_records=affine["fit_effective_records"],
            mad_log_ratio=mad,
            bounded_influence_records=sum(abs(v) > p.max_abs_log_ratio for v in raw),
            cold_start=n == 0,
            calibrated=False,
            parameters_validated=False,
            label_source="self_reported",
            policy=dict(
                prior_strength=p.prior_strength,
                instability_scale=p.instability_scale,
                max_abs_log_ratio=p.max_abs_log_ratio,
                minimum_factor=p.minimum_factor,
                maximum_factor=p.maximum_factor,
                history_filter_mode=p.history_filter_mode,
                history_miscoverage=p.history_miscoverage,
                history_bound_method=p.history_bound_method,
            ),
        )

    def _history_filter(self, owner, context, started, base, actual):
        """One-sided rank/t prediction bound, not an exercise classifier.

        Use only rides finished before this rental. Include prior automatic
        exclusions to avoid truncating the calibration distribution. Coverage
        needs exchangeable scores; time-series drift can invalidate it.
        """
        p = self.policy
        rows = self.db.execute(
            """SELECT r.base_p50,o.actual_minutes FROM personal_rides r
            JOIN personal_observations o ON r.receipt=o.receipt
            WHERE r.owner=? AND r.context=? AND r.started_ts<? AND o.returned_ts<=?
              AND (o.usable=1 OR o.reason='personal_history_long_outlier')
            ORDER BY o.returned_ts,r.receipt""",
            (owner, context, started, started),
        ).fetchall()
        n = len(rows)
        alpha = p.history_miscoverage
        method = p.history_bound_method
        decision = dict(
            algorithm="personal_history_" + method + "_upper_v1",
            excluded=False,
            suspected=False,
            bound_method=method,
            mode=p.history_filter_mode,
            history_records=n,
            reference_before_rental=True,
            nominal_upper_tail_probability=alpha,
            parameters_validated=False,
            real_coverage_validated=False,
            requires_exchangeability=True,
            requires_independent_normal_log_scores=method == "student_t",
            upper_log_ratio=None,
            upper_minutes=None,
        )
        if alpha is None:
            return dict(decision, status="risk_level_not_configured")
        # Finite-sample rank correction, with +infinity when the rank is not
        # available. Decimal avoids rounding an integral rank up spuriously.
        scores = [math.log(a) - math.log(b) for b, a in rows]
        if method == "rank":
            k, log_limit = _rank_upper_bound(scores, alpha)
            details = dict(order_statistic_rank=k, status="insufficient_history")
        else:
            log_limit, details = _student_t_upper_bound(scores, alpha)
        decision.update(details)
        if log_limit is None:
            return dict(decision, status=details["status"], unbounded=True)
        log_actual = math.log(actual) - math.log(base)
        # Avoid classifying equal ratios as different due solely to log rounding.
        numerical_tie = math.isclose(
            log_actual,
            log_limit,
            rel_tol=0.0,
            abs_tol=8 * math.ulp(max(1.0, abs(log_actual), abs(log_limit))),
        )
        suspected = log_actual > log_limit and not numerical_tie
        excluded = suspected and p.history_filter_mode in ("active", "experimental")
        status = (
            "long_outlier"
            if excluded
            else "shadow_suspected"
            if suspected
            else "within_personal_range"
        )
        try:
            upper_minutes = math.exp(math.log(base) + log_limit)
        except OverflowError:
            upper_minutes = None
        return dict(
            decision,
            status=status,
            excluded=excluded,
            suspected=suspected,
            unbounded=False,
            upper_log_ratio=log_limit,
            observed_log_ratio=log_actual,
            upper_minutes=upper_minutes,
        )

    def start_trip(
        self,
        profile_id,
        start_id,
        end_id,
        started_at,
        base_p50,
        context,
        applied_factor=1.0,
        now=None,
    ):
        owner = self._require(profile_id)
        started = _time(started_at)
        current = _time(now or datetime.now(timezone.utc))
        base_p50 = float(base_p50)
        applied_factor = float(applied_factor)
        if (
            not math.isfinite(base_p50)
            or base_p50 <= 0
            or not math.isfinite(applied_factor)
            or not 0.7 <= applied_factor <= 1.5
        ):
            raise ValueError("기본 P50과 개인 배율을 확인하세요.")
        start_id, end_id = _station(start_id), _station(end_id)
        if not start_id or not end_id or start_id == end_id or started > current:
            raise ValueError("대여소와 실제 대여시각을 확인하세요.")
        receipt = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            active = self.db.execute(
                """SELECT 1 FROM personal_rides r
                LEFT JOIN personal_observations o ON r.receipt=o.receipt
                WHERE r.owner=? AND o.receipt IS NULL LIMIT 1""",
                (owner,),
            ).fetchone()
            if active:
                raise ValueError("진행 중인 대여 기록을 먼저 완료하거나 삭제하세요.")
            self.db.execute(
                "INSERT INTO personal_rides VALUES (?,?,?,?,?,?,?,?)",
                (
                    receipt,
                    owner,
                    start_id,
                    end_id,
                    started,
                    base_p50,
                    applied_factor,
                    str(context),
                ),
            )
        return dict(
            receipt=receipt,
            base_p50_min=base_p50,
            personal_p50_min=base_p50 * applied_factor,
            started_at=datetime.fromtimestamp(started, timezone.utc).isoformat(),
        )

    def finish_trip(
        self,
        profile_id,
        receipt,
        end_id,
        returned_at,
        paused=False,
        followed_route=True,
        now=None,
        leisure=False,
    ):
        owner = self._require(profile_id)
        row = self.db.execute(
            "SELECT end_id,started_ts,context FROM personal_rides WHERE receipt=? AND owner=?",
            (receipt, owner),
        ).fetchone()
        if row is None:
            raise ValueError("이 사용자의 대여 기록이 아닙니다.")
        expected, started, context = row
        returned = _time(returned_at)
        current = _time(now or datetime.now(timezone.utc))
        if returned <= started or returned > current:
            raise ValueError("실제 반납시각을 확인하세요.")
        end_id = _station(end_id)
        reason = (
            "exercise_or_leisure"
            if leisure
            else "different_return_station"
            if end_id != expected
            else "reported_pause"
            if paused
            else "different_route"
            if not followed_route
            else None
        )
        old = self.db.execute(
            "SELECT returned_ts,actual_end_id,reason,auto_filter_json FROM personal_observations WHERE receipt=?",
            (receipt,),
        ).fetchone()
        decision = None
        if old:
            # Idempotent retry must use the originally stored automatic decision.
            if reason is None and old[2] == "personal_history_long_outlier":
                reason = old[2]
            decision = json.loads(old[3]) if old[3] else None
        elif reason is None:
            base = self.db.execute(
                "SELECT base_p50 FROM personal_rides WHERE receipt=?", (receipt,)
            ).fetchone()[0]
            decision = self._history_filter(
                owner, context, started, base, (returned - started) / 60
            )
            if decision["excluded"]:
                reason = "personal_history_long_outlier"
        if old and old[:3] != (returned, end_id, reason):
            raise ValueError(
                "이미 완료된 기록입니다. 잘못 입력했다면 기록을 삭제한 뒤 다시 등록하세요."
            )
        if not old:
            with self.db:
                self.db.execute(
                    "INSERT INTO personal_observations(receipt,returned_ts,actual_end_id,actual_minutes,usable,reason,auto_filter_json) VALUES (?,?,?,?,?,?,?)",
                    (
                        receipt,
                        returned,
                        end_id,
                        (returned - started) / 60,
                        int(reason is None),
                        reason,
                        json.dumps(decision, allow_nan=False)
                        if decision is not None
                        else None,
                    ),
                )
        return dict(
            used_for_update=reason is None,
            exclusion_reason=reason,
            duplicate=bool(old),
            actual_minutes=(returned - started) / 60,
            auto_filter=decision,
            profile=self.profile(profile_id, context, as_of=current_datetime(current)),
        )

    def delete_profile(self, profile_id):
        owner = self._require(profile_id)
        with self.db:
            self.db.execute("DELETE FROM personal_profiles WHERE owner=?", (owner,))

    def import_screenshot_record(self, profile_id, record, base_p50, context, metadata):
        """Atomically import a confirmed observation, with owner-scoped deduplication.

        Timestamp minutes and displayed duration are separate observations; screenshots
        omit seconds. No active-rental receipt is fabricated through the start API.
        """
        owner = self._require(profile_id)
        started, returned = _time(record["started_at"]), _time(record["returned_at"])
        actual, base_p50 = float(record["actual_minutes"]), float(base_p50)
        if (
            not all(math.isfinite(v) and v > 0 for v in (actual, base_p50))
            or returned <= started
            or returned > datetime.now(timezone.utc).timestamp()
            or abs((returned - started) / 60 - actual) > 1.01
        ):
            raise ValueError("등록할 이용시간·시각 또는 기본 P50이 올바르지 않습니다.")
        start_id, end_id = (
            _station(record["start_station_id"]),
            _station(record["end_station_id"]),
        )
        if not start_id or not end_id or start_id == end_id:
            raise ValueError("서로 다른 대여소 두 곳이 필요합니다.")
        # Dedupe even if model version, image crop, OCR text, or bike ID changes.
        key = hashlib.sha256(
            json.dumps(
                [start_id, end_id, int(started // 60)], separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.db.execute("""CREATE TABLE IF NOT EXISTS screenshot_imports(
            owner TEXT NOT NULL,ride_key TEXT NOT NULL,
            receipt TEXT NOT NULL REFERENCES personal_rides(receipt) ON DELETE CASCADE,
            metadata_json TEXT NOT NULL,PRIMARY KEY(owner,ride_key))""")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute(
                "SELECT receipt FROM screenshot_imports WHERE owner=? AND ride_key=?",
                (owner, key),
            ).fetchone()
            if not old:
                old = self.db.execute(
                    """SELECT r.receipt FROM personal_rides r JOIN personal_observations o ON r.receipt=o.receipt
                    WHERE r.owner=? AND r.start_id=? AND r.end_id=? AND r.started_ts>=? AND r.started_ts<? LIMIT 1""",
                    (
                        owner,
                        start_id,
                        end_id,
                        int(started // 60) * 60,
                        int(started // 60) * 60 + 60,
                    ),
                ).fetchone()
            if old:
                receipt = old[0]
                row = self.db.execute(
                    "SELECT usable,reason,auto_filter_json FROM personal_observations WHERE receipt=?",
                    (receipt,),
                ).fetchone()
                decision = json.loads(row[2]) if row[2] else None
                duplicate = True
                reason = row[1]
                usable = bool(row[0])
            else:
                decision = self._history_filter(
                    owner, context, started, base_p50, actual
                )
                usable = not decision["excluded"]
                reason = None if usable else "personal_history_long_outlier"
                receipt = secrets.token_urlsafe(24)
                self.db.execute(
                    "INSERT INTO personal_rides VALUES (?,?,?,?,?,?,?,?)",
                    (
                        receipt,
                        owner,
                        start_id,
                        end_id,
                        started,
                        base_p50,
                        1.0,
                        str(context),
                    ),
                )
                self.db.execute(
                    """INSERT INTO personal_observations
                    (receipt,returned_ts,actual_end_id,actual_minutes,usable,reason,label_source,auto_filter_json)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        receipt,
                        returned,
                        end_id,
                        actual,
                        int(usable),
                        reason,
                        "screenshot_user_confirmed",
                        json.dumps(decision, allow_nan=False),
                    ),
                )
                self.db.execute(
                    "INSERT INTO screenshot_imports VALUES (?,?,?,?)",
                    (
                        owner,
                        key,
                        receipt,
                        json.dumps(metadata, ensure_ascii=False, allow_nan=False),
                    ),
                )
                duplicate = False
        return dict(
            receipt=receipt,
            duplicate=duplicate,
            used_for_update=usable,
            exclusion_reason=reason,
            auto_filter=decision,
            profile=self.profile(profile_id, context),
        )

    def delete_observation(self, profile_id, receipt):
        owner = self._require(profile_id)
        if not self.db.execute(
            "SELECT 1 FROM personal_rides WHERE receipt=? AND owner=?", (receipt, owner)
        ).fetchone():
            raise ValueError("이 사용자의 대여 기록이 아닙니다.")
        with self.db:
            self.db.execute("DELETE FROM personal_rides WHERE receipt=?", (receipt,))


def current_datetime(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc)


class PersonalizedBikeProvider:
    """Apply P50 correction after the shared base cache, before routing decisions."""

    def __init__(self, base, profile):
        if getattr(base, "eta_quantile", "p50") != "p50":
            raise ValueError("자동 개인화는 P50에서만 지원합니다.")
        self.base = base
        self.profile = dict(profile)
        self.eta_quantile = "p50"
        self.factor = float(profile["factor"])
        if not math.isfinite(self.factor) or not 0.7 <= self.factor <= 1.5:
            raise ValueError("개인 배율 범위를 확인하세요.")

    def __getattr__(self, name):
        return getattr(self.base, name)

    def __call__(self, edge, departure):
        value = dict(self.base(edge, departure))
        if float(value.get("user_factor", 1.0)) != 1.0:
            raise ValueError("개인화 전 캐시는 기본 배율 1.0이어야 합니다.")
        if "base_eta" not in value or "p50_min" not in value["base_eta"]:
            raise ValueError("개인화 전 기본 P50이 필요합니다.")
        base_minutes = float(value["base_eta"]["p50_min"])
        value["minutes"] = personalized_minutes(base_minutes, self.profile)
        if not math.isfinite(value["minutes"]) or value["minutes"] <= 0:
            raise ValueError("기본 P50이 올바르지 않습니다.")
        value.update(
            user_factor=self.factor,
            user_offset_min=float(self.profile.get("offset_min", 0.0)),
            effective_user_factor=value["minutes"] / base_minutes,
            personalization_calibrated=False,
            personalization=dict(self.profile),
        )
        return value
