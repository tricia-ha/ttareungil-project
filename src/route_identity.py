"""Compare paths independently of walking relays and scheduled vehicle instances."""


def route_signature(legs):
    tokens = []
    start = end = None

    def flush():
        nonlocal start, end
        if start is not None and start != end:
            tokens.append(("walk", start, end, None))
        start = end = None

    for leg in legs:
        mode = leg["mode"]
        if mode == "walk":
            if start is not None and end != leg["from"]:
                flush()
            if start is None:
                start = leg["from"]
            end = leg["to"]
        elif mode not in {"wait", "alight"}:
            flush()
            service = leg.get("trip_id")
            if mode in {"bus", "subway"}:
                # Unknown services retain their vehicle ID: endpoints alone
                # cannot establish that two buses belong to the same line.
                service = next(
                    (
                        (key, str(leg[key]))
                        for key in ("route_id", "route_name", "trip_id")
                        if leg.get(key) not in (None, "")
                    ),
                    None,
                )
            elif mode == "transit" and leg.get("route_option") is not None:
                service = ("route_option", str(leg["route_option"]))
            tokens.append((mode, leg["from"], leg["to"], service))
    flush()
    return tuple(tokens)


def walking_minutes(route):
    return sum(
        float(leg.get("minutes", 0)) for leg in route["legs"] if leg["mode"] == "walk"
    )


def distinct_routes(routes):
    representatives = {}
    for route in routes:
        key = route_signature(route["legs"])
        rank = (float(route["total_minutes"]), walking_minutes(route))
        old = representatives.get(key)
        if old is None or rank < (float(old["total_minutes"]), walking_minutes(old)):
            representatives[key] = route
    return sorted(
        representatives.values(), key=lambda route: float(route["total_minutes"])
    )
