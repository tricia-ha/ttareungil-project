"""Scan timetable events for route fallback or separate connector planning.

The default uses confirmed walks only. Optional proposed walks are solely for
query ordering in EndpointPriority; those plans must never be published as real
routes. No candidates are removed from the multimodal search. No API/model calls.
"""

from collections import defaultdict
from datetime import datetime
import heapq
import itertools
import time


def timetable_seed(
    walks,
    rides,
    origin,
    destination,
    start,
    end,
    tz,
    required_mode,
    deadline,
    proposed_walks=None,
):
    begun = time.monotonic()
    walking = defaultdict(list)
    for edge in walks:
        walking[edge["from"]].append(edge)
    # Immutable predecessor chains keep earlier paths intact after improvements.
    # label = (arrival, boardings, walking_minutes, parent_label, incoming_leg)
    off, aboard, expanded_walks = {}, {}, {}
    expired = False
    examined = 0
    counter = itertools.count()

    def iso(when):
        return datetime.fromtimestamp(when, tz).isoformat()

    def rank(label):
        return label[:3]

    def matches(mode):
        return (
            required_mode is None
            or mode == required_mode
            or (required_mode == "transit" and mode in ("bus", "subway"))
        )

    def walk_closure(node, used, label):
        nonlocal expired
        queue = []

        def improve(key, value, expand=True):
            if value[0] <= end and (key not in off or rank(value) < rank(off[key])):
                off[key] = value
            if (
                expand
                and value[0] <= end
                and (
                    key not in expanded_walks or rank(value) < rank(expanded_walks[key])
                )
            ):
                expanded_walks[key] = value
                heapq.heappush(queue, (*rank(value), next(counter), key, value))

        improve((node, used), label)
        while queue:
            if time.monotonic() >= deadline:
                expired = True
                break
            *_, key, value = heapq.heappop(queue)
            if expanded_walks.get(key) is not value:
                continue
            if key[0] == destination:
                continue
            for edge in walking.get(key[0], ()):
                minutes = float(edge["minutes"])
                arrival = value[0] + minutes * 60
                leg = dict(
                    edge,
                    mode="walk",
                    start=iso(value[0]),
                    arrival=iso(arrival),
                    minutes=minutes,
                )
                improve(
                    (edge["to"], key[1]),
                    (arrival, value[1], value[2] + minutes, value, leg),
                )
            if proposed_walks is not None:
                # Priority planning only: one unverified walk between vehicle
                # boardings. Its guessed cost is never returned as a real route.
                for edge in proposed_walks(key[0]):
                    minutes = float(edge["minutes"])
                    arrival = value[0] + minutes * 60
                    leg = dict(
                        edge,
                        mode="walk",
                        start=iso(value[0]),
                        arrival=iso(arrival),
                        minutes=minutes,
                    )
                    improve(
                        (edge["to"], key[1]),
                        (arrival, value[1], value[2] + minutes, value, leg),
                        expand=False,
                    )

    walk_closure(origin, required_mode is None, (start, 0, 0.0, None, None))
    # rides values are already sorted by departure in Router's shared index.
    events = heapq.merge(*rides.values(), key=lambda row: row[0])
    for departure, arrival, edge in events:
        target = off.get((destination, True))
        # All remaining services depart no earlier than an already feasible
        # arrival. Positive movement times cannot improve that seed.
        if target is not None and departure >= target[0]:
            break
        examined += 1
        if examined % 128 == 0 and time.monotonic() >= deadline:
            expired = True
            break
        if expired:
            break
        for used in (False, True):
            candidates = []
            through = aboard.get((edge["from"], edge["trip_id"], used))
            if through is not None and through[0] <= departure + 1e-6:
                candidates.append((through, through[1]))
            # Reaching the destination ends an off-board journey. A vehicle
            # may still pass it without allowing alighting at that event.
            board = (
                off.get((edge["from"], used)) if edge["from"] != destination else None
            )
            if (
                board is not None
                and edge.get("pickup_allowed", True)
                and board[0] + float(edge.get("boarding_seconds", 0))
                <= departure + 1e-6
            ):
                candidates.append((board, board[1] + 1))
            if not candidates:
                continue
            previous, boards = min(candidates, key=lambda v: (v[1], v[0][2]))
            now_used = used or matches(edge["mode"])
            leg = dict(
                edge,
                start=iso(previous[0]),
                departure=iso(departure),
                arrival=iso(arrival),
                wait_minutes=(departure - previous[0]) / 60,
                minutes=(arrival - previous[0]) / 60,
            )
            label = (arrival, boards, previous[2], previous, leg)
            key = (edge["to"], edge["trip_id"], now_used)
            if key not in aboard or rank(label) < rank(aboard[key]):
                aboard[key] = label
            if edge.get("dropoff_allowed", True):
                alight = dict(
                    mode="alight",
                    **{"from": edge["to"], "to": edge["to"]},
                    start=iso(arrival),
                    arrival=iso(arrival),
                    minutes=0,
                )
                walk_closure(
                    edge["to"], now_used, (arrival, boards, label[2], label, alight)
                )
    target = off.get((destination, True))
    route = None
    if target is not None:
        legs, label = [], target
        while label is not None:
            if label[4] is not None:
                legs.append(label[4])
            label = label[3]
        legs.reverse()
        route = dict(
            arrival=iso(target[0]),
            total_minutes=(target[0] - start) / 60,
            score_minutes=(target[0] - start) / 60,
            bike_rentals=0,
            transit_boardings=target[1],
            legs=legs,
            route_kind="transit" if target[1] else "walk",
        )
    return route, dict(
        seconds=time.monotonic() - begun,
        events=examined,
        complete=not expired,
        found=route is not None,
    )
