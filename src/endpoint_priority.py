"""API-free endpoint ordering; these estimates are neither ETAs nor probabilities."""

from collections import defaultdict
from bisect import bisect_left
from heapq import heapify, heappop, heappush
from statistics import median
import math
import time
from datetime import datetime

from multimodal import _time_parser, timestamp
from multimodal_data import distance


def _paths(graph, seeds):
    best = dict(seeds)
    queue = [(value, key) for key, value in best.items()]
    heapify(queue)
    while queue:
        cost, node = heappop(queue)
        if cost != best[node]:
            continue
        for other, minutes in graph.get(node, {}).items():
            value = cost + minutes
            if value < best.get(other, math.inf):
                best[other] = value
                heappush(queue, (value, other))
    return best


class EndpointPriority:
    """Use scheduled trip durations, local geometry and available walking links.

    A preferred endpoint pair includes scheduled waiting when departure is known.
    Other ranks omit waiting and unknown transfers. A disconnected component
    gets a geometric fallback, never a rejection. Walking 80 m/min is the existing
    interchange estimate; 14.5 km/h is the route baseline for bicycle ordering.
    Actual costs, stock forecasts and road geometry remain the router's job.
    """

    def __init__(
        self,
        nodes,
        transit,
        walks,
        groups,
        bike_radius,
        departure=None,
        walk_radius=900,
        horizon_minutes=180,
    ):
        self.nodes = nodes
        self.forward, self.reverse = defaultdict(dict), defaultdict(dict)
        self.outgoing, self.incoming = defaultdict(int), defaultdict(int)
        speeds = defaultdict(list)
        kinds = defaultdict(set)
        lengths = {}
        self.timed = defaultdict(list)
        parse = _time_parser()

        def add(a, b, minutes):
            if (
                a not in nodes
                or b not in nodes
                or not math.isfinite(minutes)
                or minutes <= 0
            ):
                return
            if minutes < self.forward[a].get(b, math.inf):
                self.forward[a][b] = minutes
                self.reverse[b][a] = minutes

        for edge in transit:
            a, b = edge["from"], edge["to"]
            leave, arrive = parse(edge["departure"]), parse(edge["arrival"])
            minutes = (arrive - leave) / 60
            add(a, b, minutes)
            if a not in nodes or b not in nodes or minutes <= 0:
                continue
            if departure is not None:
                self.timed[a].append((leave, arrive, edge))
            self.outgoing[a] += 1
            self.incoming[b] += 1
            kinds[a].add(edge["mode"])
            kinds[b].add(edge["mode"])
            if (a, b) not in lengths:
                lengths[a, b] = distance(nodes[a], nodes[b])
            speed = lengths[a, b] / minutes
            if math.isfinite(speed) and speed > 0:
                speeds[edge["mode"]].append(speed)
        known_walks = {}
        for edge in walks:
            add(edge["from"], edge["to"], float(edge["minutes"]))
            pair, minutes = (edge["from"], edge["to"]), float(edge["minutes"])
            if math.isfinite(minutes) and minutes > 0:
                known_walks[pair] = min(minutes, known_walks.get(pair, math.inf))
        self.speeds = {kind: median(values) for kind, values in speeds.items()}
        self.kinds = kinds
        self.bike_speed = 14500 / 60
        self.walk_speed = 80
        seeds = {"origin": {"origin": 0.0}, "destination": {"destination": 0.0}}
        self.bikes = {"origin": [], "destination": []}
        self.access_costs = {}
        for group in groups:
            for a, b in group:
                side, key = ("origin", b) if a == "origin" else ("destination", a)
                walk = known_walks.get((a, b))
                if walk is None:
                    walk = distance(nodes[side], nodes[key]) / self.walk_speed
                self.access_costs[a, b] = walk
                seeds[side][key] = walk
                if nodes[key].get("kind") == "bike":
                    self.bikes[side].append((key, walk))
        self.prefix = _paths(self.forward, seeds["origin"])
        self.suffix = _paths(self.reverse, seeds["destination"])
        self.bike_radius = bike_radius
        self.walk_radius = walk_radius
        self.horizon_minutes = horizon_minutes
        self.scores = {}
        self.planned_walks = []
        self.planner_stats = {}
        self.preferred = self._preferred_pair(walks, groups, departure)
        transit_walks = list(self.planned_walks)
        mixed_walks, mixed_stats = self._preferred_mixed_journey(
            walks, groups, departure
        )
        self.mixed_planned_walks = mixed_walks
        # Validate one complete mixed skeleton first, then the transit-only
        # skeleton. This prevents an internal rail/bike interchange from being
        # crowded out by many unrelated endpoint links.
        self.planned_walks = list(dict.fromkeys(mixed_walks + transit_walks))
        self.planner_stats = dict(
            transit=self.planner_stats,
            mixed=mixed_stats,
        )

    def _preferred_mixed_journey(self, walks, groups, departure):
        """Find one complete transit+bicycle skeleton without buying API calls.

        This is a query planner, not a route result. Unknown walks and bicycle
        times are optimistic geometric estimates used only to decide which
        walking links to verify first. The main router still retains and scores
        every radius-valid candidate using confirmed walks, schedules, stock
        and the bicycle model.
        """
        if departure is None or not self.timed:
            return [], {"found": False, "reason": "no_departure_or_transit"}
        from candidate_edges import SpatialIndex

        start = timestamp(departure)
        end = start + self.horizon_minutes * 60
        endpoint_pairs = {pair for group in groups for pair in group}
        known = defaultdict(list)
        known_pairs = set()
        for edge in walks:
            a, b = edge["from"], edge["to"]
            if a in self.nodes and b in self.nodes:
                known[a].append((b, float(edge["minutes"]), False))
                known_pairs.add((a, b))

        searchable = {
            key: value
            for key, value in self.nodes.items()
            if key in ("origin", "destination")
            or value.get("kind") in ("stop", "bike")
        }
        walk_index = SpatialIndex(searchable)
        bikes = {
            key: value for key, value in searchable.items() if value.get("kind") == "bike"
        }
        if len(bikes) < 2:
            return [], {"found": False, "reason": "insufficient_bicycle_stations"}
        bike_index = SpatialIndex(bikes)
        walk_cache, bike_cache = {}, {}

        def virtual_walks(node):
            if node in walk_cache:
                return walk_cache[node]
            source = self.nodes[node]
            rows = []
            for length, other in walk_index.near(source, self.walk_radius):
                pair = (node, other)
                if other in (node, "origin") or pair in known_pairs:
                    continue
                if source.get("kind") == self.nodes[other].get("kind") == "bike":
                    continue
                if node == "origin" or other == "destination":
                    if pair not in endpoint_pairs:
                        continue
                rows.append((other, max(0.05, length / self.walk_speed), True))
            # Planning remains bounded, but preserves destination access and a
            # spatially diverse set per mode. No candidate is removed from the
            # real optimizer by this ordering-only cap.
            selected = []
            by_kind = defaultdict(list)
            for row in rows:
                other = row[0]
                kind = "destination" if other == "destination" else self.nodes[other].get("kind")
                by_kind[kind].append(row)
            for kind in sorted(by_kind):
                ordered = sorted(
                    by_kind[kind],
                    key=lambda row: (
                        distance(self.nodes[row[0]], self.nodes["destination"]),
                        row[1],
                        row[0],
                    ),
                )
                selected.extend(ordered[:8])
            walk_cache[node] = known.get(node, []) + selected
            return walk_cache[node]

        def bicycle_edges(node):
            if node in bike_cache:
                return bike_cache[node]
            if node not in bikes:
                return []
            rows = [
                (other, length / self.bike_speed)
                for length, other in bike_index.near(self.nodes[node], self.bike_radius)
                if other != node
            ]
            rows.sort(
                key=lambda row: (
                    distance(self.nodes[row[0]], self.nodes["destination"]),
                    row[1],
                    row[0],
                )
            )
            bike_cache[node] = rows[:32]
            return bike_cache[node]

        for rows in self.timed.values():
            rows.sort(key=lambda row: row[0])
        queue = [(start, "origin", 0)]
        best = {("origin", 0): start}
        parent = {}
        expanded = 0
        deadline = time.monotonic() + 1.5
        goal = None
        while queue and time.monotonic() < deadline and expanded < 30000:
            at, node, used = heappop(queue)
            if at != best.get((node, used)):
                continue
            expanded += 1
            if node == "destination" and used == 3:
                goal = (node, used)
                break

            def submit(other, arrival, mask, action):
                if arrival > end:
                    return
                key = (other, mask)
                if arrival + 1e-6 < best.get(key, math.inf):
                    best[key] = arrival
                    parent[key] = ((node, used), action)
                    heappush(queue, (arrival, other, mask))

            for other, minutes, unverified in virtual_walks(node):
                submit(
                    other,
                    at + minutes * 60,
                    used,
                    ("walk", node, other, unverified),
                )
            rows = self.timed.get(node, ())
            if rows:
                index = bisect_left([row[0] for row in rows], at - 1e-6)
                for leave, arrive, edge in rows[index:]:
                    submit(edge["to"], arrive, used | 2, ("transit", node, edge["to"], False))
            for other, minutes in bicycle_edges(node):
                submit(other, at + minutes * 60, used | 1, ("bicycle", node, other, False))

        if goal is None:
            return [], {
                "found": False,
                "expanded": expanded,
                "timed_out": time.monotonic() >= deadline,
            }
        actions = []
        cursor = goal
        while cursor != ("origin", 0):
            previous, action = parent[cursor]
            actions.append(action)
            cursor = previous
        actions.reverse()
        planned = [
            (a, b)
            for mode, a, b, unverified in actions
            if mode == "walk" and unverified
        ]
        return planned, {
            "found": True,
            "expanded": expanded,
            "estimated_minutes": (best[goal] - start) / 60,
            "unverified_walks": len(planned),
            "actions": len(actions),
        }

    def _preferred_pair(self, walks, groups, departure):
        """Pair both endpoints using a time-aware optimistic no-API journey.

        Virtual access legs use straight walking estimates. Scheduled departure
        and arrival times include waiting. Nearby unverified transfer walks may
        guide queries but never enter the actual movement graph as estimates.
        """
        if departure is None:
            return set()
        from candidate_edges import SpatialIndex
        from transit_seed import timetable_seed

        start = timestamp(departure)
        endpoint_pairs = {pair for group in groups for pair in group}
        known_pairs = {(e["from"], e["to"]) for e in walks}
        index = SpatialIndex(
            {
                k: v
                for k, v in self.nodes.items()
                if k in self.kinds or k == "destination"
            }
        )
        proposed = {}

        def walking_candidates(key):
            if key not in proposed:
                rows = []
                source = self.nodes[key]
                for length, other in index.near(source, self.walk_radius):
                    pair = (key, other)
                    if other in (key, "origin") or pair in known_pairs:
                        continue
                    if (
                        key == "origin" or other == "destination"
                    ) and pair not in endpoint_pairs:
                        continue
                    rows.append(
                        {
                            "from": key,
                            "to": other,
                            "minutes": max(0.05, length / self.walk_speed),
                            "source": "unverified_walk_priority",
                        }
                    )
                proposed[key] = rows
            return proposed[key]

        for rows in self.timed.values():
            rows.sort(key=lambda row: row[0])
        end = start + self.horizon_minutes * 60
        # These estimates buy an actual connected journey, including an unknown
        # bus/rail transfer walk. They never become movement times or prune the
        # full optimizer's candidates. The final router uses confirmed links.
        route, self.planner_stats = timetable_seed(
            walks,
            self.timed,
            "origin",
            "destination",
            start,
            end,
            datetime.fromisoformat(departure).tzinfo
            if isinstance(departure, str)
            else departure.tzinfo,
            "transit",
            time.monotonic() + 2.0,
            proposed_walks=walking_candidates,
        )
        preferred = set()
        best, pair = math.inf, None
        if route is not None:
            self.planned_walks = [
                (e["from"], e["to"])
                for e in route["legs"]
                if e["mode"] == "walk" and e.get("source") == "unverified_walk_priority"
            ]
            preferred.update(p for p in self.planned_walks if p in endpoint_pairs)
            best = start + route["total_minutes"] * 60
        for a, access in self.bikes["origin"]:
            for b, egress in self.bikes["destination"]:
                length = distance(self.nodes[a], self.nodes[b])
                value = start + (access + length / self.bike_speed + egress) * 60
                if a != b and length <= self.bike_radius and value < best:
                    best, pair = value, (("origin", a), (b, "destination"))
        return preferred | set(pair or ())

    def score(self, pair):
        if pair in self.scores:
            return self.scores[pair]
        a, b = pair
        side, key, opposite = (
            ("origin", b, "destination")
            if a == "origin"
            else ("destination", a, "origin")
        )
        node = self.nodes[key]
        walk_m = distance(self.nodes[side], node)
        walk_min = self.access_costs[pair]
        continuation = self.suffix if side == "origin" else self.prefix
        speed = max(
            (self.speeds.get(kind, self.walk_speed) for kind in self.kinds[key]),
            default=self.walk_speed,
        )
        if node.get("kind") == "bike":
            speed = self.bike_speed
        fallback = walk_min + distance(node, self.nodes[opposite]) / speed
        estimate = walk_min + continuation.get(key, math.inf)
        if node.get("kind") == "bike":
            for other, exit_walk in self.bikes[opposite]:
                length = distance(node, self.nodes[other])
                if key != other and length <= self.bike_radius:
                    estimate = min(
                        estimate, walk_min + length / self.bike_speed + exit_walk
                    )
        if not math.isfinite(estimate):
            estimate = fallback
        options = self.outgoing[key] if side == "origin" else self.incoming[key]
        result = (pair not in self.preferred, estimate, walk_m, -options, key)
        self.scores[pair] = result
        return result

    def ordered(self, groups):
        """Every mode gets turns; origin and destination alternate within a round."""
        groups = [sorted(group, key=self.score) for group in groups]
        for rank in range(max(map(len, groups), default=0)):
            heads = [group[rank] for group in groups if rank < len(group)]
            origins = sorted(
                (pair for pair in heads if pair[0] == "origin"), key=self.score
            )
            destinations = sorted(
                (pair for pair in heads if pair[1] == "destination"), key=self.score
            )
            for i in range(max(len(origins), len(destinations))):
                if i < len(origins):
                    yield origins[i]
                if i < len(destinations):
                    yield destinations[i]
