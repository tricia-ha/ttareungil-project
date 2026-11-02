"""Observable walk-risk conditions; no use of Kakao targets at prediction time."""

import math

RULE_LABELS = {
    "large_snap": "출발·도착 지점의 도로 연결 오차가 큼",
    "station_access": "역 출입구 연결이 불명확함",
    "complex_walk": "꺾이는 지점이 많은 도보",
    "short_shortcut": "짧고 거의 직선인 연결",
    "long_walk": "긴 도보시간 추정",
    "large_detour": "직선거리보다 크게 우회",
    "same_name_bus": "동명 버스 정류장 사이",
}


def features(a, b, start, end, edge):
    if not edge:
        return {}
    snap = edge.get("snap_distance_m", [0, 0])
    coords = (edge.get("geometry") or {}).get("coordinates", [])
    vectors = []
    for p, q in zip(coords, coords[1:]):
        dx = (q[0] - p[0]) * math.cos(math.radians(37.57))
        dy = q[1] - p[1]
        norm = math.hypot(dx, dy)
        if norm > 1e-7:
            vectors.append((dx / norm, dy / norm))
    turns = sum(u[0] * v[0] + u[1] * v[1] < 0.5 for u, v in zip(vectors, vectors[1:]))
    from kakao_routes import distance

    straight = distance(a, b)
    return dict(
        major_road=bool(edge.get("needs_kakao_verification")),
        major_fraction=float(edge.get("major_road_fraction", 0)),
        rail=start.startswith("subway:") or end.startswith("subway:"),
        snap_max_m=float(max(snap)),
        snap_total_m=float(sum(snap)),
        sharp_turns=turns,
        straight_m=straight,
        detour_ratio=float(edge.get("distance_m", 0)) / max(1, straight),
        osm_minutes=float(edge["minutes"]),
        same_name_bus=bool(
            start.startswith("bus:")
            and end.startswith("bus:")
            and a.get("name")
            and a.get("name") == b.get("name")
        ),
    )


def matches(rule, f):
    if not f:
        return False
    return {
        "large_snap": f["snap_max_m"] >= 20,
        "station_access": f["rail"]
        and (f["snap_max_m"] >= 10 or f["sharp_turns"] >= 2),
        "complex_walk": f["sharp_turns"] >= 6,
        "short_shortcut": f["straight_m"] <= 200 and f["detour_ratio"] <= 1.2,
        "long_walk": f["osm_minutes"] >= 10,
        "large_detour": f["detour_ratio"] >= 1.8,
        "same_name_bus": f["same_name_bus"],
    }.get(rule, False)


def reasons(a, b, start, end, edge, extra_rules=()):
    if edge is None:
        return ["osm_unavailable"]
    f = features(a, b, start, end, edge)
    result = ["major_road"] if f["major_road"] else []
    result += [rule for rule in extra_rules if rule in RULE_LABELS and matches(rule, f)]
    return result


def large_error(osm, kakao):
    absolute = abs(osm - kakao)
    ratio = max(osm, kakao) / max(0.05, min(osm, kakao))
    return absolute >= 2 or (absolute >= 1 and ratio >= 1.5)


def learn(records):
    paired = [
        r
        for r in records
        if r.get("osm_minutes") is not None and r.get("kakao_minutes") is not None
    ]
    rate = sum(r["large_error"] for r in paired) / max(1, len(paired))
    table = []
    for rule, label in RULE_LABELS.items():
        group = [r for r in paired if matches(rule, r["features"])]
        errors = sum(r["large_error"] for r in group)
        group_rate = errors / max(1, len(group))
        # Fixed screening criteria, not a fitted claim of statistical significance.
        supported = len(group) >= 5 and group_rate >= 0.5 and group_rate >= rate
        table.append(
            dict(
                rule=rule,
                label=label,
                n=len(group),
                large_errors=errors,
                error_rate=group_rate,
                supported=supported,
            )
        )
    selected = [
        x["rule"]
        for x in sorted(table, key=lambda r: (-r["error_rate"], -r["n"]))
        if x["supported"]
    ][:3]
    return selected, table


def evaluate(records, rules):
    paired = [
        r
        for r in records
        if r.get("osm_minutes") is not None and r.get("kakao_minutes") is not None
    ]
    raw = [abs(r["osm_minutes"] - r["kakao_minutes"]) for r in paired]
    retained = [
        r
        for r in paired
        if not r["features"]["major_road"]
        and not any(matches(rule, r["features"]) for rule in rules)
    ]
    replaced = [r for r in paired if r not in retained]
    missed = [r for r in retained if r["large_error"]]
    return dict(
        selected=len(records),
        paired=len(paired),
        osm_unavailable=sum(r.get("osm_minutes") is None for r in records),
        raw_mae_minutes=sum(raw) / max(1, len(raw)),
        raw_large_errors=sum(r["large_error"] for r in paired),
        kakao_replacement_pairs=len(replaced),
        retained_osm_pairs=len(retained),
        retained_large_errors=len(missed),
        retained_mae_minutes=sum(
            abs(r["osm_minutes"] - r["kakao_minutes"]) for r in retained
        )
        / max(1, len(retained)),
        residual_reference_difference_minutes=sum(
            abs(r["osm_minutes"] - r["kakao_minutes"]) for r in retained
        )
        / max(1, len(paired)),
        note="Replacement uses the Kakao comparison value itself; this is not an independent accuracy score.",
    )
