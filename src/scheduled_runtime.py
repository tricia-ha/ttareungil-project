"""Future-departure optimizer using dated GTFS, Kakao walking, V5.2.1 and inventory."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
import csv
import json
import os
import copy
from pathlib import Path

from kakao_routes import (
    KakaoClient,
    CallLimit,
    KakaoError,
    point,
    walking_edge,
    record_walking_error,
)
from kakao_runtime import (
    eligible_station_catalog,
    load_crosswalk,
    prepare_inventory,
    validate_snapshot,
)
from multimodal import Config, Network, Router, V521BikeProvider, timestamp
from multimodal_cache import CachedBikeProvider
from multimodal_data import distance, load_gtfs
from transit_schedule import build_timetable
from project_paths import (
    ETA_MODEL_DIR,
    INVENTORY_MODEL_DIR,
    PROJECT_ROOT,
    RUNTIME_CACHE_DIR,
    RUNTIME_OUTPUT_DIR,
    STATION_CATALOG,
    STATION_CROSSWALK,
    load_project_env,
)


@dataclass
class ScheduledConfig:
    horizon_minutes: int = 180
    top_k: int = 3
    max_seconds: int = 30
    step_seconds: int = 60
    max_expansions: int = 100000
    max_station_candidates: int = (
        16  # Legacy comparison only; ignored by complete_candidates=True.
    )
    transfer_stop_candidates: int = 80
    endpoint_stop_neighbors: int = 4
    endpoint_station_neighbors: int = 4
    station_stop_neighbors: int = 1
    transfer_stop_neighbors: int = 2
    max_transfer_walks: int = 16
    bike_neighbors: int = (
        4  # Legacy comparison only; not a limit in the default search.
    )
    max_connector_distance_m: float = 900
    max_bike_distance_m: float = 7000
    max_kakao_calls: int = 80
    kakao_daily_limit: int = 1000
    require_known_inventory: bool = True
    max_empty_probability: float | None = 0.1
    max_bike_predictions: int | None = None
    search_algorithm: str = "astar"
    bike_quantile: str = "p50"
    complete_candidates: bool = True
    save_network: bool = False
    bike_expansion_batch_size: int = 2
    reuse_transit_suffixes: bool = True
    search_mode: str = "integrated"
    bicycle_batch_size: int = 16
    bicycle_available: bool = True

    def validate(self):
        if not isinstance(self.bicycle_available, bool):
            raise ValueError("bicycle_available는 bool이어야 합니다.")
        if not isinstance(self.bicycle_batch_size, int) or self.bicycle_batch_size <= 0:
            raise ValueError("bicycle_batch_size는 양의 정수여야 합니다.")
        if self.search_mode not in ("integrated", "transit", "subway", "bicycle"):
            raise ValueError("search_mode는 integrated/transit/subway/bicycle입니다.")
        if self.bike_quantile not in ("p50", "p80", "p90"):
            raise ValueError("bike_quantile은 p50/p80/p90입니다.")
        if (
            not isinstance(self.bike_expansion_batch_size, int)
            or self.bike_expansion_batch_size <= 0
        ):
            raise ValueError("bike_expansion_batch_size는 양의 정수여야 합니다.")
        positive = (
            self.horizon_minutes,
            self.top_k,
            self.max_seconds,
            self.step_seconds,
            self.max_expansions,
            self.max_station_candidates,
            self.transfer_stop_candidates,
            self.endpoint_stop_neighbors,
            self.endpoint_station_neighbors,
            self.station_stop_neighbors,
            self.bike_neighbors,
            self.transfer_stop_neighbors,
            self.max_transfer_walks,
            self.max_connector_distance_m,
            self.max_bike_distance_m,
            self.max_kakao_calls,
            self.kakao_daily_limit,
        )
        if any(float(v) <= 0 for v in positive):
            raise ValueError("시간표 최적화 설정값은 양수여야 합니다.")
        if (
            self.max_empty_probability is not None
            and not 0 <= self.max_empty_probability <= 1
        ):
            raise ValueError("재고 위험 한도는 0~1이어야 합니다.")
        if self.max_bike_predictions is not None and self.max_bike_predictions <= 0:
            raise ValueError("새 예측 한도는 None 또는 양수여야 합니다.")
        if self.search_algorithm not in ("astar", "dijkstra"):
            raise ValueError("search_algorithm은 astar/dijkstra입니다.")


def _nearest(nodes, target, limit, radius):
    ranked = sorted(
        ((distance(value, target), key) for key, value in nodes.items()),
        key=lambda row: (row[0], row[1]),
    )
    return [key for d, key in ranked if d <= radius][:limit]


def select_station_candidates(stations, stop_nodes, origin, destination, limit=16):
    """Select a bounded set near endpoints and useful timetable stops."""
    if limit <= 0:
        raise ValueError("대여소 후보 수는 양수여야 합니다.")
    scored = []
    for row in stations:
        station = dict(
            latitude=float(row["latitude"]), longitude=float(row["longitude"])
        )
        endpoint = min(distance(station, origin), distance(station, destination))
        stop = min(
            (distance(station, s) for s in stop_nodes.values()), default=float("inf")
        )
        # Endpoint access is primary; a nearby timetable stop rewards transfers.
        score = min(endpoint, stop + 250)
        scored.append((score, endpoint, stop, str(row["station_id"]), row))
    scored.sort(key=lambda x: x[:4])
    return [row for *_, row in scored[:limit]]


class KakaoWalking:
    def __init__(self, client, link_cache=None):
        self.client, self.link_cache = client, link_cache

    def set_deadline(self, deadline):
        self.client.deadline = deadline

    def __call__(self, a, b, start, end):
        payload, fetched = self.client.get("walk", a, b)
        edge = walking_edge(payload, fetched, start, end)
        if edge is not None and self.link_cache is not None:
            self.link_cache.remember(start, end, a, b)
        return edge

    def cached(self, a, b, start, end):
        """Return (cache hit, edge); a cached no-route is also a hit."""
        lookup = getattr(self.client, "get_cached", None)
        if lookup is None:
            return False, None
        payload = lookup("walk", a, b)
        if payload is None:
            return False, None
        edge = walking_edge(*payload, start, end)
        if edge is not None and self.link_cache is not None:
            self.link_cache.remember(start, end, a, b)
        return True, edge


def _seed_endpoint_walks(
    nodes, stops, stations, walking, config, walks, diagnostics, transit, departure=None
):
    """Restore all cached access edges; reserve calls for reached-node work.

    Alternate origin/destination and bike/transit groups. This is a query order,
    not candidate pruning: the lazy graph retains every radius-valid edge.
    """
    from itertools import zip_longest
    from candidate_edges import DeadEndGuard

    guard = DeadEndGuard(
        Network(
            nodes=nodes,
            walks=walks,
            transit=transit,
            bike_policy={"available": True}
            if config.bicycle_available and stations
            else {},
            walk_policy={"max_distance_m": config.max_connector_distance_m},
        )
    )

    groups = []
    rail = {key: value for key, value in stops.items() if key.startswith("subway:")}
    other_stops = {key: value for key, value in stops.items() if key not in rail}
    for group in (stations, rail, other_stops):
        if not group:
            continue
        for endpoint, backwards in (("origin", False), ("destination", True)):
            groups.append(
                [
                    (key, endpoint) if backwards else (endpoint, key)
                    for key in _nearest(
                        group,
                        nodes[endpoint],
                        len(group),
                        config.max_connector_distance_m,
                    )
                ]
            )
    ordered = [pair for rank in zip_longest(*groups) for pair in rank if pair]
    resolved = {(e["from"], e["to"]) for e in walks}
    cached_lookup = getattr(walking, "cached", None)
    cached_count = 0
    cached_no_route = set()
    for a, b in ordered:
        if (a, b) in resolved:
            continue
        if distance(nodes[a], nodes[b]) < 3:
            hit, edge = (
                True,
                dict(
                    **{"from": a, "to": b},
                    minutes=0.05,
                    source="coincident_access_estimate",
                    geometry=None,
                ),
            )
        elif cached_lookup is not None:
            try:
                hit, edge = cached_lookup(nodes[a], nodes[b], a, b)
            except KakaoError as exc:
                record_walking_error(diagnostics, exc, a, b)
                hit, edge = False, None
        else:
            hit, edge = False, None
        if hit:
            resolved.add((a, b))
            if edge is not None:
                walks.append(edge)
                cached_count += 1
            else:
                cached_no_route.add((a, b))
    client = getattr(walking, "client", None)
    if client is not None and not all(
        hasattr(client, name) for name in ("calls", "max_calls")
    ):
        client = None
    remaining = max(0, client.max_calls - client.calls) if client is not None else None
    allowance = (
        max(1, remaining // 3, min(len(groups), remaining // 2)) if remaining else 0
    )
    if remaining is None:
        allowance = 4 * len(groups)
    before = client.calls if client is not None else 0
    attempts = 0
    blocked = False
    from endpoint_priority import EndpointPriority

    diagnostics["endpoint_priority"] = "geometry_and_scheduled_travel_time"
    diagnostics["probability_pruning"] = False
    diagnostics["endpoint_candidate_count"] = sum(map(len, groups))
    if guard.is_dead("origin"):
        diagnostics["endpoint_origin_unreachable"] = True
        return False, cached_count, 0
    if allowance <= 0 or not any(pair not in resolved for group in groups for pair in group):
        return False, cached_count, 0
    ranked_groups = [[pair for pair in group if pair not in cached_no_route] for group in groups]
    priority = EndpointPriority(nodes, transit, walks, ranked_groups, config.max_bike_distance_m, departure)
    for pair in priority.ordered(ranked_groups):
        if pair in resolved:
            continue
        spent = client.calls - before if client is not None else attempts
        if spent >= allowance:
            break
        a, b = pair
        if not config.bicycle_available and (
            nodes[a].get("kind") == "bike" or nodes[b].get("kind") == "bike"
        ):
            diagnostics["skipped_unavailable_bicycle_seeds"] = (
                diagnostics.get("skipped_unavailable_bicycle_seeds", 0) + 1
            )
            continue
        if guard.is_dead(b):
            diagnostics["skipped_dead_end_walk_candidates"] = (
                diagnostics.get("skipped_dead_end_walk_candidates", 0) + 1
            )
            continue
        try:
            attempts += 1
            edge = walking(nodes[a], nodes[b], a, b)
        except CallLimit:
            blocked = True
            break
        except KakaoError as exc:
            record_walking_error(diagnostics, exc, a, b)
            continue
        resolved.add(pair)
        if edge is not None:
            walks.append(edge)
    return (
        blocked,
        cached_count,
        (client.calls - before if client is not None else attempts),
    )


def build_scheduled_network(timetable, stations, origin, destination, walking, config, departure=None):
    """Add only bounded access/transfer connectors; never call Kakao for every pair."""
    config.validate()
    departure = departure or timetable.metadata.get("valid_departure")
    walking_diagnostics = {}
    if config.search_mode != "integrated":
        edges = [
            e
            for e in timetable.transit
            if (config.search_mode == "subway" and e["mode"] == "subway")
            or (config.search_mode == "transit" and e["mode"] in ("bus", "subway"))
        ]
        ids = {e[k] for e in edges for k in ("from", "to")}
        timetable = Network(
            nodes={k: v for k, v in timetable.nodes.items() if k in ids},
            transit=edges,
            metadata=dict(timetable.metadata),
        )
        if config.search_mode in ("subway", "transit"):
            stations = []
    origin_access = destination_access = None
    if config.search_mode == "bicycle":
        # With no transit stops and no station-to-station walking, a valid
        # cycling-only journey rents near origin and returns near destination.
        origin_access = {
            str(r["station_id"])
            for r in stations
            if distance(
                dict(latitude=float(r["latitude"]), longitude=float(r["longitude"])),
                origin,
            )
            <= config.max_connector_distance_m
        }
        destination_access = {
            str(r["station_id"])
            for r in stations
            if distance(
                dict(latitude=float(r["latitude"]), longitude=float(r["longitude"])),
                destination,
            )
            <= config.max_connector_distance_m
        }
        stations = [
            r
            for r in stations
            if str(r["station_id"]) in origin_access | destination_access
        ]
    nodes = dict(timetable.nodes)
    nodes["origin"] = dict(origin, kind="endpoint")
    nodes["destination"] = dict(destination, kind="endpoint")
    active_stop_ids = {e["from"] for e in timetable.transit} | {
        e["to"] for e in timetable.transit
    }
    stops = {key: nodes[key] for key in active_stop_ids if key in nodes}
    direct = distance(origin, destination)
    corridor = sorted(
        stops,
        key=lambda key: (
            distance(origin, stops[key]) + distance(stops[key], destination) - direct,
            key,
        ),
    )
    useful_stops = {
        key: stops[key] for key in corridor[: config.transfer_stop_candidates]
    }
    # Reserve endpoint candidates before adding stations useful for transfers.
    selected = list(stations) if config.complete_candidates else []
    selected_ids = {str(row["station_id"]) for row in selected}
    for target in () if config.complete_candidates else (origin, destination):
        for row in sorted(
            stations,
            key=lambda r: distance(
                dict(latitude=float(r["latitude"]), longitude=float(r["longitude"])),
                target,
            ),
        )[: config.endpoint_station_neighbors]:
            if (
                str(row["station_id"]) not in selected_ids
                and len(selected) < config.max_station_candidates
            ):
                selected.append(row)
                selected_ids.add(str(row["station_id"]))
    for row in (
        select_station_candidates(
            stations, useful_stops, origin, destination, len(stations)
        )
        if stations and not config.complete_candidates
        else []
    ):
        if len(selected) >= config.max_station_candidates:
            break
        if str(row["station_id"]) not in selected_ids:
            selected.append(row)
            selected_ids.add(str(row["station_id"]))
    for row in selected:
        key = "bike:" + str(row["station_id"])
        nodes[key] = dict(
            row,
            kind="bike",
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
        )

    station_nodes = {k: v for k, v in nodes.items() if v.get("kind") == "bike"}
    if config.search_mode == "bicycle" and not config.bicycle_available:
        return Network(
            nodes=nodes,
            metadata=dict(
                timetable.metadata,
                search_mode=config.search_mode,
                bicycle_available=False,
                bicycle_feasibility="outside_inventory_prediction_window",
                walking_calls_avoided=True,
            ),
        )
    if config.search_mode == "bicycle":
        # This mode cannot chain rentals without station-to-station walking.
        # If no allowed rental/return pair exists, access API calls cannot help.
        from candidate_edges import SpatialIndex

        returns = {
            key: value
            for key, value in station_nodes.items()
            if str(value["station_id"]) in destination_access
        }
        return_index = SpatialIndex(returns)
        feasible = any(
            end != key
            for key, value in station_nodes.items()
            if str(value["station_id"]) in origin_access
            for _, end in return_index.near(value, config.max_bike_distance_m)
        )
        if not feasible:
            return Network(
                nodes=nodes,
                metadata=dict(
                    timetable.metadata,
                    search_mode=config.search_mode,
                    station_candidates=len(selected),
                    candidate_graph_approximate=False,
                    bicycle_feasibility="no_station_pair_within_configured_distance",
                    max_bike_distance_m=config.max_bike_distance_m,
                    walking_calls_avoided=True,
                ),
            )
    if config.complete_candidates:
        # Every supported station and directed OD remains available within the
        # configured radii. APIs/ETAs are evaluated only at reached nodes.
        from extended_rail import name as rail_station_name
        from collections import defaultdict

        complexes = defaultdict(list)
        for key, value in stops.items():
            if key.startswith("subway:"):
                complexes[rail_station_name(value.get("name", ""))].append(key)
        transfers = []
        for name, platforms in complexes.items():
            if not name:
                continue
            for a in platforms:
                for b in platforms:
                    separation = distance(nodes[a], nodes[b])
                    if a != b and separation <= 700:
                        transfers.append(
                            {
                                "from": a,
                                "to": b,
                                "minutes": 3.0 + separation / 80.0,
                                "source": "rail_interchange_estimate",
                                "geometry": None,
                            }
                        )
        connector_limit, cached_seeds, paid_seeds = _seed_endpoint_walks(
            nodes,
            stops,
            station_nodes,
            walking,
            config,
            transfers,
            walking_diagnostics,
            timetable.transit,
            departure=departure,
        )
        bike_policy = (
            {"max_distance_m": config.max_bike_distance_m}
            if config.bicycle_available
            and config.search_mode not in ("subway", "transit")
            else {}
        )
        if config.search_mode == "integrated" and bike_policy:
            bike_policy["prioritize_transfers"] = True
        if config.search_mode == "bicycle":
            bike_policy.update(
                origin_access_ids=sorted(origin_access),
                destination_access_ids=sorted(destination_access),
            )
        return Network(
            nodes=nodes,
            walks=transfers,
            transit=timetable.transit,
            bike_policy=bike_policy,
            walk_policy={"max_distance_m": config.max_connector_distance_m},
            metadata=dict(
                timetable.metadata,
                **walking_diagnostics,
                search_mode=config.search_mode,
                station_candidates=len(selected),
                candidate_graph_approximate=False,
                candidate_scope="all_supported_stations_within_configured_radii",
                connectors_lazy=True,
                connector_limit_reached=connector_limit,
                walking_query_policy="cached_first_balanced_endpoints_timetable_priority",
                cached_endpoint_connections=cached_seeds,
                new_endpoint_calls=paid_seeds,
                bicycle_available=config.bicycle_available,
                bike_od_pruning=False,
                station_top_n_pruning=False,
                transit_departures_are_scheduled=True,
                transit_realtime_delay_applied=False,
            ),
        )
    walks = []
    seen = set()
    connector_limit_reached = False

    def add(a, b):
        nonlocal connector_limit_reached
        if a == b or (a, b) in seen:
            return
        seen.add((a, b))
        if distance(nodes[a], nodes[b]) > config.max_connector_distance_m:
            return
        if distance(nodes[a], nodes[b]) < 3:
            # Distinct platforms can share a master coordinate; a zero-time API
            # walk would incorrectly make their interchange instantaneous.
            value = {
                "from": a,
                "to": b,
                "minutes": 2.0,
                "source": "station_transfer",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [nodes[a]["longitude"], nodes[a]["latitude"]],
                        [nodes[b]["longitude"], nodes[b]["latitude"]],
                    ],
                },
            }
        else:
            try:
                value = walking(nodes[a], nodes[b], a, b)
            except CallLimit:
                connector_limit_reached = True
                return
            except KakaoError as exc:
                record_walking_error(walking_diagnostics, exc, a, b)
                return
        if value is not None:
            walks.append(value)

    add("origin", "destination")
    for key in _nearest(
        stops, origin, config.endpoint_stop_neighbors, config.max_connector_distance_m
    ):
        add("origin", key)
    for key in _nearest(
        stops,
        destination,
        config.endpoint_stop_neighbors,
        config.max_connector_distance_m,
    ):
        add(key, "destination")
    for key in _nearest(
        station_nodes,
        origin,
        config.endpoint_station_neighbors,
        config.max_connector_distance_m,
    ):
        add("origin", key)
    for key in _nearest(
        station_nodes,
        destination,
        config.endpoint_station_neighbors,
        config.max_connector_distance_m,
    ):
        add(key, "destination")
    for station in station_nodes:
        for stop in _nearest(
            stops,
            nodes[station],
            config.station_stop_neighbors,
            config.max_connector_distance_m,
        ):
            add(station, stop)
            add(stop, station)

    # Transfers between distinct bus stops, lines, and stations must be explicit.
    # The bounded corridor avoids all-city pairwise walking API calls.
    transfer_pairs = []
    for stop in useful_stops:
        for other in _nearest(
            stops,
            nodes[stop],
            config.transfer_stop_neighbors + 1,
            config.max_connector_distance_m,
        ):
            if other != stop:
                transfer_pairs.append(
                    (distance(nodes[stop], nodes[other]), stop, other)
                )
    for _, a, b in sorted(transfer_pairs)[: config.max_transfer_walks]:
        add(a, b)

    # Rail interchange links are mandatory, not part of the bounded bus/walk
    # candidate budget. A new rail line must not become an isolated component.
    from extended_rail import name as rail_station_name
    from collections import defaultdict

    complexes = defaultdict(list)
    existing_walk_pairs = {(e["from"], e["to"]) for e in walks}
    for key, value in stops.items():
        if key.startswith("subway:"):
            complexes[rail_station_name(value.get("name", ""))].append(key)
    for station_name, platforms in complexes.items():
        if not station_name:
            continue
        for a in platforms:
            for b in platforms:
                if a == b or (a, b) in existing_walk_pairs:
                    continue
                # Distinct stations called 신촌, for example, are not automatically
                # treated as the same interchange complex.
                separation = distance(nodes[a], nodes[b])
                if separation > 700:
                    continue
                walks.append(
                    {
                        "from": a,
                        "to": b,
                        "minutes": 3.0 + separation / 80.0,
                        "source": "rail_interchange_estimate",
                        "geometry": None,
                    }
                )
                seen.add((a, b))
                existing_walk_pairs.add((a, b))

    bikes = []
    for start, a in station_nodes.items():
        if not config.bicycle_available:
            break
        if origin_access is not None and str(a["station_id"]) not in origin_access:
            continue
        ranked = sorted(
            (distance(a, b), end)
            for end, b in station_nodes.items()
            if end != start
            and (
                destination_access is None or str(b["station_id"]) in destination_access
            )
        )
        for d, end in [x for x in ranked if x[0] <= config.max_bike_distance_m][
            : config.bike_neighbors
        ]:
            bikes.append(
                {
                    "from": start,
                    "to": end,
                    "eta_from": str(a["station_id"]),
                    "eta_to": str(nodes[end]["station_id"]),
                    "inventory_from": a.get("inventory_station_id"),
                    "straight_distance_m": d,
                }
            )
    metadata = dict(
        timetable.metadata,
        **walking_diagnostics,
        search_mode=config.search_mode,
        walking_source="kakao_walk",
        station_candidates=len(selected),
        access_walk_edges=len(walks),
        bike_candidate_edges=len(bikes),
        candidate_graph_approximate=True,
        transit_departures_are_scheduled=True,
        transit_realtime_delay_applied=False,
    )
    metadata["connector_limit_reached"] = connector_limit_reached
    return Network(
        nodes=nodes,
        walks=walks,
        bikes=bikes,
        transit=timetable.transit,
        metadata=metadata,
    )


def _artifact_identity():
    rows = []
    for folder in (ETA_MODEL_DIR, INVENTORY_MODEL_DIR):
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                stat = path.stat()
                rows.append(
                    [
                        str(path.relative_to(PROJECT_ROOT)),
                        stat.st_size,
                        stat.st_mtime_ns,
                    ]
                )
    return rows


def forecast_issue_key(now=None):
    """Use the same publication delay and issue cycle as the weather provider."""
    from kma_weather_forecast_provider import KMAWeatherForecastProvider
    from types import SimpleNamespace

    return KMAWeatherForecastProvider._latest_issue(
        SimpleNamespace(publication_delay_minutes=20), now
    ).isoformat()


def runtime_code_identity():
    # A scheduler-only edit must not invalidate unchanged ETA predictions.
    from prediction_identity import prediction_code_identity

    return prediction_code_identity()


def _save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def run_scheduled_optimization(
    origin, destination, departure=None, gtfs_path=None, **options
):
    """Warm the model before choosing an automatic departure; always release it."""
    load_project_env()
    if gtfs_path is not None:
        options["gtfs_path"] = gtfs_path
    point(origin)
    point(destination)
    if options.get("config") is not None:
        options["config"].validate()
    transit_only = getattr(options.get("config"), "search_mode", "integrated") in (
        "subway",
        "transit",
    )
    if transit_only:
        options.update(use_kma=False, use_inventory=False, personalization_id=None)
    if (
        not transit_only
        and options.get("use_kma", True)
        and not (os.getenv("KMA_API_KEY") or os.getenv("KMA_AUTH_KEY"))
    ):
        raise ValueError("기상청 API 키가 필요합니다. 또는 use_kma=False를 명시하세요.")
    if (
        departure is not None
        and timestamp(departure) <= datetime.now(timezone.utc).timestamp()
    ):
        raise ValueError(
            "출발시각이 이미 지났습니다. 미래 시각 또는 None(자동 출발)을 사용하세요."
        )
    if transit_only or options.get("eta_service") is not None:
        return _run_scheduled_optimization(origin, destination, departure, **options)
    from eta_service import BicycleETAService

    with BicycleETAService() as service:
        return _run_scheduled_optimization(
            origin,
            destination,
            departure,
            eta_service=service,
            **{k: v for k, v in options.items() if k != "eta_service"},
        )


def _run_scheduled_optimization(
    origin,
    destination,
    departure=None,
    gtfs_path=None,
    config=None,
    api_key=None,
    use_kma=True,
    use_inventory=True,
    refresh_inventory=True,
    inventory_path=None,
    crosswalk_path=None,
    user_factor=1.0,
    bike_type="regular",
    user_type="domestic",
    eta_service=None,
    output_dir=None,
    progress=print,
    timetable=None,
    transit_dir=None,
    personalization_id=None,
    personalization_path=None,
    on_routes=None,
):
    """Use a prepared subway/bus timetable, a Network, or an explicit GTFS ZIP."""
    load_project_env()
    if departure is None:
        from multimodal_data import KST

        departure = (
            (datetime.now(KST) + timedelta(minutes=5))
            .replace(microsecond=0)
            .isoformat()
        )
    timestamp(departure)
    c = config or ScheduledConfig(
        require_known_inventory=use_inventory,
        max_empty_probability=0.1 if use_inventory else None,
    )
    c.validate()
    if c.search_mode in ("subway", "transit"):
        use_kma = use_inventory = False
        personalization_id = None
    personalization = {
        "enabled": False,
        "factor": user_factor,
        "cold_start": True,
        "usable_records": 0,
        "calibrated": False,
        "basis": "base_p50",
    }
    if personalization_id is not None:
        if c.bike_quantile != "p50":
            raise ValueError("자동 개인화는 P50에서만 지원합니다.")
        if user_factor != 1.0:
            raise ValueError("자동 개인화와 수동 user_factor를 동시에 사용하지 마세요.")
        from personalization import PersonalizationStore, baseline_context

        context = baseline_context(
            getattr(eta_service, "model_version", None),
            getattr(eta_service, "root", None),
        )
        with PersonalizationStore(personalization_path) as store:
            personalization = store.profile(personalization_id, context)
    cache_factor = 1.0 if personalization_id is not None else user_factor
    if (
        c.search_mode not in ("subway", "transit")
        and c.require_known_inventory
        and not use_inventory
    ):
        raise ValueError(
            "재고 확인 필수와 재고 사용 안 함을 동시에 설정할 수 없습니다."
        )
    if use_kma and not (os.getenv("KMA_API_KEY") or os.getenv("KMA_AUTH_KEY")):
        raise ValueError("기상청 예보를 사용하려면 KMA_API_KEY를 설정하세요.")
    path = None
    if c.search_mode == "bicycle":
        timetable = Network(
            nodes={},
            metadata={
                "valid_departure": departure,
                "valid_horizon_minutes": c.horizon_minutes,
            },
        )
    if timetable is None:
        if gtfs_path is not None:
            path = Path(gtfs_path)
            timetable = load_gtfs(path, departure, c.horizon_minutes)
        else:
            from project_paths import TRANSIT_DATA_DIR
            from transit_files import prepare_transit_files

            prepare_transit_files(
                transit_dir or TRANSIT_DATA_DIR, departure, c.horizon_minutes, progress
            )
            if c.search_mode == "subway":
                timetable = build_timetable(
                    transit_dir or TRANSIT_DATA_DIR,
                    departure,
                    c.horizon_minutes,
                    include_bus=False,
                )
            else:
                timetable = build_timetable(
                    transit_dir or TRANSIT_DATA_DIR, departure, c.horizon_minutes
                )
    if timetable.metadata.get("valid_departure") != departure:
        raise ValueError("입력한 출발시각에 맞춰 시간표를 다시 구성하세요.")
    if timetable.metadata.get("valid_horizon_minutes", 0) < c.horizon_minutes:
        raise ValueError("준비된 시간표의 검색 범위가 설정값보다 짧습니다.")
    origin, destination = point(origin), point(destination)
    default_output = RUNTIME_OUTPUT_DIR / "scheduled"
    if c.search_mode != "integrated":
        default_output = default_output / c.search_mode
    output = Path(output_dir or default_output)
    output.mkdir(parents=True, exist_ok=True)
    stations = []
    if c.search_mode not in ("subway", "transit"):
        with STATION_CATALOG.open(encoding="utf-8-sig", newline="") as stream:
            stations = list(csv.DictReader(stream))

    snapshot, inventory, as_of, mapping = {}, None, None, {}
    if use_inventory:
        from inventory_service import InventoryPredictor

        inventory = InventoryPredictor()
        mapping = load_crosswalk(crosswalk_path or STATION_CROSSWALK)
        payload, snapshot = prepare_inventory(
            inventory_path, refresh_inventory, progress
        )
        stations, coverage = eligible_station_catalog(
            stations, mapping, inventory, snapshot
        )
        as_of = datetime.now(timezone.utc).isoformat()
        validate_snapshot(payload, as_of)
        if c.require_known_inventory:
            c = replace(
                c,
                bicycle_available=(
                    c.bicycle_available
                    and timestamp(departure) <= timestamp(as_of) + 3600
                ),
            )
    else:
        coverage = {
            "catalog": len(stations),
            "eligible": len(stations),
            "inventory_filter_applied": False,
        }

    client = KakaoClient(
        RUNTIME_CACHE_DIR / "scheduled_kakao_walk.sqlite",
        api_key,
        c.max_kakao_calls,
        c.kakao_daily_limit,
        progress=progress,
    )
    owned_service = cached = link_cache = None
    try:
        if hasattr(client, "get_cached"):
            from walking_link_cache import WalkingLinkCache

            link_cache = WalkingLinkCache(RUNTIME_CACHE_DIR / "walking_links.sqlite")
            for previous in (
                output / "result.json",
                RUNTIME_OUTPUT_DIR / "scheduled" / "result.json",
                RUNTIME_OUTPUT_DIR / "scheduled" / "subway" / "result.json",
                RUNTIME_OUTPUT_DIR / "scheduled" / "transit" / "result.json",
                RUNTIME_OUTPUT_DIR / "scheduled" / "bicycle" / "result.json",
            ):
                link_cache.remember_result(previous)
        walking = KakaoWalking(client, link_cache)
        network = build_scheduled_network(
            timetable, stations, origin, destination, walking, c, departure=departure
        )
        network.metadata["restored_walk_connections"] = (
            link_cache.restore(network, client) if link_cache else 0
        )
        network.metadata["station_coverage"] = coverage
        if c.save_network:
            _save(output / "network.json", network.__dict__)
        if network.bikes or network.bike_policy:
            if eta_service is None:
                from eta_service import BicycleETAService

                owned_service = BicycleETAService()
                eta_service = owned_service
            provider = V521BikeProvider(
                eta_service,
                inventory,
                snapshot,
                as_of,
                cache_factor,
                use_kma,
                bike_type,
                user_type,
                eta_quantile=c.bike_quantile,
            )
            cached = CachedBikeProvider(
                provider,
                RUNTIME_CACHE_DIR / "scheduled_bike.sqlite",
                {
                    "version": 2,
                    "code": runtime_code_identity(),
                    "artifacts": _artifact_identity(),
                    "use_kma": use_kma,
                    "user_factor": cache_factor,
                    "bike_type": bike_type,
                    "user_type": user_type,
                    "forecast_issue": forecast_issue_key() if use_kma else None,
                },
                departure_bucket_minutes=5,
            )
            bike_provider = cached
            if personalization_id is not None:
                from personalization import PersonalizedBikeProvider

                bike_provider = PersonalizedBikeProvider(cached, personalization)
        else:

            def bike_provider(edge, at):
                raise ValueError("따릉이 후보가 없습니다.")

        router_config = Config(
            horizon_minutes=c.horizon_minutes,
            step_seconds=c.step_seconds,
            bike_expansion_batch_size=c.bicycle_batch_size
            if c.search_mode == "bicycle"
            else c.bike_expansion_batch_size,
            reuse_transit_suffixes=c.reuse_transit_suffixes,
            max_expansions=c.max_expansions,
            max_seconds=c.max_seconds,
            top_k=c.top_k,
            max_bike_predictions=c.max_bike_predictions,
            search_algorithm=c.search_algorithm,
            require_known_inventory=c.require_known_inventory,
            max_empty_probability=c.max_empty_probability,
            latest_bicycle_departure=(
                datetime.fromisoformat(as_of) + timedelta(minutes=60)
            ).isoformat()
            if inventory is not None and c.require_known_inventory
            else None,
        )
        router_config.required_mode = {
            "subway": "subway",
            "transit": "transit",
            "bicycle": "bicycle",
        }.get(c.search_mode)
        if timestamp(departure) <= datetime.now(timezone.utc).timestamp():
            raise ValueError(
                "준비 중 출발시각이 지났습니다. 최신 출발시각으로 다시 실행하세요. 성공한 조회 캐시는 유지됩니다."
            )

        def publish(snapshot):
            if on_routes is None:
                return
            from transit_geometry import attach_transit_geometry

            ids = {
                leg[key]
                for group in ("routes", "mixed_routes")
                for route in snapshot.get(group, [])
                for leg in route.get("legs", [])
                for key in ("from", "to")
            }

            snapshot.update(
                nodes={key: copy.deepcopy(network.nodes[key]) for key in ids},
                departure=departure,
                search_mode=c.search_mode,
                personalization=copy.deepcopy(personalization),
                api_usage=client.stats(),
                inventory_connected=inventory is not None,
                weather_requested=use_kma,
                bicycle_time_quantile=c.bike_quantile
                if c.search_mode not in ("subway", "transit")
                else None,
                model_versions={
                    "eta": "v5.2.1" if cached else None,
                    "inventory": inventory.metadata.get("selected_model")
                    if inventory
                    else None,
                },
            )
            attach_transit_geometry(snapshot, PROJECT_ROOT / "data" / "transit")
            on_routes(snapshot)

        result = Router(
            network,
            bike_provider,
            router_config,
            walking_provider=walking,
            on_routes=publish if on_routes is not None else None,
        ).search("origin", "destination", departure)
        result["nodes"] = network.nodes
        from transit_geometry import attach_transit_geometry

        attach_transit_geometry(result, PROJECT_ROOT / "data" / "transit")
        result.update(
            departure=departure,
            api_usage=client.stats(),
            inventory_connected=inventory is not None,
            search_mode=c.search_mode,
            personalization=personalization,
            inventory_as_of=as_of,
            inventory_status_counts=dict(provider.inventory_status_counts)
            if cached
            else {},
            bicycle_time_quantile=c.bike_quantile
            if c.search_mode not in ("subway", "transit")
            else None,
            weather_requested=use_kma,
            timetable_file=None
            if c.search_mode == "bicycle"
            else path.name
            if path
            else "prepared.json",
            model_versions={
                "eta": "v5.2.1" if cached else None,
                "inventory": inventory.metadata.get("selected_model")
                if inventory
                else None,
            },
            bicycle_checkpoint={
                "hits": cached.hits,
                "misses": cached.misses,
                "batch_calls": cached.batch_calls,
                "inventory_hits": provider.inventory_cache_hits,
                "inventory_misses": provider.inventory_cache_misses,
            }
            if cached
            else None,
        )
        result["limitations"].extend(
            [
                "시간표 기준이며 실제 지연·운휴·조기 도착은 반영하지 않음",
                "설정한 도보·따릉이 거리와 실제 확보한 시간표 범위 내 탐색",
                "재고는 관측 기준 60분 이내에만 평가하며, 이후 따릉이 후보는 제외됨",
            ]
        )
        _save(output / "result.json", result)
        from kakao_optimizer import map_html

        (output / "routes.html").write_text(map_html(result), encoding="utf-8")
        if progress:
            progress(
                f"완료: {result['status']} / 경로 {len(result['routes'])}개 / 새 카카오 도보 호출 {client.calls}회",
                flush=True,
            )
        return result
    finally:
        if link_cache is not None:
            link_cache.close()
        if cached is not None:
            cached.close()
        if owned_service is not None:
            owned_service.close()
        client.close()
