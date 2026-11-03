"""Lazy, radius-complete local connectors; no nearest-N station/OD pruning."""

from collections import defaultdict, deque
import heapq
import math
from multimodal_data import distance
from station_coordinates import bicycle_coordinates
from kakao_routes import CallLimit, KakaoError, WalkingDeadline, record_walking_error


def adaptive_radii(
    origin,
    destination,
    nodes,
    stations,
    walk_radius,
    bike_radius,
    max_access_walk_minutes,
):
    """Retain the original core; discover access within a walking-time policy.

    The walking ceiling is an explicit user policy, not a path-time estimate.
    Bicycle discovery spans the requested OD plus its two access envelopes.
    Only actual walking responses and model ETAs become route costs.
    """
    access = max(walk_radius, max_access_walk_minutes * 80)
    bicycle = max(bike_radius, distance(origin, destination) + 2 * access)
    return access, bicycle


def adaptive_walk_policy(
    origin,
    destination,
    nodes,
    stations,
    core_radius,
    max_access_walk_minutes,
):
    """Build a density- and OD-aware *priority* envelope for walking.

    ``max_distance_m`` remains a fallback envelope so a previously valid long
    access is not deleted.  ``preferred_endpoint_radius_m`` controls which
    endpoint candidates are tried first.  Dense endpoints therefore stay near
    the core, while sparse endpoints and longer ODs expand earlier.
    """
    core = float(core_radius)
    maximum = max(core, float(max_access_walk_minutes) * 80.0)
    priority_floor = min(core, 3 * 80.0)
    od_distance = distance(origin, destination)
    # Access walking has more opportunity to pay off on a longer OD.  This is
    # only a priority cap; candidates up to ``maximum`` remain as a fallback.
    preferred_cap = min(maximum, max(core, od_distance * 0.25))
    target = 3 if od_distance < 3000 else 4 if od_distance < 10000 else 6

    transit = [
        value
        for value in nodes.values()
        if value.get("kind") != "endpoint"
        and value.get("latitude") not in (None, "")
        and value.get("longitude") not in (None, "")
    ]
    bicycles = [
        {
            "latitude": float(value["latitude"]),
            "longitude": float(value["longitude"]),
        }
        for value in stations
        if value.get("latitude") not in (None, "")
        and value.get("longitude") not in (None, "")
    ]

    def endpoint_radius(point):
        needed = priority_floor
        distances_by_mode = {}
        for name, values in (("transit", transit), ("bicycle", bicycles)):
            distances = [distance(point, value) for value in values]
            distances_by_mode[name] = distances
            nearest = heapq.nsmallest(
                target, (value for value in distances if value <= maximum)
            )
            if nearest:
                kth = nearest[-1]
                needed = max(needed, kth)
        radius = min(preferred_cap, max(priority_floor, needed))
        counts = {
            name: sum(value <= radius for value in distances)
            for name, distances in distances_by_mode.items()
        }
        return radius, counts

    origin_radius, origin_counts = endpoint_radius(origin)
    destination_radius, destination_counts = endpoint_radius(destination)
    return {
        "core_distance_m": core,
        "priority_floor_m": priority_floor,
        "max_distance_m": maximum,
        "preferred_endpoint_radius_m": {
            "origin": origin_radius,
            "destination": destination_radius,
        },
        "target_neighbors": target,
        "od_distance_m": od_distance,
        "preferred_cap_m": preferred_cap,
        "endpoint_counts": {
            "origin": origin_counts,
            "destination": destination_counts,
        },
        "strategy": "density_od_priority_with_max_fallback",
    }


class SpatialIndex:
    def __init__(self, nodes, cell_degrees=0.01):
        self.nodes, self.cell = nodes, cell_degrees
        self.cells = defaultdict(list)
        for key, point in nodes.items():
            self.cells[self._cell(point)].append(key)

    def _cell(self, point):
        return (
            math.floor(float(point["latitude"]) / self.cell),
            math.floor(float(point["longitude"]) / self.cell),
        )

    def near(self, point, radius):
        lat, lon = float(point["latitude"]), float(point["longitude"])
        dy = radius / 110000
        dx = dy / max(0.01, math.cos(math.radians(lat)))
        found = []
        for y in range(
            math.floor((lat - dy) / self.cell), math.floor((lat + dy) / self.cell) + 1
        ):
            for x in range(
                math.floor((lon - dx) / self.cell),
                math.floor((lon + dx) / self.cell) + 1,
            ):
                for key in self.cells.get((y, x), ()):
                    d = distance(point, self.nodes[key])
                    if d <= radius:
                        found.append((d, key))
        return sorted(found)

    def any_near(self, point, radius, accept=None):
        lat, lon = float(point["latitude"]), float(point["longitude"])
        dy = radius / 110000
        dx = dy / max(0.01, math.cos(math.radians(lat)))
        for y in range(
            math.floor((lat - dy) / self.cell), math.floor((lat + dy) / self.cell) + 1
        ):
            for x in range(
                math.floor((lon - dx) / self.cell),
                math.floor((lon + dx) / self.cell) + 1,
            ):
                for key in self.cells.get((y, x), ()):
                    if accept is not None and not accept(key):
                        continue
                    if distance(point, self.nodes[key]) <= radius:
                        return True
        return False


class DeadEndGuard:
    """Reject only nodes with no possible outgoing movement in the full graph.

    Unknown road walks and any scheduled departure count as possible exits.
    A bike station can also be a walking relay even when rentals are disabled.
    """

    def __init__(self, network, walk_index=None, destination="destination"):
        self.network, self.destination = network, destination
        self.walk_index = walk_index or (
            SpatialIndex(network.nodes) if network.walk_policy else None
        )
        self.static_exits = {
            e["from"]
            for edges in (network.walks, network.transit, network.bikes)
            for e in edges
        }
        self.memo = {}

    def is_dead(self, key):
        if self.destination not in self.network.nodes:
            return False
        if key == self.destination or key in self.static_exits:
            return False
        source = self.network.nodes[key]
        if source.get("kind") == "bike" and self.network.bike_policy:
            return False
        if key in self.memo:
            return self.memo[key]
        pending, seen = [key], set()
        while pending:
            node = pending.pop()
            if node in seen or self.memo.get(node) is True:
                continue
            value = self.network.nodes[node]
            if (
                node == self.destination
                or node in self.static_exits
                or self.memo.get(node) is False
                or (value.get("kind") == "bike" and self.network.bike_policy)
            ):
                self.memo.update((visited, False) for visited in seen)
                self.memo[key] = False
                return False
            seen.add(node)
            # A computation cap never proves a dead end: retain larger or
            # uncertain components. No API is needed for this optimistic test.
            if len(seen) > 32:
                self.memo.update((visited, False) for visited in seen)
                self.memo[key] = False
                return False
            if self.walk_index is None:
                continue

            def allowed(other):
                return other not in (node, "origin") and not (
                    value.get("kind") == self.network.nodes[other].get("kind") == "bike"
                )

            def anchor(other):
                return allowed(other) and (
                    other == self.destination
                    or other in self.static_exits
                    or (
                        self.network.nodes[other].get("kind") == "bike"
                        and bool(self.network.bike_policy)
                    )
                )

            radius = self.network.walk_policy["max_distance_m"]
            if self.walk_index.any_near(value, radius, accept=anchor):
                self.memo.update((visited, False) for visited in seen)
                self.memo[key] = False
                return False
            pending.extend(
                other
                for _, other in self.walk_index.near(value, radius)
                if allowed(other) and other not in seen
            )
        self.memo.update((node, True) for node in seen)
        return True


class LazyConnections:
    def __init__(self, network, walking=None, destination="destination"):
        self.network, self.walking = network, walking
        bike_nodes = {k: v for k, v in network.nodes.items() if v.get("kind") == "bike"}
        self.bike_index = SpatialIndex(bike_nodes)
        self.walk_index = SpatialIndex(network.nodes) if network.walk_policy else None
        self.dead_ends = (
            DeadEndGuard(network, self.walk_index, destination)
            if walking is not None and network.walk_policy
            else None
        )
        self.bike_cache, self.walk_cache = {}, {}
        self._walk_candidates, self._walk_complete = {}, set()
        self._cached_walks = {}
        self._new_walks_blocked = False
        self._existing_walks = {(e["from"], e["to"]) for e in network.walks}
        self.calls = 0
        self.transfer_index = (
            SpatialIndex(
                {k: v for k, v in network.nodes.items() if v.get("kind") == "stop"}
            )
            if network.bike_policy.get("prioritize_transfers") and network.transit
            else None
        )
        self._transfer_targets = {}

    def _preferred_walk_radius(self, start, rows):
        """Return the first-pass radius without deleting fallback candidates."""
        policy = self.network.walk_policy
        endpoints = policy.get("preferred_endpoint_radius_m", {})
        if start in endpoints:
            radius = float(endpoints[start])
        else:
            core = float(policy.get("core_distance_m", policy["max_distance_m"]))
            floor = float(policy.get("priority_floor_m", core))
            target = max(1, int(policy.get("target_neighbors", 4)))
            groups = defaultdict(list)
            source = self.network.nodes[start]
            for d, end in rows:
                if end in (start, "origin"):
                    continue
                value = self.network.nodes[end]
                if source.get("kind") == value.get("kind") == "bike":
                    continue
                kind = "rail" if end.startswith("subway:") else value.get("kind")
                groups[kind].append(d)
            if groups:
                per_group = max(1, target // 2)
                radius = max(
                    floor,
                    *(
                        values[min(per_group, len(values)) - 1]
                        for values in groups.values()
                    ),
                )
            else:
                radius = floor
        radius = min(float(policy["max_distance_m"]), radius)
        stats = self.network.metadata.setdefault(
            "adaptive_walk_priority",
            {
                "evaluated_nodes": 0,
                "expanded_nodes": 0,
                "min_radius_m": None,
                "max_radius_m": 0,
            },
        )
        stats["evaluated_nodes"] += 1
        core = float(policy.get("core_distance_m", policy["max_distance_m"]))
        stats["expanded_nodes"] += int(radius > core)
        stats["min_radius_m"] = (
            radius
            if stats["min_radius_m"] is None
            else min(stats["min_radius_m"], radius)
        )
        stats["max_radius_m"] = max(stats["max_radius_m"], radius)
        return radius

    def _fair_transfer_order(self, rows, goal):
        groups = [defaultdict(deque) for _ in range(3)]
        radius = self.network.walk_policy.get("max_distance_m", 900)
        for edge in rows:
            target = self.network.nodes[edge["to"]]
            if distance(target, goal) <= radius:
                group = 0
            else:
                if edge["to"] not in self._transfer_targets:
                    self._transfer_targets[edge["to"]] = self.transfer_index.any_near(
                        target, radius
                    )
                group = 1 if self._transfer_targets[edge["to"]] else 2
            groups[group][self.bike_index._cell(target)].append(edge)
        queues = []
        for cells in groups:
            cells = deque(cells.values())
            balanced = deque()
            while cells:
                cell = cells.popleft()
                balanced.append(cell.popleft())
                if cell:
                    cells.append(cell)
            if balanced:
                queues.append(balanced)
        # Destination, transit access and remaining candidates all get turns.
        # Every directed pair is retained, including locally backward detours.
        queues = deque(queues)
        ordered = []
        while queues:
            group = queues.popleft()
            ordered.append(group.popleft())
            if group:
                queues.append(group)
        return ordered

    def bikes_from(self, start):
        if start not in self.bike_index.nodes or not self.network.bike_policy:
            return []
        if start not in self.bike_cache:
            source = self.bike_index.nodes[start]
            origins = self.network.bike_policy.get("origin_access_ids")
            if origins is not None and str(source["station_id"]) not in origins:
                self.bike_cache[start] = []
                return []
            destinations = self.network.bike_policy.get("destination_access_ids")
            destinations = set(destinations) if destinations is not None else None
            rows = []
            for d, end in self.bike_index.near(
                source, self.network.bike_policy["max_distance_m"]
            ):
                if end == start:
                    continue
                target = self.bike_index.nodes[end]
                if (
                    destinations is not None
                    and str(target["station_id"]) not in destinations
                ):
                    continue
                rows.append(
                    bicycle_coordinates(
                        {
                            "from": start,
                            "to": end,
                            "eta_from": str(source["station_id"]),
                            "eta_to": str(target["station_id"]),
                            "inventory_from": source.get("inventory_station_id"),
                            "straight_distance_m": d,
                        },
                        source,
                        target,
                    )
                )
            goal = self.network.nodes.get("destination")
            if goal:
                # Prefer useful progress, but retain every candidate.
                rows.sort(
                    key=lambda e: (
                        distance(self.network.nodes[e["to"]], goal),
                        e["straight_distance_m"],
                        e["to"],
                    )
                )
                if self.transfer_index is not None:
                    rows = self._fair_transfer_order(rows, goal)
            core = self.network.bike_policy.get("core_distance_m")
            if core is not None:
                near = deque(e for e in rows if e["straight_distance_m"] <= core)
                extended = deque(e for e in rows if e["straight_distance_m"] > core)
                rows = []
                while near or extended:
                    if near:
                        rows.append(near.popleft())
                    if extended:
                        rows.append(extended.popleft())
            self.bike_cache[start] = rows
        return self.bike_cache[start]

    def iter_walks_from(self, start, expired=lambda: False):
        """One lookup at a time; each state also receives previously resolved edges.

        A None yield represents one unsuccessful lookup, allowing the scheduler
        to yield control even when calls fail or the API quota is exhausted.
        """
        if not self.network.walk_policy or start == "destination":
            return
        if self.walking is None:
            raise ValueError("지연 도보 연결에는 walking_provider가 필요합니다.")
        source = self.network.nodes[start]
        rows = self.walk_cache.setdefault(start, [])
        cached_lookup = getattr(self.walking, "cached", None)
        priority_lookup = getattr(self.walking, 'peek', cached_lookup)
        if start not in self._walk_candidates:
            candidates = []
            nearby = self.walk_index.near(
                source, self.network.walk_policy["max_distance_m"]
            )
            preferred_radius = self._preferred_walk_radius(start, nearby)
            for d, end in nearby:
                if end in (start, "origin") or (start, end) in self._existing_walks:
                    continue
                target = self.network.nodes[end]
                if source.get("kind") == target.get("kind") == "bike":
                    continue
                hit = False
                if priority_lookup is not None:
                    try:
                        hit, edge = priority_lookup(source, target, start, end)
                    except KakaoError as exc:
                        record_walking_error(self.network.metadata, exc, start, end)
                    if hit:
                        self._cached_walks[start, end] = edge
                if not hit and self.dead_ends.is_dead(end):
                    self.network.metadata["skipped_dead_end_walk_candidates"] = (
                        self.network.metadata.get("skipped_dead_end_walk_candidates", 0)
                        + 1
                    )
                    continue
                candidates.append(
                    (not hit, end != "destination", d > preferred_radius, d, end)
                )
            # Cached edges cost no calls; destination egress comes before local
            # exploration. All directed radius-valid candidates remain present.
            first, groups = [], defaultdict(deque)
            for missed, not_goal, outside_preferred, d, end in sorted(candidates):
                if not missed or not not_goal:
                    first.append((d, end))
                else:
                    kind = self.network.nodes[end].get("kind")
                    group = "rail" if end.startswith("subway:") else kind
                    groups[(outside_preferred, group)].append((d, end))
            tiers = {
                outside: deque(
                    values
                    for (is_outside, _), values in groups.items()
                    if is_outside == outside
                )
                for outside in (False, True)
            }

            def take(outside):
                queues = tiers[outside]
                if not queues:
                    return False
                group = queues.popleft()
                first.append(group.popleft())
                if group:
                    queues.append(group)
                return True

            # Two locally promising calls for every fallback call keeps sparse
            # access discoverable without letting distant candidates dominate.
            while tiers[False] or tiers[True]:
                took = take(False)
                took = take(False) or took
                took = take(True) or took
                if not took:
                    break
            self._walk_candidates[start] = iter(first)
        cursor = 0
        blocked_batch = 0
        while True:
            if cursor < len(rows):
                value = rows[cursor]
                cursor += 1
                yield value
                continue
            if start in self._walk_complete:
                return
            if expired():
                self.network.metadata["connector_limit_reached"] = True
                return
            try:
                d, end = next(self._walk_candidates[start])
            except StopIteration:
                self._walk_complete.add(start)
                return
            if end in (start, "origin") or (start, end) in self._existing_walks:
                continue
            target = self.network.nodes[end]
            # A bicycle leg may connect to any nearby transit platform, not only
            # the closest stop. No zero-time bike->bike rental splitting.
            if source.get("kind") == "bike" and target.get("kind") == "bike":
                continue
            try:
                if (start, end) in self._cached_walks:
                    value = self._cached_walks[start, end]
                elif d < 3:
                    value = {
                        "from": start,
                        "to": end,
                        "minutes": 0.05,
                        "source": "coincident_access_estimate",
                        "geometry": None,
                    }
                else:
                    if self._new_walks_blocked and cached_lookup is not None:
                        hit, value = cached_lookup(source, target, start, end)
                        if not hit:
                            self.network.metadata["skipped_blocked_walk_lookups"] = (
                                self.network.metadata.get(
                                    "skipped_blocked_walk_lookups", 0
                                )
                                + 1
                            )
                            blocked_batch += 1
                            if blocked_batch % 16 == 0:
                                yield None
                            continue
                        if value is not None:
                            rows.append(value)
                            cursor += 1
                        yield value
                        continue
                    self.calls += 1
                    value = self.walking(source, target, start, end)
            except CallLimit as exc:
                self.network.metadata["connector_limit_reached"] = True
                if cached_lookup is not None:
                    self._new_walks_blocked = True
                if isinstance(exc, WalkingDeadline):
                    self.network.metadata["walking_deadline_reached"] = True
                value = None  # Later candidates may still be in the local API cache.
            except KakaoError as exc:
                record_walking_error(self.network.metadata, exc, start, end)
                value = None
            if value is not None:
                rows.append(value)
                cursor += 1
            yield value

    def walks_from(self, start, expired=lambda: False):
        return [
            edge for edge in self.iter_walks_from(start, expired) if edge is not None
        ]
