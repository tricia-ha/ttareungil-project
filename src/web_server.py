"""Single-process PC web server; shares one model and keeps uploads in memory."""

from __future__ import annotations
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from collections import deque
from urllib.parse import urlsplit
import hashlib, json, os, re, secrets, socket, threading, time
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import Field, StrictBool, ValidationError
from project_paths import PROJECT_ROOT, RUNTIME_CACHE_DIR, load_project_env
from project_service import ProjectService, Input, Point, RideEdits
from web_accounts import WebAccounts
from web_capacity import Capacity, GIB
from route_identity import distinct_routes
from route_progress import STAGES

WEB = PROJECT_ROOT / "web"
MAX_IMAGE = 12 * 1024 * 1024


def safe_error(exc):
    if isinstance(exc, ValidationError):
        first = exc.errors(include_input=False, include_url=False)[0]
        labels = {
            "origin": "출발지",
            "destination": "도착지",
            "latitude": "위도",
            "longitude": "경도",
            "departure": "출발 날짜·시각",
            "mode": "경로 종류",
            "max_seconds": "탐색시간",
            "use_kma": "날씨 설정",
            "use_inventory": "재고 설정",
            "personalize": "개인화 설정",
            "username": "아이디",
            "password": "비밀번호",
            "current_password": "현재 비밀번호",
            "new_password": "새 비밀번호",
        }
        parts = [labels[key] for key in first.get("loc", ()) if key in labels]
        field = " ".join(parts) or "입력값"
        if first["type"] == "missing":
            return field + " 정보가 없습니다. 입력하거나 다시 선택해 주세요."
        return (
            field
            + " 형식을 확인해 주세요."
            + (
                " 장소는 검색 결과 또는 지도에서 다시 선택할 수 있습니다."
                if first.get("loc") and first["loc"][0] in ("origin", "destination")
                else ""
            )
        )
    name = type(exc).__name__
    if isinstance(exc, ValueError) or name in (
        "HistoricalWeatherUnavailable",
        "KakaoError",
        "CallLimit",
    ):
        message = str(exc)
    elif isinstance(exc, ImportError):
        message = "필수 패키지가 없습니다. 웹_설치.cmd와 OCR_설치.cmd를 확인해 주세요."
    else:
        message = (
            "처리를 완료하지 못했습니다. 서버 설정·네트워크·모델 파일을 확인해 주세요."
        )
    for key, value in os.environ.items():
        if any(part in key for part in ("KEY", "TOKEN", "SECRET")) and len(value) >= 8:
            message = message.replace(value, "[숨김]")
    return re.sub(r"https?://\S+", "외부 API", message)[:500]


def compact_routes(result):
    keep = (
        "routes",
        "mixed_routes",
        "status",
        "search_complete",
        "termination_reason",
        "elapsed_seconds",
        "data_gaps",
        "limitations",
        "personalization",
        "api_usage",
        "model_versions",
        "weather_requested",
        "inventory_connected",
        "departure",
        "bicycle_time_quantile",
        "search_mode",
        "geometry_summary",
    )
    value = {key: result[key] for key in keep if key in result}
    for key in ("routes", "mixed_routes"):
        if key in value:
            value[key] = distinct_routes(value[key])
    ids = {
        leg[key]
        for routes in ("routes", "mixed_routes")
        for route in result.get(routes, [])
        for leg in route.get("legs", [])
        for key in ("from", "to")
        if key in leg
    }
    value["nodes"] = {
        key: node for key, node in result.get("nodes", {}).items() if key in ids
    }
    return value


class PlacePoint(Point):
    # Place search includes a display address; the routing service needs only Point.
    address: str = ""


class RouteInput(Input):
    origin: PlacePoint
    destination: PlacePoint
    departure: str = "auto"
    mode: str = "integrated"
    use_kma: StrictBool = True
    use_inventory: StrictBool = True
    personalize: StrictBool = True
    max_seconds: float = Field(30, gt=0, le=90)


class LoginInput(Input):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class PasswordInput(Input):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class ConfirmInput(Input):
    draft_id: str = Field(min_length=20, max_length=100)
    card_index: int = Field(ge=0)
    confirmed: StrictBool
    manual: StrictBool = False
    edits: RideEdits = Field(default_factory=RideEdits)


@dataclass
class Session:
    token: str
    profile_id: str | None = None
    touched: float = 0.0


class Backend:
    def __init__(self, service_factory=None, *, testing=False):
        self.service = (
            service_factory
            or (
                lambda: ProjectService(
                    db_path=RUNTIME_CACHE_DIR / "web_personalization.sqlite"
                )
            )
        )()
        self.accounts = WebAccounts(self.service.db_path)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bicycle")
        self.capacity = Capacity(
            probe=(lambda: dict(available=32 * GIB, total=32 * GIB, cpu=0))
            if testing
            else None,
            model_ready=lambda: self.service.eta_service is not None,
        )
        self.lock = threading.RLock()
        self.jobs = {}
        self.sessions = {}
        self.rates = {}
        self.places = {}
        self.last_inventory_refresh = 0.0

    def close(self):
        self.capacity.close()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.service.close()

    def session(self, request):
        token = request.cookies.get("tw_browser", "")
        now = time.monotonic()
        with self.lock:
            self.sessions = {
                k: s for k, s in self.sessions.items() if now - s.touched < 86400
            }
            if token not in self.sessions:
                if len(self.sessions) >= 1000:
                    raise HTTPException(
                        503, "접속자가 많습니다. 잠시 후 다시 시도해 주세요."
                    )
                token = secrets.token_urlsafe(32)
                profile = request.cookies.get("tw_profile")
                if profile:
                    try:
                        self.service.get_profile(profile)
                    except ValueError:
                        profile = None
                self.sessions[token] = Session(token, profile, now)
                request.state.new_browser = token
            session = self.sessions[token]
            session.touched = now
            if session.profile_id and not self.accounts.valid_session(
                session.profile_id, request.cookies.get("tw_auth")
            ):
                session.profile_id = None
                request.state.profile_cookie = None
                request.state.auth_cookie = None
            return session

    def throttle(self, key, limit=10):
        now = time.monotonic()
        with self.lock:
            if len(self.rates) > 3000:
                self.rates = {
                    k: v for k, v in self.rates.items() if v and now - v[-1] < 60
                }
            values = self.rates.setdefault(key, deque())
            while values and now - values[0] > 60:
                values.popleft()
            if len(values) >= limit:
                raise HTTPException(429, "요청이 많습니다. 잠시 후 다시 시도해 주세요.")
            values.append(now)

    def principal(self, session):
        return (session.token, session.profile_id)

    def idle(self, session):
        with self.lock:
            if any(
                j["owner"][0] == session.token and j["status"] in ("queued", "running")
                for j in self.jobs.values()
            ):
                raise HTTPException(409, "진행 중인 작업이 끝난 뒤 다시 시도해 주세요.")

    def submit(
        self, session, kind, fn, *, stream=False, model_required=True, route_query=None
    ):
        with self.lock:
            now = time.monotonic()
            self.jobs = {
                k: j
                for k, j in self.jobs.items()
                if j["status"] in ("queued", "running") or now - j["created"] < 900
            }
            self.idle(session)
            if (
                sum(j["status"] in ("queued", "running") for j in self.jobs.values())
                >= 6
            ):
                raise HTTPException(
                    429, "현재 탐색 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요."
                )
            if len(self.jobs) >= 80:
                raise HTTPException(429, "잠시 후 다시 시도해 주세요.")
            job_id = secrets.token_urlsafe(24)
            self.jobs[job_id] = dict(
                owner=self.principal(session),
                kind=kind,
                status="queued",
                created=now,
                model_required=model_required,
                route_query=route_query,
            )
            self._progress(self.jobs[job_id], 'queued')
            self.executor.submit(self.run, job_id, fn, stream)
            return dict(job_id=job_id, status="queued")

    def _progress(self, job, stage):
        if (
            job.get('kind') != 'route'
            or not isinstance(stage, str)
            or stage not in STAGES
        ):
            return
        if job.get('progress', {}).get('stage') == stage:
            return
        message, detail = STAGES[stage]
        job.update(
            progress=dict(stage=stage, message=message, detail=detail),
            stage_started=time.monotonic(),
        )

    def snapshot(self, job):
        value = {
            k: v for k, v in job.items()
            if k not in ('owner', 'created', 'stage_started', 'finished')
        }
        if job.get('progress'):
            now = job.get('finished', time.monotonic())
            value['progress'] = dict(
                job['progress'],
                elapsed_seconds=max(0, int(now - job['created'])),
                stage_elapsed_seconds=max(0, int(now - job.get('stage_started', now))),
            )
        return value

    def publish(self, job_id, result):
        if 'progress_stage' in result:
            with self.lock:
                job = self.jobs[job_id]
                if job['status'] == 'running':
                    self._progress(job, result['progress_stage'])
            return
        snapshot = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] == "running":
                job.update(partial_result=snapshot, revision=job.get("revision", 0) + 1)

    def run(self, job_id, fn, stream=False):
        try:
            job = self.jobs[job_id]
            resource_kind = (
                "transit"
                if job["kind"] == "route" and not job["model_required"]
                else job["kind"]
            )
            with self.capacity.slot(resource_kind):
                with self.lock:
                    self.jobs[job_id]["status"] = "running"
                    self._progress(self.jobs[job_id], 'starting')
                result = (
                    fn(lambda value: self.publish(job_id, value)) if stream else fn()
                )
            # Validate JSON before publishing; no NaN or arbitrary Python values.
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            with self.lock:
                self.jobs[job_id].update(status="done", result=result)
                self.jobs[job_id].pop("partial_result", None)
        except Exception as exc:
            with self.lock:
                job = self.jobs[job_id]
                partial = job.get("partial_result")
                if job["kind"] == "route" and partial and partial.get("routes"):
                    partial.update(
                        status="partial",
                        search_complete=False,
                        termination_reason="interrupted_after_partial",
                    )
                    job.update(status="done", result=partial)
                    job.pop("partial_result", None)
                else:
                    job.update(status="error", error=safe_error(exc))
        finally:
            with self.lock:
                job = self.jobs[job_id]
                job['finished'] = time.monotonic()
                self._progress(job, 'done' if job['status'] == 'done' else 'error')


def allowed_hosts():
    names = ["localhost", "127.0.0.1", "::1", "*.trycloudflare.com"]
    from web_tunnel_config import configured_host

    ngrok_host = configured_host()
    if ngrok_host:
        names.append(ngrok_host)
    try:
        names.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    names.extend(
        v.strip() for v in os.getenv("WEB_ALLOWED_HOSTS", "").split(",") if v.strip()
    )
    return list(dict.fromkeys(names))


def create_app(service_factory=None, *, testing=False):
    load_project_env()

    @asynccontextmanager
    async def lifespan(app):
        app.state.backend = Backend(service_factory, testing=testing)
        try:
            yield
        finally:
            app.state.backend.close()

    app = FastAPI(
        title="따라와",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts() + (["testserver"] if testing else []),
    )

    @app.middleware("http")
    async def protection(request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            cross = request.headers.get("sec-fetch-site") == "cross-site"
            valid = bool(
                origin
                and urlsplit(origin).netloc.lower()
                == request.headers.get("host", "").lower()
                and urlsplit(origin).scheme in ("http", "https")
            )
            if (
                cross
                or origin
                and not valid
                or request.method not in ("GET", "HEAD")
                and not valid
            ):
                return JSONResponse(
                    {"detail": "같은 사이트에서 요청해 주세요."}, status_code=403
                )
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse(
                    {"detail": "요청 길이가 올바르지 않습니다."}, status_code=400
                )
            if length > MAX_IMAGE:
                return JSONResponse(
                    {"detail": "12MB 이내 파일만 지원합니다."}, status_code=413
                )
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=(self)",
                "Content-Security-Policy": "default-src 'self'; script-src 'self' https://unpkg.com; style-src 'self' 'unsafe-inline' https://unpkg.com; img-src 'self' data: blob: https://tile.openstreetmap.org https://*.tile.openstreetmap.org https://unpkg.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            }
        )
        if request.url.path in ("/app.js", "/app.css", "/base.css"):
            response.headers["Cache-Control"] = "no-cache"
        if request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            secure = request.url.scheme == "https"
            if getattr(request.state, "new_browser", None):
                response.set_cookie(
                    "tw_browser",
                    request.state.new_browser,
                    max_age=86400,
                    httponly=True,
                    samesite="lax",
                    secure=secure,
                )
            if hasattr(request.state, "profile_cookie"):
                if request.state.profile_cookie:
                    response.set_cookie(
                        "tw_profile",
                        request.state.profile_cookie,
                        max_age=30 * 86400,
                        httponly=True,
                        samesite="lax",
                        secure=secure,
                    )
                else:
                    response.delete_cookie("tw_profile")
            if hasattr(request.state, "auth_cookie"):
                if request.state.auth_cookie:
                    response.set_cookie(
                        "tw_auth",
                        request.state.auth_cookie,
                        max_age=30 * 86400,
                        httponly=True,
                        samesite="lax",
                        secure=secure,
                    )
                else:
                    response.delete_cookie("tw_auth")
        return response

    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": safe_error(exc)}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": safe_error(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        return JSONResponse({"detail": safe_error(exc)}, status_code=500)

    def state(request):
        return request.app.state.backend

    def signed(request):
        backend = state(request)
        session = backend.session(request)
        if not session.profile_id:
            raise HTTPException(401, "개인 ID를 만들거나 불러와 주세요.")
        return backend, session

    async def read_json(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 32768:
                raise HTTPException(413, "입력 크기를 확인해 주세요.")
        try:
            return json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "입력 형식을 확인해 주세요.") from None

    @app.get("/api/health")
    def health(request: Request):
        backend = state(request)
        with backend.lock:
            statuses = [j["status"] for j in backend.jobs.values()]
        return dict(
            ok=True,
            model_version="V5.2.1",
            model_loaded=backend.service.eta_service is not None,
            running=statuses.count("running"),
            queued=statuses.count("queued"),
            max_parallel_routes=2,
            configured={
                k: bool(os.getenv(v))
                for k, v in [
                    ("kakao", "KAKAO_REST_API_KEY"),
                    ("weather", "KMA_API_KEY"),
                    ("inventory", "SEOUL_API_KEY"),
                ]
            },
            pending=sum(status in ("queued", "running") for status in statuses),
        )

    @app.get("/api/session")
    def session_info(request: Request):
        backend = state(request)
        session = backend.session(request)
        try:
            profile = (
                backend.service.get_profile(session.profile_id)
                if session.profile_id
                else None
            )
        except ValueError:
            session.profile_id = None
            request.state.profile_cookie = None
            profile = None
        return dict(
            profile_id=session.profile_id,
            username=backend.accounts.username(session.profile_id),
            profile=profile,
        )

    @app.post("/api/account")
    async def create_profile(request: Request):
        backend = state(request)
        session = backend.session(request)
        backend.idle(session)
        backend.throttle(("account", request.client.host), 10)
        q = LoginInput.model_validate(await read_json(request))
        if session.profile_id and backend.accounts.username(session.profile_id):
            raise ValueError(
                "이미 로그인되어 있습니다. 다른 아이디를 만들려면 로그아웃해 주세요."
            )
        previous = session.profile_id
        profile_id = previous or backend.service.create_profile()["profile_id"]
        try:
            username = backend.accounts.register(q.username, q.password, profile_id)
        except Exception:
            if not previous:
                backend.service.delete_profile(profile_id)
            raise
        session.profile_id = profile_id
        request.state.profile_cookie = profile_id
        request.state.auth_cookie = backend.accounts.session_key(profile_id)
        return dict(
            profile_id=profile_id,
            username=username,
            profile=backend.service.get_profile(profile_id),
        )

    @app.post("/api/session")
    async def restore(request: Request):
        backend = state(request)
        session = backend.session(request)
        backend.idle(session)
        backend.throttle(("restore", request.client.host), 10)
        q = LoginInput.model_validate(await read_json(request))
        backend.throttle(("login_name", q.username.casefold()), 6)
        profile_id = backend.accounts.login(q.username, q.password)
        profile = backend.service.get_profile(profile_id)
        session.profile_id = profile_id
        request.state.profile_cookie = profile_id
        request.state.auth_cookie = backend.accounts.session_key(profile_id)
        return dict(
            profile_id=profile_id,
            username=backend.accounts.username(profile_id),
            profile=profile,
        )

    @app.delete("/api/session")
    def logout(request: Request):
        backend = state(request)
        session = backend.session(request)
        backend.idle(session)
        session.profile_id = None
        request.state.profile_cookie = None
        request.state.auth_cookie = None
        return {"ok": True}

    @app.post("/api/account/password")
    async def change_password(request: Request):
        backend, session = signed(request)
        backend.idle(session)
        backend.throttle(("password", request.client.host), 5)
        q = PasswordInput.model_validate(await read_json(request))
        request.state.auth_cookie = backend.accounts.change_password(
            session.profile_id, q.current_password, q.new_password
        )
        return {"changed": True}

    @app.get("/api/rides")
    def rides(request: Request):
        backend, session = signed(request)
        return backend.service.list_rides(session.profile_id)

    @app.delete("/api/rides/{receipt}")
    def delete_ride(receipt: str, request: Request):
        backend, session = signed(request)
        backend.idle(session)
        return backend.service.delete_ride(session.profile_id, receipt)

    @app.delete("/api/account")
    def delete_account(request: Request):
        backend, session = signed(request)
        backend.idle(session)
        backend.service.delete_profile(session.profile_id)
        backend.accounts.delete(session.profile_id)
        session.profile_id = None
        request.state.profile_cookie = None
        request.state.auth_cookie = None
        return {"ok": True}

    @app.get("/api/stations")
    def stations():
        from screenshot_history import catalogs

        return [
            dict(
                station_id=key,
                name=value["station_name"],
                latitude=float(value["latitude"]),
                longitude=float(value["longitude"]),
                district=value.get("district", ""),
            )
            for key, value in catalogs()[0].items()
        ]

    @app.get("/api/places")
    def places(request: Request, q: str = ""):
        backend = state(request)
        session = backend.session(request)
        q = q.strip()
        if not 2 <= len(q) <= 80:
            raise HTTPException(400, "장소 이름을 2~80자로 입력해 주세요.")
        backend.throttle(("places", session.token), 10)
        from screenshot_history import catalogs

        matches = [
            dict(
                name=f"{key}. {v['station_name']}",
                latitude=float(v["latitude"]),
                longitude=float(v["longitude"]),
                address=v.get("district", ""),
            )
            for key, v in catalogs()[0].items()
            if q in v["station_name"] or q == key
        ][:8]
        exact = q.isdigit() or any(
            q == v["station_name"] for v in catalogs()[0].values()
        )
        if matches and exact:
            return matches
        with backend.lock:
            cached = backend.places.get(q)
            if cached and time.monotonic() - cached[0] < 86400:
                return cached[1]
        key = os.getenv("KAKAO_REST_API_KEY")
        if not key:
            if matches:
                return matches
            raise HTTPException(
                400, "장소 검색 키가 없습니다. 지도에서 위치를 선택해 주세요."
            )
        backend.throttle(("places_global",), 30)
        try:
            response = requests.get(
                "https://dapi.kakao.com/v2/local/search/keyword.json",
                params={"query": q, "x": 126.978, "y": 37.5665, "size": 8},
                headers={"Authorization": "KakaoAK " + key},
                timeout=(5, 12),
            )
            if response.status_code != 200:
                if matches:
                    return matches
                raise ValueError(
                    "장소 검색 권한·호출 한도를 확인하거나 지도에서 위치를 선택해 주세요."
                )
            values = [
                dict(
                    name=r["place_name"],
                    latitude=float(r["y"]),
                    longitude=float(r["x"]),
                    address=r.get("road_address_name") or r.get("address_name", ""),
                )
                for r in response.json()["documents"]
                if 37.0 < float(r["y"]) < 38.2 and 126.3 < float(r["x"]) < 127.8
            ]
        except requests.RequestException:
            raise ValueError(
                "장소 검색 연결에 실패했습니다. 지도에서도 위치를 선택할 수 있습니다."
            ) from None
        with backend.lock:
            if len(backend.places) >= 1000:
                backend.places.pop(next(iter(backend.places)))
            backend.places[q] = (time.monotonic(), values + matches[:3])
        return values + matches[:3]

    @app.post("/api/routes", status_code=202)
    async def routes(request: Request):
        backend = state(request)
        session = backend.session(request)
        backend.throttle(("route", session.token), 6)
        backend.throttle(("route_ip", request.client.host), 20)
        q = RouteInput.model_validate(await read_json(request))
        if q.mode not in ("integrated", "transit", "bicycle"):
            raise ValueError("경로 종류를 확인해 주세요.")
        profile = session.profile_id if q.personalize else None

        def work(on_update):
            refresh = time.monotonic() - backend.last_inventory_refresh >= 60
            result = backend.service.find_routes(
                q.origin.model_dump(exclude={"address"}),
                q.destination.model_dump(exclude={"address"}),
                departure=q.departure,
                mode=q.mode,
                profile_id=profile,
                use_kma=q.use_kma,
                use_inventory=q.use_inventory,
                refresh_inventory=refresh,
                max_seconds=q.max_seconds,
                max_kakao_calls=min(
                    80, max(1, int(os.getenv("WEB_MAX_KAKAO_CALLS", "20")))
                ),
                on_routes=lambda value: on_update(compact_routes(value)),
                on_progress=lambda stage: on_update({'progress_stage': stage}),
            )
            if q.mode != "transit" and q.use_inventory and refresh:
                backend.last_inventory_refresh = time.monotonic()
            return compact_routes(result)

        return backend.submit(
            session,
            "route",
            work,
            stream=True,
            model_required=q.mode != "transit",
            route_query=q.model_dump(mode="json"),
        )

    @app.get("/api/routes/current")
    def current_route(request: Request):
        backend = state(request)
        session = backend.session(request)
        now = time.monotonic()
        with backend.lock:
            owned = [
                (key, value)
                for key, value in backend.jobs.items()
                if value["owner"] == backend.principal(session)
                and value["kind"] == "route"
                and (
                    value["status"] in ("queued", "running")
                    or now - value["created"] < 900
                )
            ]
            if not owned:
                return {"job": None}
            key, value = max(owned, key=lambda item: item[1]["created"])
            return {
                "job": dict(
                    job_id=key,
                    **backend.snapshot(value),
                )
            }

    @app.post("/api/screenshots", status_code=202)
    async def screenshot(request: Request):
        backend, session = signed(request)
        backend.idle(session)
        backend.throttle(("ocr", session.token), 6)
        if request.headers.get("content-type", "").split(";")[0] not in (
            "image/png",
            "image/jpeg",
            "application/octet-stream",
        ):
            raise HTTPException(415, "PNG/JPEG 스크린샷을 선택해 주세요.")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_IMAGE:
                raise HTTPException(413, "12MB 이내 사진만 지원합니다.")
        if not data:
            raise ValueError("사진을 선택해 주세요.")
        image = bytes(data)
        profile = session.profile_id
        return backend.submit(
            session,
            "screenshot",
            lambda: backend.service.preview_screenshot_bytes(profile, image),
        )

    @app.post("/api/screenshots/confirm", status_code=202)
    async def confirm(request: Request):
        backend, session = signed(request)
        backend.throttle(("confirm", session.token), 10)
        q = ConfirmInput.model_validate(await read_json(request))
        profile = session.profile_id
        return backend.submit(
            session,
            "confirm",
            lambda: backend.service.confirm_screenshot(
                profile,
                q.draft_id,
                q.card_index,
                confirmed=q.confirmed,
                manual=q.manual,
                edits=q.edits.model_dump(exclude_none=True),
            ),
        )

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, request: Request):
        backend = state(request)
        session = backend.session(request)
        with backend.lock:
            value = backend.jobs.get(job_id)
            if not value or value["owner"] != backend.principal(session):
                raise HTTPException(404, "작업을 찾을 수 없습니다.")
            return backend.snapshot(value)

    @app.get("/")
    def index():
        html = (WEB / "index.html").read_text(encoding="utf-8-sig")
        for name in ("base.css", "app.css", "app.js"):
            version = hashlib.sha256((WEB / name).read_bytes()).hexdigest()[:12]
            html = html.replace(f'"/{name}"', f'"/{name}?v={version}"')
        return HTMLResponse(html)

    @app.get("/app.js")
    def javascript():
        return FileResponse(WEB / "app.js", media_type="text/javascript")

    @app.get("/app.css")
    def css():
        return FileResponse(WEB / "app.css", media_type="text/css")

    @app.get("/base.css")
    def base_css():
        return FileResponse(WEB / "base.css", media_type="text/css")

    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")
    return app
