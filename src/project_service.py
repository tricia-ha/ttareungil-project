"""Python service for routing, screenshot records and per-ID personalization."""

from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
import hashlib, io, json, secrets, time, uuid
import os
import threading
from web_capacity import LockedModel
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from project_paths import (
    load_project_env,
    resolve_project_path,
    RUNTIME_CACHE_DIR,
    RUNTIME_OUTPUT_DIR,
)
from personalization import PersonalizationStore, baseline_context
from screenshot_history import (
    recognize,
    extract_cards,
    validate_record,
    resolve_stations,
    catalogs,
)
from open_meteo_history import get_history_provider, HistoricalWeatherUnavailable


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Point(Input):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: str = ""


class RouteQuery(Input):
    action: Literal["route"]
    origin: Point
    destination: Point
    departure: str
    profile_id: str | None = None
    search_mode: Literal["integrated", "transit", "subway", "bicycle"] = "integrated"
    use_kma: bool = True
    use_inventory: bool = True
    refresh_inventory: bool = True
    user_factor: float = Field(1.0, ge=0.7, le=1.5)
    bike_type: str = "regular"
    user_type: str = "domestic"
    horizon_minutes: int = Field(180, gt=0, le=360)
    top_k: int = Field(3, ge=1, le=5)
    max_seconds: float = Field(30, gt=0, le=600)
    max_kakao_calls: int = Field(80, gt=0, le=200)
    max_empty_probability: float | None = Field(0.1, ge=0, le=1)


class ProfileQuery(Input):
    action: Literal[
        "profile.create", "profile.get", "profile.delete", "rides.list", "rides.delete"
    ]
    profile_id: str | None = None
    receipt: str | None = None


class ScreenshotQuery(Input):
    action: Literal["screenshot.preview"]
    profile_id: str
    image_path: str
    crop: tuple[int, int, int, int] | None = None


class RideEdits(Input):
    start_station_id: str | None = Field(default=None, max_length=30)
    end_station_id: str | None = Field(default=None, max_length=30)
    start_station_name: str | None = Field(default=None, max_length=200)
    end_station_name: str | None = Field(default=None, max_length=200)
    started_at: str | None = None
    returned_at: str | None = None
    actual_minutes: float | None = Field(default=None, gt=0)
    distance_km: float | None = Field(default=None, gt=0)
    bicycle_number: str | None = None


class ConfirmQuery(Input):
    action: Literal["screenshot.confirm"]
    profile_id: str
    draft_id: str = Field(min_length=20, max_length=100)
    card_index: int = Field(ge=0)
    confirmed: StrictBool
    manual: StrictBool = False
    edits: RideEdits = Field(default_factory=RideEdits)
    allow_missing_weather: bool = False


class StationQuery(Input):
    action: Literal["stations.resolve"]
    start_station_id: str = Field(max_length=30)
    end_station_id: str = Field(max_length=30)
    start_station_name: str = Field(default="", max_length=200)
    end_station_name: str = Field(default="", max_length=200)


def draft_table(store):
    store.db.execute("""CREATE TABLE IF NOT EXISTS screenshot_drafts(
        token TEXT PRIMARY KEY,owner TEXT NOT NULL REFERENCES personal_profiles(owner) ON DELETE CASCADE,
        expires REAL NOT NULL,payload TEXT NOT NULL)""")
    with store.db:
        store.db.execute(
            "DELETE FROM screenshot_drafts WHERE expires<?", (time.time(),)
        )


class ProjectService:
    def __init__(self, db_path=None, eta_service=None, weather_provider=None):
        load_project_env()
        self.db_path = Path(db_path or RUNTIME_CACHE_DIR / "personalization.sqlite")
        self.eta_service = eta_service
        self.owns_model = False
        self.weather_provider = weather_provider
        self._model_lock = threading.RLock()
        self._guarded_model = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        with self._model_lock:
            if self.owns_model and self.eta_service is not None:
                self.eta_service.close()
                self.eta_service = None
                self.owns_model = False
                self._guarded_model = None

    def store(self):
        return PersonalizationStore(self.db_path)

    def model(self):
        with self._model_lock:
            if self.eta_service is None:
                from eta_service import BicycleETAService
                from shared_assets import prepare_route_cache

                prepare_route_cache()
                self.eta_service = BicycleETAService()
                self.owns_model = True
            return self.eta_service

    def shared_model(self):
        with self._model_lock:
            if self._guarded_model is None:
                self._guarded_model = LockedModel(self.model(), self._model_lock)
            return self._guarded_model

    def context(self):
        model = self.eta_service
        return baseline_context(
            getattr(model, "model_version", None), getattr(model, "root", None)
        )

    def _execute(self, query):
        if not isinstance(query, dict):
            raise ValueError("요청은 딕셔너리여야 합니다.")
        action = query.get("action")
        if action == "route":
            return self.route(RouteQuery.model_validate(query))
        if action in (
            "profile.create",
            "profile.get",
            "profile.delete",
            "rides.list",
            "rides.delete",
        ):
            return self.profile(ProfileQuery.model_validate(query))
        if action == "screenshot.preview":
            return self.preview(ScreenshotQuery.model_validate(query))
        if action == "screenshot.confirm":
            return self.confirm(ConfirmQuery.model_validate(query))
        if action == "stations.resolve":
            q = StationQuery.model_validate(query)
            return resolve_stations(
                q.model_dump(),
                dict(origin=q.start_station_name, destination=q.end_station_name),
            )
        raise ValueError("지원하지 않는 action입니다.")

    def find_routes(
        self,
        origin,
        destination,
        departure="auto",
        mode="integrated",
        profile_id=None,
        on_routes=None,
        on_progress=None,
        **options,
    ):
        """Return routes/legs/times/geometry and search diagnostics as a Python dict."""
        query = dict(
            action="route",
            origin=origin,
            destination=destination,
            departure=departure,
            search_mode=mode,
            profile_id=profile_id,
            **options,
        )
        if on_routes is not None or on_progress is not None:
            return self.route(
                RouteQuery.model_validate(query),
                on_routes=on_routes,
                on_progress=on_progress,
            )
        return self._execute(query)

    def create_profile(self):
        return self._execute(dict(action="profile.create"))

    def get_profile(self, profile_id):
        return self._execute(dict(action="profile.get", profile_id=profile_id))

    def list_rides(self, profile_id):
        return self._execute(dict(action="rides.list", profile_id=profile_id))

    def delete_profile(self, profile_id):
        return self._execute(dict(action="profile.delete", profile_id=profile_id))

    def delete_ride(self, profile_id, receipt):
        return self._execute(
            dict(action="rides.delete", profile_id=profile_id, receipt=receipt)
        )

    def preview_screenshot(self, profile_id, image_path, crop=None):
        return self._execute(
            dict(
                action="screenshot.preview",
                profile_id=profile_id,
                image_path=str(image_path),
                crop=crop,
            )
        )

    def confirm_screenshot(
        self,
        profile_id,
        draft_id,
        card_index,
        *,
        confirmed,
        edits=None,
        allow_missing_weather=False,
        manual=False,
    ):
        return self._execute(
            dict(
                action="screenshot.confirm",
                profile_id=profile_id,
                draft_id=draft_id,
                card_index=card_index,
                confirmed=confirmed,
                manual=manual,
                edits=edits or {},
                allow_missing_weather=allow_missing_weather,
            )
        )

    def profile(self, q):
        with self.store() as store:
            if q.action == "profile.create":
                token = store.create_profile()
                return dict(
                    profile_id=token, profile=store.profile(token, self.context())
                )
            if not q.profile_id:
                raise ValueError("profile_id가 필요합니다.")
            owner = store._require(q.profile_id)
            if q.action == "profile.get":
                return store.profile(q.profile_id, self.context())
            if q.action == "profile.delete":
                store.delete_profile(q.profile_id)
                return {"deleted": True}
            if q.action == "rides.delete":
                if not q.receipt:
                    raise ValueError("receipt가 필요합니다.")
                store.delete_observation(q.profile_id, q.receipt)
                return dict(
                    deleted=True, profile=store.profile(q.profile_id, self.context())
                )
            rows = store.db.execute(
                """SELECT r.receipt,r.start_id,r.end_id,r.started_ts,o.returned_ts,
                r.base_p50,o.actual_minutes,o.usable,o.reason,o.label_source FROM personal_rides r
                JOIN personal_observations o ON r.receipt=o.receipt WHERE r.owner=? ORDER BY r.started_ts""",
                (owner,),
            ).fetchall()
            keys = [
                "receipt",
                "start_station_id",
                "end_station_id",
                "started_at",
                "returned_at",
                "base_p50_min",
                "actual_minutes",
                "used_for_update",
                "exclusion_reason",
                "label_source",
            ]
            records = []
            for row in rows:
                value = dict(zip(keys, row))
                for key in ("started_at", "returned_at"):
                    value[key] = datetime.fromtimestamp(
                        value[key], timezone.utc
                    ).isoformat()
                value["used_for_update"] = bool(value["used_for_update"])
                records.append(value)
            return dict(count=len(records), records=records)

    def preview(self, q):
        with self.store() as store:
            owner = store._require(q.profile_id)
        path = resolve_project_path(q.image_path)
        if not path.is_file() or path.stat().st_size > 12 * 1024 * 1024:
            raise ValueError("12MB 이내 PNG/JPEG 파일을 지정해 주세요.")
        data = path.read_bytes()
        return self.preview_screenshot_bytes(q.profile_id, data, q.crop)

    def preview_screenshot_bytes(self, profile_id, data, crop=None):
        """Recognize an uploaded image in memory without saving its original file."""
        q = ScreenshotQuery(
            action="screenshot.preview",
            profile_id=profile_id,
            image_path="memory",
            crop=crop,
        )
        crop = q.crop
        with self.store() as store:
            owner = store._require(q.profile_id)
        if not isinstance(data, bytes) or not data or len(data) > 12 * 1024 * 1024:
            raise ValueError("12MB 이내 PNG/JPEG 파일을 지정해 주세요.")
        if crop is not None:
            from PIL import Image, ImageOps

            with Image.open(io.BytesIO(data)) as source:
                if (
                    source.format not in ("PNG", "JPEG")
                    or source.width * source.height > 20_000_000
                ):
                    raise ValueError("PNG/JPEG, 최대 2천만 화소만 지원합니다.")
                image = ImageOps.exif_transpose(source)
                l, t, r, b = crop
                if not (0 <= l < r <= image.width and 0 <= t < b <= image.height):
                    raise ValueError(
                        "crop은 이미지 범위 내 [왼쪽, 위, 오른쪽, 아래]여야 합니다."
                    )
                buf = io.BytesIO()
                image.crop(crop).save(buf, format="PNG")
                data = buf.getvalue()
        tokens, size = recognize(data)
        result = extract_cards(tokens, size)
        from screenshot_manual import prepare_manual_review

        result = prepare_manual_review(result, tokens, size)
        if not result["manual_reviewable_count"]:
            return result
        draft = secrets.token_urlsafe(32)
        payload = dict(image_sha256=hashlib.sha256(data).hexdigest(), result=result)
        with self.store() as store:
            draft_table(store)
            with store.db:
                store.db.execute(
                    "INSERT INTO screenshot_drafts VALUES (?,?,?,?)",
                    (
                        draft,
                        owner,
                        time.time() + 1800,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
        return dict(result, draft_id=draft, expires_in_seconds=1800, image_size=size)

    def confirm(self, q):
        if not q.confirmed:
            raise ValueError(
                "인식 결과와 원본을 대조한 뒤 confirmed=true로 등록하세요."
            )
        with self.store() as store:
            owner = store._require(q.profile_id)
            draft_table(store)
            row = store.db.execute(
                "SELECT payload FROM screenshot_drafts WHERE token=? AND owner=? AND expires>?",
                (q.draft_id, owner, time.time()),
            ).fetchone()
        if row is None:
            raise ValueError("초안이 없거나 만료됐습니다. 사진을 다시 인식해 주세요.")
        draft = json.loads(row[0])
        cards = draft["result"]["cards"]
        if q.card_index >= len(cards):
            raise ValueError("수정할 이용내역을 선택해 주세요.")
        card = cards[q.card_index]
        if not card.get("reviewable") and not (
            q.manual and card.get("manual_editable")
        ):
            raise ValueError("직접 입력할 수 있는 이용내역을 선택해 주세요.")
        data = dict(card.get("record") or card.get("partial_record") or {})
        edits = q.edits.model_dump(exclude_none=True)
        data.update(edits)
        required = {
            "bicycle_number": "자전거 번호",
            "start_station_id": "출발 대여소 번호",
            "end_station_id": "도착 대여소 번호",
            "started_at": "대여 날짜·시각",
            "returned_at": "반납 날짜·시각",
            "actual_minutes": "이용시간",
            "distance_km": "이용거리",
        }
        missing = [
            label for key, label in required.items() if data.get(key) in (None, "")
        ]
        if missing:
            raise ValueError("다음 항목을 입력해 주세요: " + ", ".join(missing))
        originals = card.get("observed_station_names", {})
        names = dict(
            origin=edits.get("start_station_name", originals.get("origin", "")),
            destination=edits.get("end_station_name", originals.get("destination", "")),
        )
        mapping = resolve_stations(data, names)
        if not mapping["can_register"]:
            raise ValueError(
                " ".join(
                    s["message"]
                    for s in mapping["stations"].values()
                    if s["status"] != "matched"
                )
            )
        record = validate_record(data)
        stations, _ = catalogs()
        start, end = (
            stations[record["start_station_id"]],
            stations[record["end_station_id"]],
        )
        try:
            weather = (self.weather_provider or get_history_provider()).for_route(
                (start["latitude"], start["longitude"]),
                (end["latitude"], end["longitude"]),
                record["started_at"],
            )
        except HistoricalWeatherUnavailable as exc:
            if not q.allow_missing_weather:
                raise
            weather = dict(
                weather=None,
                source="historical_observation_unavailable",
                error=str(exc),
            )
        model = self.shared_model()
        context = self.context()
        prediction = model.predict(
            record["start_station_id"],
            record["end_station_id"],
            record["started_at"],
            bike_type=record["bike_type"],
            user_type="unknown",
            weather=weather["weather"],
        )
        metadata = dict(
            source="screenshot_user_confirmed",
            manual_correction=q.manual,
            image_sha256=draft["image_sha256"],
            original_ocr=card.get("record") or card.get("partial_record"),
            confirmed_record=record,
            original_station_names=originals,
            confirmed_station_names=names,
            station_mapping=mapping,
            ocr_corrections=card.get("corrections", []),
            historical_weather=weather,
            weather_source=weather["source"],
            user_type="unknown",
            bicycle_lookup_period="2025",
            authenticity_verified=False,
        )
        with self.store() as store:
            result = store.import_screenshot_record(
                q.profile_id, record, prediction["eta"]["p50_min"], context, metadata
            )
        return dict(
            result,
            record=record,
            station_mapping=mapping,
            base_p50_min=prediction["eta"]["p50_min"],
            weather_source=weather["source"],
            historical_weather=weather,
        )

    def route(self, q, on_routes=None, on_progress=None):
        from scheduled_runtime import ScheduledConfig, run_scheduled_optimization
        from route_progress import notify_progress

        when = None if q.departure == "auto" else q.departure
        if when is not None:
            parsed = datetime.fromisoformat(when)
            if parsed.utcoffset() is None or parsed <= datetime.now(timezone.utc):
                raise ValueError(
                    "departure는 시간대가 포함된 미래 시각 또는 auto여야 합니다."
                )
        uses_bike = q.search_mode not in ("subway", "transit")
        if not os.getenv("KAKAO_REST_API_KEY", "").strip():
            raise ValueError("KAKAO_REST_API_KEY를 설정해 주세요.")
        if (
            uses_bike
            and q.use_kma
            and not (os.getenv("KMA_API_KEY") or os.getenv("KMA_AUTH_KEY"))
        ):
            raise ValueError("KMA_API_KEY를 설정하거나 use_kma=false를 명시해 주세요.")
        if uses_bike and q.profile_id and q.user_factor != 1.0:
            raise ValueError(
                "profile_id 개인화와 수동 user_factor를 함께 사용할 수 없습니다."
            )
        if uses_bike and q.profile_id:
            with self.store() as store:
                store._require(q.profile_id)
        config = ScheduledConfig(
            search_mode=q.search_mode,
            horizon_minutes=q.horizon_minutes,
            top_k=q.top_k,
            max_seconds=q.max_seconds,
            max_kakao_calls=q.max_kakao_calls,
            bike_quantile="p50",
            require_known_inventory=q.use_inventory and uses_bike,
            max_empty_probability=q.max_empty_probability
            if q.use_inventory and uses_bike
            else None,
        )
        if uses_bike:
            notify_progress(on_progress, 'model')
        model = self.shared_model() if uses_bike else None
        return run_scheduled_optimization(
            q.origin.model_dump(),
            q.destination.model_dump(),
            when,
            config=config,
            use_kma=q.use_kma and uses_bike,
            use_inventory=q.use_inventory and uses_bike,
            refresh_inventory=q.refresh_inventory,
            eta_service=model,
            personalization_id=q.profile_id if uses_bike else None,
            personalization_path=self.db_path,
            user_factor=q.user_factor,
            bike_type=q.bike_type,
            user_type=q.user_type,
            output_dir=RUNTIME_OUTPUT_DIR / "runs" / uuid.uuid4().hex,
            progress=None,
            on_routes=on_routes,
            on_progress=on_progress,
        )
