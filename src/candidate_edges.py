"""Lazy, radius-complete local connectors; no nearest-N station/OD pruning."""

from collections import defaultdict, deque
import math
from multimodal_data import distance
from kakao_routes import CallLimit, KakaoError, WalkingDeadline, record_walking_error


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
                    {
                        "from": start,
                        "to": end,
                        "eta_from": str(source["station_id"]),
                        "eta_to": str(target["station_id"]),
                        "inventory_from": source.get("inventory_station_id"),
                        "straight_distance_m": d,
                    }
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
        if start not in self._walk_candidates:
            candidates = []
            for d, end in self.walk_index.near(
                source, self.network.walk_policy["max_distance_m"]
            ):
                if end in (start, "origin") or (start, end) in self._existing_walks:
                    continue
                target = self.network.nodes[end]
                if source.get("kind") == target.get("kind") == "bike":
                    continue
                hit = False
                if cached_lookup is not None:
                    try:
                        hit, edge = cached_lookup(source, target, start, end)
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
                candidates.append((not hit, end != "destination", d, end))
            # Cached edges cost no calls; destination egress comes before local
            # exploration. All directed radius-valid candidates remain present.
            first, groups = [], defaultdict(deque)
            for missed, not_goal, d, end in sorted(candidates):
                if not missed or not not_goal:
                    first.append((d, end))
                else:
                    kind = self.network.nodes[end].get("kind")
                    group = "rail" if end.startswith("subway:") else kind
                    groups[group].append((d, end))
            queues = deque(groups.values())
            while queues:
                group = queues.popleft()
                first.append(group.popleft())
                if group:
                    queues.append(group)
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
