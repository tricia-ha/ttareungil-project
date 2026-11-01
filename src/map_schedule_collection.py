"""Collect public station timetable responses separately from route queries."""

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import time

import requests

BASE = "https://pts.map.naver.com"


class CollectionStopped(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if path.suffix == ".gz":
        with gzip.open(part, "wt", encoding="utf8") as stream:
            stream.write(text)
    else:
        part.write_text(text, encoding="utf8")
    part.replace(path)


def read_json(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf8"))


def validate_schedule(value, station_id):
    if not isinstance(value, dict) or str(value.get("stopId")) != str(station_id):
        raise ValueError("Timetable station ID mismatch")
    calendars = value.get("calendars")
    schedules = value.get("schedules")
    if (
        not isinstance(calendars, list)
        or not calendars
        or not isinstance(schedules, list)
    ):
        raise ValueError("Unexpected timetable schema")
    ids = {c["id"] for c in calendars}
    if len(ids) != len(calendars):
        raise ValueError("Duplicate service calendars")
    for schedule in schedules:
        if schedule.get("calendarId") not in ids:
            raise ValueError("Unknown service calendar")
        for direction in ("up", "down"):
            if not isinstance(schedule.get(direction), list):
                raise ValueError("Missing timetable direction")
    return value


def collect(
    folder,
    max_requests=50,
    interval=1.0,
    refresh=False,
    session=None,
    progress=print,
    max_age_days=7,
):
    """Checkpoint every response; no parallel requests, retries or access workarounds."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "collection.json"
    state = read_json(state_path) if state_path.exists() else {"stations": {}}
    state.update(source="naver_public_station_timetable", source_base=BASE)
    if refresh and (state.get("complete") or "refresh_since" not in state):
        state.update(refresh_since=time.time(), complete=False)
        atomic_json(state_path, state)
    refresh_since = state.get("refresh_since")
    client = session or requests.Session()
    calls = 0
    last_call = 0.0

    def fresh(path):
        return (
            path.exists()
            and time.time() - path.stat().st_mtime < max_age_days * 86400
            and (refresh_since is None or path.stat().st_mtime >= refresh_since)
        )

    def fetch(url):
        nonlocal calls, last_call
        if calls >= max_requests:
            raise CollectionStopped("request_budget")
        wait = interval - (time.monotonic() - last_call)
        if wait > 0:
            time.sleep(wait)
        calls += 1
        last_call = time.monotonic()
        response = client.get(url, timeout=(5, 25))
        if response.status_code in (401, 403, 429):
            raise CollectionStopped("http_" + str(response.status_code))
        response.raise_for_status()
        if "json" not in response.headers.get("content-type", "").lower():
            raise CollectionStopped("unexpected_response_type")
        return response.json()

    try:
        catalog_path = folder / "lanes.json.gz"
        if not fresh(catalog_path):
            lanes = fetch(BASE + "/api/subway/lanes/stations")
            if not isinstance(lanes, list):
                raise ValueError("Unexpected route catalog")
            atomic_json(catalog_path, lanes)
        lanes = read_json(catalog_path)
        selected = [r for r in lanes if 201 <= int(r["type"]["id"]) <= 224]
        stations = {str(s["id"]): s for r in selected for s in r["stations"]}
        priority = {"1907", "1922", "11321", "11333", "11711", "11721"}
        targets = sorted(stations, key=lambda k: (k not in priority, int(k)))
        state["expected_stations"] = len(targets)
        for station_id in targets:
            path = folder / "stops" / (station_id + ".json.gz")
            cached = False
            if fresh(path):
                try:
                    validate_schedule(read_json(path), station_id)
                    cached = True
                except (ValueError, OSError, EOFError):
                    pass  # Preserve a bad cache until a validated replacement is ready.
            if cached:
                state["stations"].setdefault(
                    station_id,
                    {
                        "name": stations[station_id]["name"],
                        "restored_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                continue
            url = (
                BASE
                + f"/api/subway/schedules/stops/{station_id}?lang=ko&caller=pc_search"
            )
            data = validate_schedule(fetch(url), station_id)
            atomic_json(path, data)
            state["stations"][station_id] = {
                "name": stations[station_id]["name"],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "url": url,
            }
            state["complete"] = False
            atomic_json(state_path, state)
            if progress:
                progress(
                    f"시간표 {len(state['stations'])}/{len(targets)}: {stations[station_id]['name']}",
                    flush=True,
                )
        state["complete"] = True
        state.pop("stopped", None)
        state.pop("refresh_since", None)
    except (CollectionStopped, requests.RequestException, ValueError, KeyError) as exc:
        state["complete"] = False
        state["stopped"] = (
            str(exc) if isinstance(exc, CollectionStopped) else type(exc).__name__
        )
        if progress:
            progress(
                "수집 중단: " + state["stopped"] + "; 저장된 응답은 유지됩니다.",
                flush=True,
            )
    finally:
        state["requests_this_run"] = calls
        atomic_json(state_path, state)
        if session is None:
            client.close()
    return state
