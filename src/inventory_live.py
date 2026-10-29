from project_paths import INVENTORY_SNAPSHOT_DIR as INVENTORY_DATA_DIR
from inventory_credentials import get_api_key

"""서울 bikeList 수집. sample은 5개만 반환하며 전체 수집으로 표시하지 않는다."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json, os, re, sqlite3, time, urllib.request, uuid
from contextlib import contextmanager, closing

KST = timezone(timedelta(hours=9))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def collector_lock(directory):
    """OS lock releases on crash, so restarting does not need stale PID cleanup."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "collector.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("이미 이 폴더의 재고 수집기가 실행 중입니다.") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def fetch_snapshot(sample=False, opener=None):
    key = "sample" if sample else get_api_key()
    if not key:
        raise ValueError(
            "전체 재고 수집에는 SEOUL_API_KEY가 필요합니다. --sample은 5개 시험 응답입니다."
        )
    opener = opener or urllib.request.urlopen
    rows = []
    seen = set()
    started = datetime.now(KST).isoformat()
    for page in range(20):
        start = page * 1000 + 1
        end = 5 if sample else start + 999
        try:
            with opener(
                f"http://openapi.seoul.go.kr:8088/{key}/json/bikeList/{start}/{end}/",
                timeout=20,
            ) as response:
                data = json.load(response)
        except Exception:
            raise RuntimeError(
                "서울 API 연결 실패. 인증키·네트워크를 확인하세요."
            ) from None
        payload = data.get("rentBikeStatus", {})
        result = payload.get("RESULT", data.get("RESULT", {}))
        code = result.get("CODE")
        if code == "INFO-200" and rows:
            break
        if code != "INFO-000":
            raise RuntimeError("서울 API 오류 코드: " + str(code))
        batch = payload.get("row", [])
        if not batch:
            if rows:
                break
            raise RuntimeError("서울 API 재고 응답이 비어 있습니다.")
        observed = datetime.now(KST).isoformat()
        for r in batch:
            sid = r.get("stationId", "")
            m = re.match("^\\s*(\\d+)\\s*\\.", r.get("stationName", ""))
            if not re.fullmatch("ST-\\d+", sid) or not m:
                raise RuntimeError("API 대여소 키 형식이 예상과 다릅니다.")
            if sid in seen:
                raise RuntimeError(
                    "API 페이징에 중복 ID가 있습니다. 부분 자료를 전체로 사용하지 않습니다."
                )
            seen.add(sid)
            bikes = int(r["parkingBikeTotCnt"])
            if bikes < 0:
                raise RuntimeError("음수 재고 응답입니다.")
            rows.append(
                {
                    "station_id": sid,
                    "station_number": str(int(m.group(1))),
                    "station_name": r["stationName"],
                    "current_bikes": bikes,
                    "rack_count": int(r["rackTotCnt"]),
                    "latitude": float(r["stationLatitude"]),
                    "longitude": float(r["stationLongitude"]),
                    "snapshot_at": observed,
                    "timestamp_source": "client_response_received",
                }
            )
        if sample or len(batch) < 1000:
            break
    else:
        raise RuntimeError("20페이지 상한 도달. 전체 수집 완전성을 확인해야 합니다.")
    return {
        "mode": "sample" if sample else "authenticated",
        "partial": sample,
        "station_count": len(rows),
        "collection_started_at": started,
        "fetched_at": datetime.now(KST).isoformat(),
        "timestamp_note": "API가 관측시각을 제공하지 않아 응답 수신시각을 기록함. 원천 시스템 지연은 별도 확인 필요.",
        "rows": rows,
    }


def save_snapshot(snapshot, database=None):
    database = Path(database or INVENTORY_DATA_DIR / "inventory_history.sqlite")
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=30)) as con, con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS snapshots (station_id TEXT, station_number TEXT, snapshot_at TEXT, bikes INTEGER, mode TEXT, name TEXT, PRIMARY KEY(station_id,snapshot_at))"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS snapshots_mode_time ON snapshots(mode,snapshot_at)"
        )
        con.executemany(
            "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?)",
            [
                (
                    r["station_id"],
                    r["station_number"],
                    r["snapshot_at"],
                    r["current_bikes"],
                    snapshot["mode"],
                    r["station_name"],
                )
                for r in snapshot["rows"]
            ],
        )
    atomic_json(database.parent / "inventory_latest.json", snapshot)
    if snapshot["mode"] == "authenticated" and (not snapshot["partial"]):
        atomic_json(database.parent / "inventory_latest_full.json", snapshot)


def collect(
    sample=False,
    interval=60,
    iterations=0,
    duration_minutes=0,
    database=None,
    fetcher=None,
):
    """Append observations; retry failed cycles; never refit a model while collecting."""
    if interval < 30 or iterations < 0 or duration_minutes < 0:
        raise ValueError("간격은 30초 이상, 횟수와 시간은 0 이상이어야 합니다.")
    database = Path(database or INVENTORY_DATA_DIR / "inventory_history.sqlite")
    directory = database.parent
    fetcher = fetcher or fetch_snapshot
    with collector_lock(directory):
        stop = directory / "collector.stop"
        stop.unlink(missing_ok=True)
        started = time.monotonic()
        deadline = started + duration_minutes * 60 if duration_minutes else float("inf")
        state = {
            "pid": os.getpid(),
            "status": "running",
            "started_at": datetime.now(KST).isoformat(),
            "interval_seconds": interval,
            "duration_minutes": duration_minutes,
            "attempts": 0,
            "successful_cycles": 0,
            "failed_cycles": 0,
            "consecutive_failures": 0,
            "last_success_at": None,
            "last_station_count": None,
        }
        status_file = directory / "collector_status.json"

        def report(event):
            state["updated_at"] = datetime.now(KST).isoformat()
            state["elapsed_seconds"] = round(time.monotonic() - started, 2)
            atomic_json(status_file, state)
            record = dict(state, event=event)
            with (directory / "collector_events.jsonl").open(
                "a", encoding="utf-8"
            ) as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(json.dumps(record, ensure_ascii=False), flush=True)

        report("started")
        try:
            while not stop.exists() and time.monotonic() < deadline:
                cycle_started = time.monotonic()
                state["attempts"] += 1
                try:
                    snapshot = fetcher(sample=sample)
                    save_snapshot(snapshot, database)
                    state.update(
                        successful_cycles=state["successful_cycles"] + 1,
                        consecutive_failures=0,
                        last_success_at=snapshot["fetched_at"],
                        last_station_count=snapshot["station_count"],
                    )
                    state.pop("last_error", None)
                    report("snapshot_saved")
                    wait = max(0, interval - (time.monotonic() - cycle_started))
                except Exception as error:
                    state["failed_cycles"] += 1
                    state["consecutive_failures"] += 1
                    state["last_error"] = (
                        type(error).__name__ + "; 수집 실패, 자동 재시도 예정"
                    )
                    wait = min(300, 5 * 2 ** min(state["consecutive_failures"] - 1, 6))
                    report("retry_scheduled")
                if iterations and state["attempts"] >= iterations:
                    break
                wake = min(time.monotonic() + wait, deadline)
                while time.monotonic() < wake and (not stop.exists()):
                    time.sleep(max(0, min(1, wake - time.monotonic())))
        except KeyboardInterrupt:
            state["stop_reason"] = "keyboard_interrupt"
        finally:
            state["status"] = "stopped"
            state["stopped_at"] = datetime.now(KST).isoformat()
            state.setdefault(
                "stop_reason", "requested" if stop.exists() else "run_limit"
            )
            report("stopped")
    return state
