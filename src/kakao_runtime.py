"""One-project entry point: V5.2.1 ETA + final inventory model + Kakao/KMA."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import math
import threading
from pathlib import Path

from kakao_routes import KakaoClient, point
from kakao_optimizer import KakaoConfig, build_network, search, save_json, map_html
from multimodal import V521BikeProvider, timestamp
from multimodal_cache import CachedBikeProvider
from project_paths import (
    PROJECT_ROOT,
    ETA_MODEL_DIR,
    INVENTORY_MODEL_DIR,
    INVENTORY_SNAPSHOT_DIR as INVENTORY_DATA_DIR,
    RUNTIME_CACHE_DIR,
    RUNTIME_OUTPUT_DIR,
    STATION_CATALOG,
    STATION_CROSSWALK,
    load_project_env,
)

ROOT = PROJECT_ROOT


def eligible_station_catalog(stations, mapping, inventory, snapshot):
    """Return live stations supported by both production models."""
    eligible = []
    counts = dict(
        catalog=len(stations),
        missing_mapping=0,
        missing_live=0,
        unsupported_inventory=0,
        eligible=0,
        inventory_filter_applied=True,
    )
    for row in stations:
        eta_id = str(row.get("station_id", "")).strip()
        if eta_id.endswith(".0"):
            eta_id = eta_id[:-2]
        if eta_id.isdigit():
            eta_id = str(int(eta_id))
        inv_id = mapping.get(eta_id)
        if not inv_id:
            counts["missing_mapping"] += 1
            continue
        live = snapshot.get(inv_id)
        if live is None:
            counts["missing_live"] += 1
            continue
        if inv_id not in inventory.station_ids:
            counts["unsupported_inventory"] += 1
            continue
        merged = dict(row)
        merged.update(
            station_id=eta_id,
            station_name=live.get("station_name") or row.get("station_name"),
            latitude=live["latitude"],
            longitude=live["longitude"],
            active=True,
            inventory_station_id=inv_id,
            current_bikes=live["current_bikes"],
        )
        eligible.append(merged)
    counts["eligible"] = len(eligible)
    if not eligible:
        raise ValueError(
            "현재 운영·이동시간·재고 모델을 모두 만족하는 대여소가 없습니다."
        )
    return eligible, counts


def load_crosswalk(path=STATION_CROSSWALK):
    """Use verified IDs; never guess numeric station IDs from coordinates."""
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    mapping = {}
    inverse = {}
    for row in rows:
        status = row.get("mapping_status", "")
        if status not in ("api_verified", "api_master_verified", "provided_verified"):
            continue
        eta_id = str(
            row.get("eta_station_id") or row.get("station_number") or ""
        ).strip()
        inv_id = str(
            row.get("inv_station_id")
            or row.get("inventory_station_id")
            or row.get("station_id")
            or ""
        ).strip()
        if eta_id.isdigit():
            eta_id = str(int(eta_id))
        if not eta_id or not inv_id.startswith("ST-"):
            continue
        if eta_id in mapping and mapping[eta_id] != inv_id:
            raise ValueError("동일 ETA ID에 여러 재고 ID가 있습니다.")
        if inv_id in inverse and inverse[inv_id] != eta_id:
            raise ValueError("동일 재고 ID에 여러 ETA ID가 있습니다.")
        mapping[eta_id] = inv_id
        inverse[inv_id] = eta_id
    if not mapping:
        raise ValueError("확인된 대여소 ID 매핑이 없습니다.")
    return mapping


def validate_snapshot(payload, as_of, max_age_seconds=600):
    if payload.get("partial", True) or payload.get("mode") != "authenticated":
        raise ValueError(
            "인증된 전체 재고 응답이 필요합니다. sample 자료는 사용할 수 없습니다."
        )
    rows = payload.get("rows", [])
    if not rows or payload.get("station_count") != len(rows):
        raise ValueError("재고 응답 건수가 맞지 않습니다.")
    seen = set()
    for row in rows:
        sid = row["station_id"]
        if sid in seen:
            raise ValueError("재고 응답에 중복 대여소가 있습니다.")
        seen.add(sid)
        if (
            not isinstance(row["current_bikes"], int)
            or isinstance(row["current_bikes"], bool)
            or not 0 <= row["current_bikes"] <= 500
        ):
            raise ValueError("재고는 0~500의 정수여야 합니다.")
        age = timestamp(as_of) - timestamp(row["snapshot_at"])
        if not 0 <= age <= max_age_seconds:
            raise ValueError(
                "10분 이내 최신 재고가 필요합니다. 재고 새로 조회를 사용하세요."
            )
    return {r["station_id"]: r for r in rows}


_inventory_lock = threading.Lock()


def prepare_inventory(inventory_path=None, refresh=True, progress=print):
    with _inventory_lock:
        return _prepare_inventory(inventory_path, refresh, progress)


def _prepare_inventory(inventory_path=None, refresh=True, progress=print):
    from inventory_credentials import get_api_key
    from inventory_live import fetch_snapshot, save_snapshot

    path = (
        Path(inventory_path)
        if inventory_path
        else INVENTORY_DATA_DIR / "inventory_latest_full.json"
    )
    now = datetime.now(timezone.utc).isoformat()
    if path.is_file():
        with path.open(encoding="utf-8-sig") as stream:
            payload = json.load(stream)
        try:
            rows = validate_snapshot(
                payload, now, max_age_seconds=120 if refresh else 600
            )
            if progress:
                progress("저장된 최신 전체 재고 재사용", flush=True)
            return payload, rows
        except (ValueError, KeyError):
            if not refresh:
                raise
    if not refresh:
        raise ValueError(
            "재고 파일이 없습니다. refresh_inventory=True 또는 최신 파일을 지정하세요."
        )
    if not get_api_key():
        raise ValueError(
            "현재 재고 조회에는 서울 열린데이터광장 SEOUL_API_KEY가 필요합니다."
        )
    if progress:
        progress("서울 API에서 현재 전체 따릉이 재고 조회", flush=True)
    payload = fetch_snapshot(sample=False)
    rows = validate_snapshot(payload, datetime.now(timezone.utc).isoformat())
    save_snapshot(payload)
    if path != INVENTORY_DATA_DIR / "inventory_latest_full.json":
        save_json(path, payload)
    return payload, rows


def run_optimization(
    origin,
    destination,
    departure,
    api_key=None,
    config=None,
    max_api_calls=40,
    daily_limit=1000,
    use_kma=True,
    user_factor=1.0,
    bike_type="regular",
    user_type="domestic",
    eta_service=None,
    output_dir=None,
    inventory_path=None,
    crosswalk_path=None,
    use_inventory=True,
    refresh_inventory=True,
    progress=print,
):
    load_project_env()
    timestamp(departure)
    c = config or KakaoConfig(
        require_known_inventory=use_inventory,
        max_empty_probability=0.1 if use_inventory else None,
    )
    c.validate()
    origin, destination = point(origin), point(destination)
    if not math.isfinite(user_factor) or not 0.7 <= user_factor <= 1.5:
        raise ValueError("개인계수는 0.7~1.5입니다.")
    if c.require_known_inventory and not use_inventory:
        raise ValueError(
            "재고 확인 필수와 재고 사용 안 함을 동시에 설정할 수 없습니다."
        )
    if use_kma:
        import os

        if not (os.getenv("KMA_API_KEY") or os.getenv("KMA_AUTH_KEY")):
            raise ValueError("기상청 예보를 사용하려면 KMA_API_KEY를 설정하세요.")
    output = Path(output_dir) if output_dir else RUNTIME_OUTPUT_DIR / "kakao"
    output.mkdir(parents=True, exist_ok=True)
    with STATION_CATALOG.open(encoding="utf-8-sig", newline="") as stream:
        stations = list(csv.DictReader(stream))
    snapshot, inventory, as_of, mapping = {}, None, None, {}
    if use_inventory:
        from inventory_service import InventoryPredictor

        mapping = load_crosswalk(crosswalk_path or STATION_CROSSWALK)
        inventory = InventoryPredictor()
        # Validate credentials or a supplied snapshot before spending Kakao calls.
        payload, snapshot = prepare_inventory(
            inventory_path, refresh_inventory, progress
        )
        stations, station_coverage = eligible_station_catalog(
            stations, mapping, inventory, snapshot
        )
    else:
        station_coverage = dict(
            catalog=len(stations),
            eligible=len(stations),
            inventory_filter_applied=False,
        )
    client = KakaoClient(
        RUNTIME_CACHE_DIR / "kakao_cache.sqlite",
        api_key,
        max_api_calls,
        daily_limit,
        progress=progress,
    )
    owned_service = None
    cached = None
    try:
        network = build_network(
            client, stations, origin, destination, c, progress, output / "network.json"
        )
        network["metadata"]["station_coverage"] = station_coverage
        save_json(output / "network.json", network)
        api_stats = client.stats()
        if any(e["mode"] == "bicycle" for e in network["edges"]):
            if eta_service is None:
                if progress:
                    progress("V5.2.1 모델·OSM 도로망 불러오기", flush=True)
                from eta_service import BicycleETAService

                owned_service = BicycleETAService()
                eta_service = owned_service
            if use_inventory:
                # Long network/model preparation must not consume snapshot freshness.
                payload, snapshot = prepare_inventory(
                    inventory_path, refresh_inventory, progress
                )
                as_of = datetime.now(timezone.utc).isoformat()
                validate_snapshot(payload, as_of)
                for edge in network["edges"]:
                    if edge["mode"] == "bicycle":
                        edge["inventory_from"] = mapping.get(edge["eta_from"])
                network["metadata"]["inventory_model"] = inventory.metadata
                network["metadata"]["inventory_snapshot_at"] = payload["fetched_at"]
                network["metadata"]["inventory_policy"] = dict(
                    require_known=c.require_known_inventory,
                    max_empty_probability=c.max_empty_probability,
                    probability_calibrated=False,
                )
                save_json(output / "network.json", network)
            provider = V521BikeProvider(
                eta_service,
                inventory,
                snapshot,
                as_of,
                user_factor,
                use_kma,
                bike_type,
                user_type,
                eta_quantile=c.bike_quantile,
            )
            artifacts = []
            for folder in (ETA_MODEL_DIR, INVENTORY_MODEL_DIR):
                for path in sorted(folder.rglob("*")):
                    if path.is_file():
                        stat = path.stat()
                        artifacts.append(
                            [
                                str(path.relative_to(ROOT)),
                                stat.st_size,
                                stat.st_mtime_ns,
                            ]
                        )
            from scheduled_runtime import forecast_issue_key, runtime_code_identity

            cached = CachedBikeProvider(
                provider,
                RUNTIME_CACHE_DIR / "kakao_bike_cache.sqlite",
                dict(
                    version=5,
                    code=runtime_code_identity(),
                    artifacts=artifacts,
                    use_kma=use_kma,
                    user_factor=user_factor,
                    bike_type=bike_type,
                    user_type=user_type,
                    forecast_issue=forecast_issue_key() if use_kma else None,
                ),
                departure_bucket_minutes=5,
            )
            bike_provider = cached
        else:

            def bike_provider(edge, at):
                raise ValueError("따릉이 후보가 없습니다.")

        result = search(network, bike_provider, departure, c, progress)
        result["api_usage"] = api_stats
        result["station_catalog_as_of"] = sorted(
            {r.get("catalog_as_of", "unknown") for r in stations}
        )
        result["inventory_connected"] = inventory is not None
        result["weather_requested"] = use_kma
        result["model_versions"] = dict(
            eta="v5.2.1",
            inventory=inventory.metadata.get("selected_model") if inventory else None,
        )
        result["bicycle_checkpoint"] = (
            dict(hits=cached.hits, misses=cached.misses) if cached else None
        )
        save_json(output / "result.json", result)
        (output / "routes.html").write_text(map_html(result), encoding="utf-8")
        if progress:
            progress(
                f"완료: {result['status']} / 경로 {len(result['routes'])}개 / 새 카카오 호출 {api_stats['new_api_calls']}회",
                flush=True,
            )
            progress(f"결과: {output}", flush=True)
        return result
    finally:
        if cached is not None:
            cached.close()
        if owned_service is not None:
            owned_service.close()
        client.close()
