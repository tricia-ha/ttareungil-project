"""Consolidate ordinary consecutive walks without hiding real transfers."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from kakao_routes import CallLimit, KakaoError
from route_identity import walking_minutes


def _timestamp(value):
    return datetime.fromisoformat(value).timestamp()


def _iso(value, template):
    return datetime.fromtimestamp(value, datetime.fromisoformat(template).tzinfo).isoformat()


def _special(leg):
    source = str(leg.get("source", "")).lower()
    return bool(
        leg.get("platform_transfer")
        or leg.get("platform_access_complete")
        or any(token in source for token in ("rail", "transfer", "station"))
    )


def _geometry(legs):
    coordinates = []
    for leg in legs:
        geometry = leg.get("geometry")
        if not geometry or geometry.get("type") != "LineString":
            return None
        values = geometry.get("coordinates") or []
        if coordinates and values and coordinates[-1] == values[0]:
            values = values[1:]
        coordinates.extend(values)
    return {"type": "LineString", "coordinates": coordinates} if coordinates else None


def _combined(legs):
    first, last = legs[0], legs[-1]
    minutes = sum(float(leg["minutes"]) for leg in legs)
    value = {
        "mode": "walk",
        "from": first["from"],
        "to": last["to"],
        "start": first["start"],
        "arrival": last["arrival"],
        "minutes": minutes,
        "modeled_arrival": last.get("modeled_arrival", last["arrival"]),
        "modeled_minutes": minutes,
        "grid_buffer_seconds": sum(
            float(leg.get("grid_buffer_seconds", 0)) for leg in legs
        ),
        "source": "combined_walk",
        "via": [leg["to"] for leg in legs[:-1]],
        "combined_sources": [leg.get("source") for leg in legs],
        "combined_segments": len(legs),
    }
    distances = [leg.get("distance_m") for leg in legs]
    if all(value is not None for value in distances):
        value["distance_m"] = sum(float(distance) for distance in distances)
    geometry = _geometry(legs)
    if geometry:
        value["geometry"] = geometry
    return value


def _wait_for_saved_time(direct, original_arrival):
    arrival = _timestamp(original_arrival)
    start = _timestamp(direct["arrival"])
    if arrival <= start + 1e-6:
        return None
    return {
        "mode": "wait",
        "from": direct["to"],
        "to": direct["to"],
        "start": direct["arrival"],
        "arrival": original_arrival,
        "minutes": (arrival - start) / 60,
        "source": "walk_relay_replaced_by_direct_wait",
    }


def _direct(run, nodes, walking, allow_new, attempted):
    if walking is None:
        return None, False
    first, last = run[0], run[-1]
    start, end = first["from"], last["to"]
    if start == end:
        return None, False
    source, target = nodes.get(start), nodes.get(end)
    if not source or not target:
        return None, False
    cached = getattr(walking, "cached", None)
    try:
        hit, edge = (cached(source, target, start, end) if cached else (False, None))
    except KakaoError:
        hit, edge = False, None
    called = False
    # New calls are reserved for a final destination walk where a shorter
    # direct route can improve the displayed ETA. Search discovery is already
    # complete, so this cannot consume calls needed to find another path.
    if not hit and allow_new and end == "destination" and (start, end) not in attempted:
        attempted.add((start, end))
        called = True
        try:
            edge = walking(source, target, start, end)
        except (CallLimit, KakaoError):
            edge = None
    if edge is None:
        return None, called
    old_minutes = sum(float(leg["minutes"]) for leg in run)
    if float(edge["minutes"]) >= old_minutes - 1e-9:
        return None, called
    direct = dict(edge)
    direct.update(
        mode="walk",
        start=first["start"],
        arrival=_iso(
            _timestamp(first["start"]) + float(edge["minutes"]) * 60,
            first["start"],
        ),
        modeled_minutes=float(edge["minutes"]),
        grid_buffer_seconds=0.0,
        relay_segments_replaced=len(run),
    )
    direct["modeled_arrival"] = direct["arrival"]
    return direct, called


def consolidate_route(route, nodes, walking=None, allow_new=False, attempted=None):
    """Return a copy with ordinary walk relays collapsed.

    Published rail/station transfers remain separate. A cached direct walk may
    replace an ordinary relay. Before a later timed leg, saved time becomes an
    explicit wait so an already evaluated transit or bicycle prediction is not
    silently moved to another departure time.
    """
    attempted = attempted if attempted is not None else set()
    legs = deepcopy(route.get("legs", []))
    output, merged, direct_replacements, new_calls = [], 0, 0, 0
    index = 0
    while index < len(legs):
        if legs[index].get("mode") != "walk" or _special(legs[index]):
            output.append(legs[index])
            index += 1
            continue
        end = index + 1
        while (
            end < len(legs)
            and legs[end].get("mode") == "walk"
            and not _special(legs[end])
            and legs[end - 1].get("to") == legs[end].get("from")
        ):
            end += 1
        run = legs[index:end]
        if len(run) == 1:
            output.append(run[0])
            index = end
            continue
        merged += len(run) - 1
        if run[0]["from"] == run[-1]["to"]:
            output.append(
                {
                    "mode": "wait",
                    "from": run[0]["from"],
                    "to": run[0]["from"],
                    "start": run[0]["start"],
                    "arrival": run[-1]["arrival"],
                    "minutes": sum(float(leg["minutes"]) for leg in run),
                    "source": "walk_relay_roundtrip_replaced_by_wait",
                }
            )
            index = end
            continue
        direct, called = _direct(run, nodes, walking, allow_new, attempted)
        new_calls += int(called)
        if direct is None:
            output.append(_combined(run))
        else:
            direct_replacements += 1
            output.append(direct)
            if end < len(legs):
                wait = _wait_for_saved_time(direct, run[-1]["arrival"])
                if wait:
                    output.append(wait)
        index = end
    value = deepcopy(route)
    old_total = float(value.get("total_minutes", 0))
    old_score = float(value.get("score_minutes", old_total))
    value["legs"] = output
    if output and output[-1].get("to") == "destination":
        new_arrival = output[-1]["arrival"]
        start = _timestamp(legs[0]["start"]) if legs else _timestamp(new_arrival)
        new_total = (_timestamp(new_arrival) - start) / 60
        value["arrival"] = new_arrival
        value["total_minutes"] = new_total
        value["score_minutes"] = new_total + (old_score - old_total)
    return value, {
        "merged_walk_legs": merged,
        "direct_replacements": direct_replacements,
        "new_direct_calls": new_calls,
    }


def consolidate_result(result, nodes, walking=None, allow_new=False):
    value = deepcopy(result)
    attempted, totals = set(), {
        "merged_walk_legs": 0,
        "direct_replacements": 0,
        "new_direct_calls": 0,
    }
    for group in ("routes", "mixed_routes"):
        rows = []
        for route in value.get(group, []):
            consolidated, stats = consolidate_route(
                route, nodes, walking, allow_new, attempted
            )
            rows.append(consolidated)
            for key in totals:
                totals[key] += stats[key]
        rows.sort(
            key=lambda route: (
                float(route.get("score_minutes", route["total_minutes"])),
                int(route.get("transit_boardings", 0)),
                float(route["total_minutes"]),
                walking_minutes(route),
            )
        )
        value[group] = rows
    value["walk_consolidation"] = totals
    return value
