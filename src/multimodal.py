"""Time-expanded multimodal prototype. No artificial bike rental-count limit.

Movement arrivals retain exact seconds. A grid is used only for explicit waits.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from route_identity import route_signature, walking_minutes
from datetime import datetime
from collections import defaultdict, deque
from bisect import bisect_left
import heapq
import itertools
import json
import math
import time
import copy


def timestamp(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value.tzinfo is None:
        raise ValueError("시각에는 +09:00 등 시간대가 필요합니다.")
    return value.timestamp()


def _time_parser():
    """Reuse exact timestamp strings only within one validation/index build."""
    values = {}

    def parse(value):
        if not isinstance(value, str):
            return timestamp(value)
        parsed = values.get(value)
        if parsed is None:
            parsed = timestamp(value)
            values[value] = parsed
        return parsed

    return parse


@dataclass
class Network:
    nodes: dict
    walks: list = field(default_factory=list)
    bikes: list = field(default_factory=list)
    transit: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    bike_policy: dict = field(default_factory=dict)
    walk_policy: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))

    def validate(self):
        parse_time = _time_parser()
        for edge in self.walks + self.bikes + self.transit:
            if edge["from"] not in self.nodes or edge["to"] not in self.nodes:
                raise ValueError("연결선의 노드가 nodes에 없습니다.")
        for edge in self.walks:
            if not math.isfinite(float(edge["minutes"])) or edge["minutes"] <= 0:
                raise ValueError("도보시간은 양수여야 합니다.")
        for edge in self.transit:
            if edge["mode"] not in ("bus", "subway"):
                raise ValueError("대중교통 mode는 bus/subway입니다.")
            if parse_time(edge["arrival"]) <= parse_time(edge["departure"]):
                raise ValueError("대중교통 도착은 출발보다 늦어야 합니다.")
        for edge in self.bikes:
            if edge["from"] == edge["to"]:
                raise ValueError("동일 대여소 자전거 연결은 허용하지 않습니다.")


@dataclass
class Config:
    horizon_minutes: float = 180
    step_seconds: int = 60
    max_expansions: int = 100000
    max_seconds: float = 30
    risk_penalty_minutes: float = 0
    unknown_risk_penalty_minutes: float = 0
    max_empty_probability: float | None = None
    require_known_inventory: bool = False
    top_k: int = 3
    max_bike_predictions: int | None = None
    search_algorithm: str = "astar"
    bike_expansion_batch_size: int = 2
    reuse_transit_suffixes: bool = True
    required_mode: str | None = None
    reuse_bicycle_buckets: bool = True
    preserve_mixed_routes: bool = True
    mixed_top_k: int = 1
    prune_transit_roundtrips: bool = True
    latest_bicycle_departure: str | None = None
    prune_known_dead_ends: bool = True
    skip_unroutable_waits: bool = True


def relaxed_lower_bounds(network, destination, transit_weights=None):
    """Reverse shortest paths in a relaxation of the supplied search graph.

    Walking uses its known duration, transit its minimum in-vehicle duration,
    and every bicycle edge has cost zero. Waiting, boarding, stock constraints,
    grid rounding and penalties are omitted. No learned ETA/speed is assumed.
    Consequently this is a lower bound even for non-FIFO bicycle predictions.
    Nodes with no reverse path fall back to zero in the forward A* search.
    """
    reverse = defaultdict(dict)

    def add(a, b, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("하한 그래프의 구간시간은 0 이상 유한수여야 합니다.")
        previous = reverse[b].get(a, float("inf"))
        if seconds < previous:
            reverse[b][a] = seconds

    for edge in network.walks:
        add(edge["from"], edge["to"], float(edge["minutes"]) * 60)
    for edge in network.bikes:
        add(edge["from"], edge["to"], 0.0)
    if transit_weights is None:
        for edge in network.transit:
            add(
                edge["from"],
                edge["to"],
                timestamp(edge["arrival"]) - timestamp(edge["departure"]),
            )
    else:
        for (a, b), seconds in transit_weights.items():
            add(a, b, seconds)
    distances = {destination: 0.0}
    counter = itertools.count()
    queue = [(0.0, next(counter), destination)]
    while queue:
        value, _, node = heapq.heappop(queue)
        if value != distances[node]:
            continue
        for previous, cost in reverse.get(node, {}).items():
            candidate = value + cost
            if candidate < distances.get(previous, float("inf")):
                distances[previous] = candidate
                heapq.heappush(queue, (candidate, next(counter), previous))
    return distances


class Router:
    """Bike callback(edge, departure ISO) -> minutes, model/route/risk metadata.

    No FIFO assumption is made for learned bicycle travel times: explicit wait
    states allow later departures. Dominance only applies at identical time,
    location and on-board trip. Risk penalties are scores, never probabilities.
    """

    def __init__(
        self,
        network,
        bike_provider,
        config=None,
        walking_provider=None,
        _schedule_index=None,
        on_routes=None,
    ):
        network.validate()
        self.network, self.bike_provider = network, bike_provider
        self.config = config or Config()
        self.walking_provider = walking_provider
        self._schedule_index = _schedule_index
        self.on_routes = on_routes

    def search(self, origin, destination, departure):
        c = self.config
        if c.required_mode not in (None, "bicycle", "subway", "transit"):
            raise ValueError("required_mode는 bicycle/subway/transit 또는 None입니다.")
        if not isinstance(c.mixed_top_k, int) or c.mixed_top_k <= 0:
            raise ValueError("mixed_top_k는 양의 정수여야 합니다.")
        retain_mixed = (
            c.preserve_mixed_routes
            and c.required_mode is None
            and bool(self.network.transit)
            and bool(self.network.bikes or self.network.bike_policy)
        )
        lazy = None
        if self.network.bike_policy or self.network.walk_policy:
            from candidate_edges import LazyConnections

            lazy = LazyConnections(
                self.network, self.walking_provider, destination=destination
            )
        preparation_started = time.monotonic()
        if c.search_algorithm not in ("astar", "dijkstra"):
            raise ValueError("search_algorithm은 astar/dijkstra입니다.")
        if origin not in self.network.nodes or destination not in self.network.nodes:
            raise ValueError("출발/도착 노드가 없습니다.")
        if (
            c.step_seconds <= 0
            or c.horizon_minutes <= 0
            or c.max_seconds <= 0
            or c.max_expansions <= 0
            or c.top_k <= 0
            or not isinstance(c.bike_expansion_batch_size, int)
            or c.bike_expansion_batch_size <= 0
            or (c.max_bike_predictions is not None and c.max_bike_predictions <= 0)
        ):
            raise ValueError("탐색 한도는 양수여야 합니다.")
        if c.risk_penalty_minutes < 0 or c.unknown_risk_penalty_minutes < 0:
            raise ValueError("위험 감점은 음수일 수 없습니다.")
        if (
            c.max_empty_probability is not None
            and not 0 <= c.max_empty_probability <= 1
        ):
            raise ValueError("품절 위험 한도는 0~1입니다.")
        dt = (
            datetime.fromisoformat(departure)
            if isinstance(departure, str)
            else departure
        )
        start = timestamp(dt)
        end = start + c.horizon_minutes * 60
        bicycle_deadline = (
            timestamp(c.latest_bicycle_departure)
            if c.require_known_inventory and c.latest_bicycle_departure is not None
            else None
        )

        def iso(t):
            return datetime.fromtimestamp(t, dt.tzinfo).isoformat()

        def grid(t):
            return (
                start + math.ceil((t - start) / c.step_seconds - 1e-9) * c.step_seconds
            )

        walks, bikes, rides = (defaultdict(list) for _ in range(3))
        trip_rides = defaultdict(list)
        transit_weights = {}
        for edge in self.network.walks:
            walks[edge["from"]].append(edge)
        for edge in self.network.bikes:
            bikes[edge["from"]].append(edge)
        shared = self._schedule_index
        if shared is not None and shared[:3] == (start, end, c.search_algorithm):
            rides, trip_rides, ride_times, trip_times, transit_weights = shared[3:]
        else:
            index_time = _time_parser()
            for edge in self.network.transit:
                dep, arr = index_time(edge["departure"]), index_time(edge["arrival"])
                if start <= dep <= end and arr <= end:
                    rides[edge["from"]].append((dep, arr, edge))
                    trip_rides[(edge["from"], edge["trip_id"])].append((dep, arr, edge))
                    if c.search_algorithm == "astar":
                        pair = (edge["from"], edge["to"])
                        transit_weights[pair] = min(
                            transit_weights.get(pair, float("inf")), arr - dep
                        )
            del index_time
            for node in rides:
                rides[node].sort(key=lambda row: row[0])
            for key in trip_rides:
                trip_rides[key].sort(key=lambda row: row[0])
            ride_times = {k: [r[0] for r in rows] for k, rows in rides.items()}
            trip_times = {k: [r[0] for r in rows] for k, rows in trip_rides.items()}
        schedule_index = (
            start,
            end,
            c.search_algorithm,
            rides,
            trip_rides,
            ride_times,
            trip_times,
            transit_weights,
        )
        heuristic_started = time.monotonic()
        # Unknown lazy walk durations cannot use an overestimating static bound.
        # Zero is admissible; A* then has Dijkstra's ordering for these networks.
        lower_bounds = (
            relaxed_lower_bounds(self.network, destination, transit_weights)
            if c.search_algorithm == "astar" and lazy is None
            else {}
        )
        heuristic_seconds = time.monotonic() - heuristic_started
        preparation_seconds = time.monotonic() - preparation_started

        # Secure complete routes on already known walking/timetable edges before
        # optional connector calls can consume the query budget. No extra model
        # inference or walking API calls are made by this bounded first pass.
        begun = time.monotonic()
        if self.walking_provider is not None and hasattr(
            self.walking_provider, "set_deadline"
        ):
            self.walking_provider.set_deadline(begun + c.max_seconds)
        known_routes, known_expanded = [], 0
        known_roundtrip_pruned = known_roundtrip_waits = 0
        known_walking_replacements = 0
        if (
            lazy is not None
            and c.required_mode != "bicycle"
            and (self.network.walks or self.network.transit)
        ):
            known_network = Network(
                nodes=self.network.nodes,
                walks=self.network.walks,
                transit=self.network.transit,
                metadata=self.network.metadata,
            )
            known_config = replace(
                c,
                max_seconds=min(2.0, c.max_seconds * 0.2),
                max_expansions=min(5000, max(1, c.max_expansions // 5)),
            )
            known_result = Router(
                known_network, None, known_config, _schedule_index=schedule_index
            ).search(origin, destination, dt)
            known_routes = known_result["routes"]
            known_expanded = known_result["expanded_states"]
            known_roundtrip_pruned = known_result.get("pruned_transit_roundtrips", 0)
            known_roundtrip_waits = known_result.get("roundtrip_wait_replacements", 0)
            known_walking_replacements = known_result.get(
                "walking_roundtrip_replacements", 0
            )

        # state: location, time, current vehicle trip. alighting is explicit.
        counter = itertools.count()
        heap, best, records, required_used, bicycle_paths = [], {}, {}, {}, {}
        transit_anchors = {}
        walking_anchors = {}
        walking_roundtrip_replacements = 0
        roundtrip_pruning = bool(c.prune_transit_roundtrips and self.network.transit)
        pruned_roundtrip_boarded = pruned_roundtrip_alight = (
            roundtrip_wait_replacements
        ) = 0
        single_bicycle_graph = (
            c.required_mode == "bicycle"
            and not self.network.transit
            and all(
                k in (origin, destination) or v.get("kind") == "bike"
                for k, v in self.network.nodes.items()
            )
            and all(
                e["from"] == origin or e["to"] == destination
                for e in self.network.walks
            )
        )

        def push(node, t, trip, penalty, boardings, parent, leg, after_bike=False):
            nonlocal pruned_roundtrip_alight, roundtrip_wait_replacements
            nonlocal walking_roundtrip_replacements
            if t > end + 1e-6:
                return
            walk_anchors = walking_anchors.get(parent, {})
            if leg and leg["mode"] not in {"walk", "wait", "alight"}:
                walk_anchors = {}
            walk_key = (node, after_bike)
            previous_walk = walk_anchors.get(walk_key)
            if (
                leg
                and leg["mode"] == "walk"
                and previous_walk
                and previous_walk[1] < t - 1e-6
            ):
                old_idx, old_t, old_penalty, old_boards = previous_walk
                # Retain the exact later action time for timetable/stock changes.
                # Matching after_bike also preserves bicycle re-rental eligibility.
                waiting = dict(
                    mode="wait",
                    **{"from": node, "to": node},
                    start=iso(old_t),
                    arrival=iso(t),
                    minutes=(t - old_t) / 60,
                    source="walking_roundtrip_replaced_by_wait",
                )
                replacement = push(
                    node, t, None, old_penalty, old_boards, old_idx, waiting, after_bike
                )
                walking_roundtrip_replacements += int(replacement is not None)
                return replacement
            anchors = transit_anchors.get(parent, {}) if roundtrip_pruning else {}
            if leg and leg["mode"] in ("walk", "bicycle"):
                # Crossing to another platform or using a bicycle is not a
                # transit-only loop. Preserve these valid transfer choices.
                anchors = {}
            earlier = anchors.get(node)
            if (
                roundtrip_pruning
                and leg
                and leg["mode"] == "alight"
                and earlier
                and earlier[1] < t - 1e-6
                and not earlier[4]
            ):
                # Preserve this EXACT later action time, including inventory
                # changes between minute-grid waits, without the pointless ride.
                pruned_roundtrip_alight += 1
                old_idx, old_t, old_penalty, old_boards, _ = earlier
                waiting = dict(
                    mode="wait",
                    **{"from": node, "to": node},
                    start=iso(old_t),
                    arrival=iso(t),
                    minutes=(t - old_t) / 60,
                    source="transit_roundtrip_replaced_by_wait",
                )
                replacement = push(
                    node, t, None, old_penalty, old_boards, old_idx, waiting
                )
                roundtrip_wait_replacements += int(replacement is not None)
                return
            used = required_used.get(parent, False) or bool(
                c.required_mode
                and leg
                and (
                    leg["mode"] == c.required_mode
                    or c.required_mode == "transit"
                    and leg["mode"] in ("bus", "subway")
                )
            )
            if retain_mixed:
                used = required_used.get(parent, 0) | (
                    1
                    if leg and leg["mode"] == "bicycle"
                    else 2
                    if leg and leg["mode"] in ("bus", "subway")
                    else 0
                )
            path = bicycle_paths.get(parent)
            if single_bicycle_graph and leg and leg["mode"] == "bicycle":
                path = (leg["from"], leg["to"])
            key = (node, round(t, 6), trip, after_bike, used, path)
            value = (penalty, boardings)
            if key in best and best[key] <= value:
                return
            best[key] = value
            idx = next(counter)
            records[idx] = (parent, leg)
            required_used[idx] = used
            if trip is None:
                if walk_key not in walk_anchors:
                    walk_anchors = dict(walk_anchors)
                    walk_anchors[walk_key] = (idx, t, penalty, boardings)
                walking_anchors[idx] = walk_anchors
            if single_bicycle_graph:
                bicycle_paths[idx] = path
            if roundtrip_pruning:
                if trip is None and node not in anchors:
                    anchors = dict(anchors)
                    anchors[node] = (idx, t, penalty, boardings, after_bike)
                transit_anchors[idx] = anchors
            priority = t - start + penalty * 60 + lower_bounds.get(node, 0.0)
            heapq.heappush(
                heap, (priority, boardings, idx, node, t, trip, penalty, after_bike)
            )
            return idx

        push(origin, start, None, 0.0, 0, None, None)
        results, mixed_results, failures, cache = list(known_routes), [], [], {}
        last_update = None
        update_seconds = 0.0

        def publish_routes():
            nonlocal last_update, begun, update_seconds
            if self.on_routes is None or not (results or mixed_results):
                return
            identity = tuple(
                (
                    group,
                    route_signature(r["legs"]),
                    r["total_minutes"],
                    r["score_minutes"],
                    walking_minutes(r),
                )
                for group, values in (("routes", results), ("mixed", mixed_results))
                for r in values
            )
            if identity == last_update:
                return
            update_started = time.monotonic()
            snapshot = copy.deepcopy(
                dict(
                    routes=results,
                    mixed_routes=mixed_results,
                    status="searching",
                    search_complete=False,
                )
            )
            try:
                self.on_routes(snapshot)
                last_update = identity
            except Exception:
                # Display failures must never change search or lose routes.
                self.network.metadata["route_update_errors"] = (
                    self.network.metadata.get("route_update_errors", 0) + 1
                )
            finally:
                duration = time.monotonic() - update_started
                update_seconds += duration
                begun += duration
                if self.walking_provider is not None and hasattr(
                    self.walking_provider, "set_deadline"
                ):
                    self.walking_provider.set_deadline(begun + c.max_seconds)

        publish_routes()

        def keep_route(pool, route, limit):
            signature = route_signature(route["legs"])
            old = next(
                (r for r in pool if route_signature(r["legs"]) == signature), None
            )
            rank = lambda r: (
                r["score_minutes"],
                r["transit_boardings"],
                r["total_minutes"],
                walking_minutes(r),
            )
            if old is None:
                pool.append(route)
            elif rank(route) < rank(old):
                pool.remove(old)
                pool.append(route)
            pool.sort(key=rank)
            del pool[limit:]

        def incumbent_bound():
            if (
                len(results) < c.top_k
                or retain_mixed
                and len(mixed_results) < c.mixed_top_k
            ):
                return None
            return (
                max(
                    results[-1]["score_minutes"],
                    mixed_results[-1]["score_minutes"] if retain_mixed else 0.0,
                )
                * 60
            )

        unroutable_pairs = {}
        expanded, truncated, failure_count = 0, False, 0
        prediction_count, termination_reason = 0, None
        skipped_predictions = 0
        batch_failures = 0
        work = deque()
        work_turn = False
        work_batches = 0
        transit_frontiers = defaultdict(list)
        transit_edges_examined = 0
        reused_transit_expansions = 0
        # Only a single bicycle leg followed by a static destination walk is
        # eligible. Mixed-mode transfers, risk penalties and unknown providers
        # retain the original minute-by-minute search.
        bicycle_bucket_reuse = (
            c.reuse_bicycle_buckets
            and single_bicycle_graph
            and c.risk_penalty_minutes == 0
            and c.unknown_risk_penalty_minutes == 0
            and hasattr(self.bike_provider, "eta_reuse_window")
        )
        served_bicycle_windows = {}
        reused_bicycle_transitions = skipped_bicycle_wait_states = 0
        expired_bicycle_waits = 0
        skipped_dead_end_predictions = unroutable_waits = 0

        def known_dead_end(target):
            if not c.prune_known_dead_ends or target == destination:
                return False
            if walks[target] or rides.get(target):
                return False
            # Unknown lazy connections stay candidates. Only an exhausted,
            # empty outgoing walk stream is a known dead end in this query.
            return lazy is None or (
                target in lazy._walk_complete and not lazy.walk_cache.get(target)
            )

        def emit(edge, minutes, cycling, meta, increment, t, boards, idx, penalty):
            minutes = float(minutes)
            if not math.isfinite(minutes) or minutes <= 0:
                raise ValueError("이동시간은 양수여야 합니다.")
            arr = t + minutes * 60
            leg = dict(
                edge,
                mode="bicycle" if cycling else "walk",
                start=iso(t),
                arrival=iso(arr),
                modeled_arrival=iso(arr),
                minutes=minutes,
                modeled_minutes=minutes,
                grid_buffer_seconds=0.0,
                details=meta,
            )
            push(edge["to"], arr, None, penalty + increment, boards, idx, leg, cycling)

        def walk_work(node, t, boards, idx, penalty):
            for edge in lazy.iter_walks_from(
                node, lambda: time.monotonic() - begun >= c.max_seconds
            ):
                if edge is not None:
                    emit(edge, edge["minutes"], False, {}, 0.0, t, boards, idx, penalty)
                yield  # Yield after each lookup, including a failed/quota-blocked lookup.

        def bike_work(node, t, boards, idx, penalty, edges):
            nonlocal \
                prediction_count, \
                skipped_predictions, \
                batch_failures, \
                failure_count
            nonlocal reused_bicycle_transitions, skipped_bicycle_wait_states
            nonlocal expired_bicycle_waits
            nonlocal skipped_dead_end_predictions, unroutable_waits
            if bicycle_deadline is not None and t > bicycle_deadline + 1e-6:
                expired_bicycle_waits += 1
                return
            windows = {}
            if bicycle_bucket_reuse:
                for edge in edges:
                    token, until = self.bike_provider.eta_reuse_window(edge, iso(t))
                    if timestamp(until) <= t:
                        raise ValueError(
                            "ETA 재사용 구간 종료시각이 올바르지 않습니다."
                        )
                    windows[id(edge)] = (token, timestamp(until))
            iterator = iter(edges)
            while True:
                chunk = list(itertools.islice(iterator, c.bike_expansion_batch_size))
                if not chunk:
                    break
                eligible = []
                for edge in chunk:
                    if (edge["from"], edge["to"]) in unroutable_pairs:
                        continue
                    if known_dead_end(edge["to"]):
                        skipped_dead_end_predictions += 1
                        continue
                    window = windows.get(id(edge))
                    if (
                        window
                        and served_bicycle_windows.get(window[0], float("inf")) <= t
                    ):
                        # Earlier feasible rental, identical ETA, same return
                        # station and no risk penalty dominates this departure.
                        reused_bicycle_transitions += 1
                        continue
                    if hasattr(self.bike_provider, "precheck_inventory"):
                        preliminary = (
                            self.bike_provider.precheck_inventory(edge, iso(t)) or {}
                        )
                        risk = preliminary.get("p_empty")
                        if risk is None and c.require_known_inventory:
                            continue
                        if (
                            risk is not None
                            and c.max_empty_probability is not None
                            and risk > c.max_empty_probability
                        ):
                            continue
                    eligible.append(edge)
                if eligible and hasattr(self.bike_provider, "prefetch"):
                    remaining = (
                        len(eligible)
                        if c.max_bike_predictions is None
                        else min(
                            len(eligible),
                            max(0, c.max_bike_predictions - prediction_count),
                        )
                    )
                    before = getattr(self.bike_provider, "misses", None)
                    prefetched = 0
                    try:
                        prefetched = self.bike_provider.prefetch(
                            eligible, iso(t), remaining
                        )
                    except Exception:
                        batch_failures += 1
                    finally:
                        prediction_count += (
                            self.bike_provider.misses - before
                            if before is not None
                            else prefetched
                        )
                for edge in eligible:
                    if time.monotonic() - begun >= c.max_seconds:
                        yield  # Keep unfinished work visible to the outer timeout check.
                        return
                    key = (edge["from"], edge["to"], t)
                    if key not in cache:
                        try:
                            needs_new = (
                                self.bike_provider.needs_prediction(edge, iso(t))
                                if hasattr(self.bike_provider, "needs_prediction")
                                else True
                            )
                            if (
                                needs_new
                                and c.max_bike_predictions is not None
                                and prediction_count >= c.max_bike_predictions
                            ):
                                skipped_predictions += 1
                                continue
                            prediction_count += int(needs_new)
                            cache[key] = self.bike_provider(edge, iso(t))
                        except Exception as exc:
                            cache[key] = None
                            if getattr(exc, "permanent_route_failure", False):
                                unroutable_pairs[(edge["from"], edge["to"])] = str(exc)[
                                    :200
                                ]
                                continue
                            failure_count += 1
                            if len(failures) < 30:
                                failures.append(
                                    dict(
                                        start=edge["from"],
                                        end=edge["to"],
                                        type=type(exc).__name__,
                                        message=str(exc)[:200],
                                    )
                                )
                    meta = cache[key]
                    if meta is None:
                        continue
                    risk = meta.get("p_empty")
                    if risk is None:
                        if c.require_known_inventory:
                            continue
                        increment = c.unknown_risk_penalty_minutes
                    else:
                        if not math.isfinite(risk) or not 0 <= risk <= 1:
                            raise ValueError("잘못된 재고 확률")
                        if (
                            c.max_empty_probability is not None
                            and risk > c.max_empty_probability
                        ):
                            continue
                        increment = c.risk_penalty_minutes * risk
                    emit(
                        edge,
                        meta["minutes"],
                        True,
                        meta,
                        increment,
                        t,
                        boards,
                        idx,
                        penalty,
                    )
                    window = windows.get(id(edge))
                    if window:
                        served_bicycle_windows[window[0]] = min(
                            t, served_bicycle_windows.get(window[0], float("inf"))
                        )
                yield
            if c.skip_unroutable_waits and all(
                (e["from"], e["to"]) in unroutable_pairs or known_dead_end(e["to"])
                for e in edges
            ):
                unroutable_waits += 1
                return
            # Delay future departure states until this state's candidate stream
            # is exhausted, avoiding a flood of near-identical waiting tasks.
            # Its pending lower bound prevents an unsafe early optimality claim.
            wait_to = grid(t + 1e-6)
            if wait_to <= t + 1e-6:
                wait_to += c.step_seconds
            if windows and all(
                (e["from"], e["to"]) in unroutable_pairs
                or served_bicycle_windows.get(windows[id(e)][0], float("inf")) <= t
                for e in edges
            ):
                # Jump only after EVERY OD has a feasible earlier departure (or
                # permanent road failure). Unavailable inventory keeps all waits.
                next_change = grid(min(w[1] for w in windows.values()))
                skipped_bicycle_wait_states += max(
                    0, int(round((next_change - wait_to) / c.step_seconds))
                )
                wait_to = max(wait_to, next_change)
            if bicycle_deadline is not None and wait_to > bicycle_deadline + 1e-6:
                expired_bicycle_waits += 1
                return
            push(
                node,
                wait_to,
                None,
                penalty,
                boards,
                idx,
                dict(
                    mode="wait",
                    **{"from": node, "to": node},
                    start=iso(t),
                    arrival=iso(wait_to),
                    minutes=(wait_to - t) / 60,
                ),
            )

        while heap or work:
            if expanded + known_expanded >= c.max_expansions:
                truncated = True
                termination_reason = "max_expansions"
                break
            if time.monotonic() - begun >= c.max_seconds:
                truncated = True
                termination_reason = "max_seconds"
                break
            bound = incumbent_bound()
            if (
                bound is not None
                and (not heap or heap[0][0] > bound + 1e-6)
                and all(item[0] > bound + 1e-6 for item in work)
            ):
                break
            # Round-robin unfinished expensive tasks, alternating with reached
            # states. Continuations are not reinserted at the original time in
            # the route heap (which would monopolize its minimum again).
            if work and (work_turn or not heap):
                item = work.popleft()
                bound, state_key, state_value, iterator = item
                if best.get(state_key) == state_value:
                    try:
                        next(iterator)
                        work.append(item)
                        work_batches += 1
                    except StopIteration:
                        pass
                work_turn = False
                continue
            work_turn = True
            priority, boards, idx, node, t, trip, penalty, after_bike = heapq.heappop(
                heap
            )
            used = required_used[idx]
            if best.get(
                (node, round(t, 6), trip, after_bike, used, bicycle_paths.get(idx))
            ) != (penalty, boards):
                continue
            expanded += 1
            score = t - start + penalty * 60  # Report actual objective, never g+h.
            if node == destination and trip is None:
                if c.required_mode and not used:
                    continue
                legs, cur = [], idx
                while cur is not None:
                    parent, leg = records[cur]
                    if leg is not None:
                        legs.append(leg)
                    cur = parent
                legs.reverse()
                signature = route_signature(legs)
                route = dict(
                    arrival=iso(t),
                    total_minutes=(t - start) / 60,
                    score_minutes=score / 60,
                    bike_rentals=sum(l["mode"] == "bicycle" for l in legs),
                    transit_boardings=boards,
                    legs=legs,
                )
                route["route_kind"] = (
                    "mixed"
                    if route["bike_rentals"] and boards
                    else "bicycle"
                    if route["bike_rentals"]
                    else "transit"
                    if boards
                    else "walk"
                )
                keep_route(results, route, c.top_k)
                if retain_mixed and route["route_kind"] == "mixed":
                    keep_route(mixed_results, route, c.mixed_top_k)
                publish_routes()
                # A deferred task still represents unexamined outgoing edges.
                # Do not stop at the first discovered route if its bound can
                # still beat the current k-th score (including score ties).
                threshold = incumbent_bound()
                if threshold is not None:
                    if (not heap or heap[0][0] > threshold + 1e-6) and all(
                        item[0] > threshold + 1e-6 for item in work
                    ):
                        break
                continue
            # Staying aboard preserves trip identity through intermediate stops.
            if trip is None:
                schedule = rides.get(node, ())
                departures = ride_times.get(node, ())
            else:
                schedule = trip_rides.get((node, trip), ())
                departures = trip_times.get((node, trip), ())
            index = bisect_left(departures, t - 1e-6)
            frontier = transit_frontiers[(node, trip, used)]
            # An earlier arrival with an equal/better (risk penalty, boarding
            # count) already submitted every event this state could board.
            # Skip only duplicate transit submissions, not this state's walks,
            # bicycle actions, waiting options, or any outgoing event identity.
            reuse = (
                c.reuse_transit_suffixes
                and schedule
                and any(
                    old_t <= t and old_value <= (penalty, boards)
                    for old_t, old_value in frontier
                )
            )
            if reuse:
                reused_transit_expansions += 1
            for dep, arr, edge in (
                () if reuse else itertools.islice(schedule, index, None)
            ):
                transit_edges_examined += 1
                if dep < t - 1e-6:
                    continue
                same = trip == edge["trip_id"]
                if trip is not None and not same:
                    continue
                if trip is None and not edge.get("pickup_allowed", True):
                    continue
                if trip is None and dep < t + float(edge.get("boarding_seconds", 0)):
                    continue
                leg = dict(
                    edge,
                    mode=edge["mode"],
                    start=iso(t),
                    departure=iso(dep),
                    arrival=iso(arr),
                    wait_minutes=(dep - t) / 60,
                    minutes=(arr - t) / 60,
                )
                anchors = transit_anchors.get(idx, {})
                earlier = anchors.get(edge["to"]) if roundtrip_pruning else None
                can_replace_onboard = False
                if (
                    earlier
                    and earlier[1] < arr - 1e-6
                    and not earlier[4]
                    and earlier[3] + 1 <= boards + int(not same)
                ):
                    continuations = trip_rides.get((edge["to"], edge["trip_id"]), ())
                    # Staying aboard may be essential on a pickup-restricted
                    # circular service or with an unmet boarding buffer.
                    can_replace_onboard = all(
                        next_edge.get("pickup_allowed", True)
                        and dep2 + 1e-6
                        >= earlier[1] + float(next_edge.get("boarding_seconds", 0))
                        for dep2, _, next_edge in continuations
                        if dep2 >= arr - 1e-6
                    )
                if can_replace_onboard:
                    pruned_roundtrip_boarded += 1
                    on_idx = None
                else:
                    on_idx = push(
                        edge["to"],
                        arr,
                        edge["trip_id"],
                        penalty,
                        boards + int(not same),
                        idx,
                        leg,
                    )
                if edge.get("dropoff_allowed", True):
                    off = dict(
                        mode="alight",
                        **{"from": edge["to"], "to": edge["to"]},
                        start=iso(arr),
                        arrival=iso(arr),
                        minutes=0,
                    )
                    # Record the vehicle edge first, then the off-board state.
                    ride_idx = next(counter)
                    records[ride_idx] = (idx, leg)
                    required_used[ride_idx] = (
                        (used | 2)
                        if retain_mixed
                        else used
                        or edge["mode"] == c.required_mode
                        or c.required_mode == "transit"
                        and edge["mode"] in ("bus", "subway")
                    )
                    if roundtrip_pruning:
                        transit_anchors[ride_idx] = anchors
                    off_idx = push(
                        edge["to"],
                        arr,
                        None,
                        penalty,
                        boards + int(not same),
                        ride_idx,
                        off,
                    )
                    if roundtrip_pruning and on_idx is not None and off_idx is not None:
                        # A passenger staying aboard could have alighted here.
                        # Remember that valid alternative for later loop checks.
                        transit_anchors[on_idx] = transit_anchors[off_idx]
            if c.reuse_transit_suffixes and schedule and not reuse:
                value = (penalty, boards)
                frontier[:] = [
                    (old_t, old_value)
                    for old_t, old_value in frontier
                    if not (t <= old_t and value <= old_value)
                ]
                frontier.append((t, value))
            if trip is not None:
                continue
            if t >= end:
                # Goal recognition and zero-time alighting happen above. No
                # positive-duration movement can finish inside the horizon now.
                self.network.metadata["skipped_horizon_movement_states"] = (
                    self.network.metadata.get("skipped_horizon_movement_states", 0) + 1
                )
                continue
            if lazy is not None:
                if node not in lazy.bike_cache:
                    bikes[node].extend(lazy.bikes_from(node))
            # Deterministic known edges are cheap and immediately usable.
            for edge in walks[node]:
                emit(edge, edge["minutes"], False, {}, 0.0, t, boards, idx, penalty)
            state_key = (
                node,
                round(t, 6),
                trip,
                after_bike,
                used,
                bicycle_paths.get(idx),
            )
            state_value = (penalty, boards)
            if lazy is not None and self.network.walk_policy:
                work.append(
                    (
                        priority,
                        state_key,
                        state_value,
                        walk_work(node, t, boards, idx, penalty),
                    )
                )
            if bikes[node] and not after_bike:
                # Evaluate a reached rental point before buying more access
                # walks. Its remaining batches rejoin normal round-robin work.
                work.appendleft(
                    (
                        priority,
                        state_key,
                        state_value,
                        bike_work(node, t, boards, idx, penalty, bikes[node]),
                    )
                )
        # Retain the known-route fallback even if later API work times out.
        # Merge by the same objective, not by discovery order; a newly found
        # better mixed route can still replace a first-pass route.
        merged, seen = [], set()
        for route in sorted(
            results + known_routes,
            key=lambda r: (
                r["score_minutes"],
                r["transit_boardings"],
                r["total_minutes"],
                walking_minutes(r),
            ),
        ):
            signature = route_signature(route["legs"])
            if signature not in seen:
                seen.add(signature)
                merged.append(route)
        results = merged[: c.top_k]
        connectors_missing = bool(self.network.metadata.get("connector_limit_reached"))
        walking_errors = self.network.metadata.get("walking_api_errors", 0)
        incomplete = (
            truncated
            or failure_count > 0
            or skipped_predictions > 0
            or connectors_missing
            or walking_errors > 0
        )
        if walking_errors and termination_reason is None:
            termination_reason = "walking_api_error"
        if connectors_missing and termination_reason is None:
            termination_reason = "connector_limit"
        if skipped_predictions and termination_reason is None:
            termination_reason = "bike_prediction_limit"
        return dict(
            status="partial" if incomplete else ("ok" if results else "no_route"),
            required_mode=c.required_mode,
            bicycle_time_quantile=getattr(self.bike_provider, "eta_quantile", None),
            routes=results,
            mixed_routes=mixed_results,
            mixed_routes_requested=retain_mixed,
            mixed_route_found=bool(mixed_results),
            search_complete=not incomplete,
            budget_exhausted=truncated or skipped_predictions > 0 or connectors_missing,
            expanded_states=expanded + known_expanded,
            known_route_count=len(known_routes),
            known_route_expanded_states=known_expanded,
            deferred_work_batches=work_batches,
            pending_work_items=len(work),
            transit_edges_examined=transit_edges_examined,
            reused_transit_expansions=reused_transit_expansions,
            transit_roundtrip_pruning=roundtrip_pruning,
            walking_roundtrip_replacements=walking_roundtrip_replacements
            + known_walking_replacements,
            pruned_transit_roundtrips=pruned_roundtrip_boarded
            + pruned_roundtrip_alight
            + known_roundtrip_pruned,
            roundtrip_wait_replacements=roundtrip_wait_replacements
            + known_roundtrip_waits,
            bicycle_bucket_reuse=bicycle_bucket_reuse,
            reused_bicycle_transitions=reused_bicycle_transitions,
            skipped_bicycle_wait_states=skipped_bicycle_wait_states,
            expired_bicycle_waits=expired_bicycle_waits,
            skipped_dead_end_predictions=skipped_dead_end_predictions,
            unroutable_waits=unroutable_waits,
            expansion_policy="round_robin_connections",
            bike_expansion_batch_size=c.bike_expansion_batch_size,
            termination_reason=termination_reason,
            bike_predictions=prediction_count,
            search_algorithm=c.search_algorithm,
            heuristic_mode="zero_admissible_lazy_graph"
            if lazy is not None
            else "static_relaxation",
            search_preparation_seconds=preparation_seconds,
            heuristic_build_seconds=heuristic_seconds,
            heuristic_origin_minutes=lower_bounds.get(origin, 0.0) / 60,
            heuristic_positive_nodes=sum(value > 0 for value in lower_bounds.values()),
            skipped_bike_requests=skipped_predictions,
            bike_batch_fallbacks=batch_failures,
            elapsed_seconds=time.monotonic() - begun,
            route_update_seconds=update_seconds,
            prediction_failures=failure_count,
            unroutable_bike_pairs=[
                dict(start=a, end=b, reason=reason)
                for (a, b), reason in unroutable_pairs.items()
            ],
            failure_examples=failures,
            network_metadata=self.network.metadata,
            coverage_complete=self.network.metadata.get("coverage_complete", True),
            data_gaps=self.network.metadata.get("data_gaps", []),
            limitations=[
                "따릉이 구간별 예측시간을 사용한 계획시간이며 전체 경로의 분위수는 아님",
                "입력 교통망 범위와 시간 격자 내 탐색; 서울 전역 최적 보장 없음",
                "위험 합계는 순위 점수이며 전체 대여 성공확률이 아님",
                "반납 가능성은 평가하지 않음",
                "대안은 유지된 상태에서 발견된 서로 다른 경로이며 전체 k-최단경로 보장은 아님",
            ],
            step_seconds=c.step_seconds,
        )


class V521BikeProvider:
    """Reuse a loaded model. Snapshot is frozen at query time, never fabricated."""

    def __init__(
        self,
        eta_service,
        inventory=None,
        snapshot=None,
        as_of=None,
        user_factor=1.0,
        use_kma=False,
        bike_type="regular",
        user_type="domestic",
        eta_quantile="p50",
    ):
        if not math.isfinite(user_factor) or not 0.7 <= user_factor <= 1.5:
            raise ValueError("개인계수는 0.7~1.5입니다.")
        if eta_quantile not in ("p50", "p80", "p90"):
            raise ValueError("이동시간 분위수는 p50/p80/p90입니다.")
        self.eta_quantile = eta_quantile
        self.eta, self.inventory, self.snapshot = eta_service, inventory, snapshot or {}
        self.as_of = as_of
        self.factor, self.use_kma = user_factor, use_kma
        self.bike_type, self.user_type = bike_type, user_type
        self._inventory_cache = {}
        self.inventory_status_counts = defaultdict(int)
        self.inventory_cache_hits = self.inventory_cache_misses = 0

    def _eta_value(self, result, forecast_error=None):
        minutes = float(result["eta"][self.eta_quantile + "_min"]) * self.factor
        if not math.isfinite(minutes) or minutes <= 0:
            raise ValueError("이동시간 예측은 0보다 큰 유한수여야 합니다.")
        return dict(
            minutes=minutes,
            eta_quantile=self.eta_quantile,
            base_eta=result["eta"],
            user_factor=self.factor,
            personalization_calibrated=False,
            route=result["route"],
            model=result["model"],
            weather_error=forecast_error,
            weather_forecast=result.get("weather_forecast"),
        )

    def predict_eta_batch(self, edges, departure):
        requests = [
            dict(
                start_station_id=e["eta_from"],
                end_station_id=e["eta_to"],
                departure=departure,
                bike_type=self.bike_type,
                user_type=self.user_type,
            )
            for e in edges
        ]
        if self.use_kma and hasattr(self.eta, "predict_with_kma_batch"):
            results = self.eta.predict_with_kma_batch(requests)
        elif not self.use_kma and hasattr(self.eta, "predict_batch"):
            results = self.eta.predict_batch(requests)
        else:
            return [self.predict_eta(edge, departure) for edge in edges]
        return [
            self._eta_value(result, result.get("weather_error")) for result in results
        ]

    def predict_eta(self, edge, departure):
        """Predict travel time without binding it to one inventory snapshot."""
        request = dict(
            start_station_id=edge["eta_from"],
            end_station_id=edge["eta_to"],
            departure=departure,
            bike_type=self.bike_type,
            user_type=self.user_type,
        )
        forecast_error = None
        if self.use_kma:
            try:
                result = self.eta.predict_with_kma(**request)
            except Exception as exc:
                if getattr(exc, "permanent_route_failure", False):
                    raise
                forecast_error = type(exc).__name__
                result = self.eta.predict(**request)
        else:
            result = self.eta.predict(**request)
        return self._eta_value(result, forecast_error)

    def precheck_inventory(self, edge, departure):
        """Compute cheap availability risk before expensive route inference."""
        key = (edge.get("inventory_from"), departure)
        if key in self._inventory_cache:
            self.inventory_cache_hits += 1
            return self._inventory_cache[key]
        self.inventory_cache_misses += 1
        risk, risk_status, prediction = None, "unavailable", None
        station = edge.get("inventory_from")
        obs = self.snapshot.get(station)
        if self.inventory is not None and obs is not None and self.as_of is not None:
            horizon = (timestamp(departure) - timestamp(self.as_of)) / 60
            age = timestamp(self.as_of) - timestamp(obs["snapshot_at"])
            if not 0 <= age <= 600:
                risk_status = "stale_snapshot"
            elif not 0 <= horizon <= 60:
                risk_status = "outside_60min_horizon"
            else:
                try:
                    prediction = self.inventory.predict(
                        station_id=station,
                        current_bikes=obs["current_bikes"],
                        eta_minutes=horizon,
                        as_of=self.as_of,
                        snapshot_at=obs["snapshot_at"],
                    )
                    risk, risk_status = prediction["p_empty_at_arrival"], "uncalibrated"
                except (ValueError, KeyError):
                    risk_status = "unsupported_station"
        result = dict(
            p_empty=risk, inventory_status=risk_status, inventory_prediction=prediction
        )
        self.inventory_status_counts[risk_status] += 1
        self._inventory_cache[key] = result
        return result

    def attach_inventory(self, value, edge, departure):
        """Attach current-stock risk to a cached travel-time result."""
        value = dict(value)
        value.update(self.precheck_inventory(edge, departure))
        return value

    def __call__(self, edge, departure):
        return self.attach_inventory(self.predict_eta(edge, departure), edge, departure)
