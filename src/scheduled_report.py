"""Small route tables for the notebook and web integration."""

MODES = {
    "walk": "도보",
    "bicycle": "따릉이",
    "bus": "버스",
    "subway": "지하철",
    "wait": "대기",
}


def route_segments(result, rank=0):
    nodes = result.get("nodes", {})

    def name(key):
        node = nodes.get(key, {})
        return node.get("name") or node.get("station_name") or key

    rows = []
    for leg in result["routes"][rank]["legs"]:
        mode = leg["mode"]
        if mode not in MODES:
            continue
        merge = bool(
            rows
            and (
                (mode in ("bus", "subway") and rows[-1]["운행편"] == leg.get("trip_id"))
                or (
                    mode == "wait"
                    and rows[-1]["이동수단"] == "대기"
                    and rows[-1]["도착"] == name(leg["from"])
                )
            )
        )
        if merge:
            rows[-1]["도착"] = name(leg["to"])
            rows[-1]["도착시각"] = leg["arrival"]
            rows[-1]["구간시간(분)"] += leg["minutes"]
        else:
            rows.append(
                {
                    "이동수단": MODES[mode],
                    "노선": leg.get("route_name", ""),
                    "출발": name(leg["from"]),
                    "도착": name(leg["to"]),
                    "출발시각": leg.get("departure", leg.get("start")),
                    "도착시각": leg["arrival"],
                    "구간시간(분)": leg["minutes"],
                    "탑승대기(분)": leg.get("wait_minutes", 0),
                    "대여불가확률": (leg.get("details") or {}).get("p_empty"),
                    "운행편": leg.get("trip_id"),
                }
            )
    for row in rows:
        for col in ("구간시간(분)", "탑승대기(분)"):
            row[col] = round(row[col], 2)
    return rows


def route_summary(result):
    return [
        {
            "순위": i + 1,
            "총시간(분)": round(route["total_minutes"], 1),
            "따릉이횟수": route["bike_rentals"],
            "대중교통탑승": route["transit_boardings"],
            "이동수단": " → ".join(
                r["이동수단"]
                for r in route_segments(result, i)
                if r["이동수단"] != "대기"
            ),
            "도착시각": route["arrival"],
        }
        for i, route in enumerate(result["routes"])
    ]
