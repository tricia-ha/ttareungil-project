"""Adapters for a dated, scheduled GTFS feed and a separate OSM pedestrian graph."""

import csv
import io
import math
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from multimodal import Network, timestamp

KST = timezone(timedelta(hours=9))


def load_gtfs(path, departure, horizon_minutes=180):
    """Strict scheduled-feed subset; unsupported semantics raise, never guess.

    Supports calendar exceptions, >24h times, prior service day, pickup/dropoff
    prohibitions. Frequency services and transfer/pathway rules require a richer
    importer. Do not delete those files merely to bypass this guard.
    """
    start = datetime.fromisoformat(departure).astimezone(KST)
    timestamp(departure)
    finish = start + timedelta(minutes=horizon_minutes)
    with zipfile.ZipFile(path) as archive:
        names = {}
        for name in archive.namelist():
            base = Path(name).name
            if base in names:
                raise ValueError("GTFS ZIP의 파일 이름 중복: " + base)
            names[base] = name

        def rows(name):
            if name not in names:
                return []
            with archive.open(names[name]) as f:
                return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

        for name in ("frequencies.txt", "transfers.txt", "pathways.txt"):
            if rows(name):
                raise ValueError(
                    name
                    + " 규칙을 지원하는 추가 변환이 필요합니다. 임의로 무시하지 않습니다."
                )
        stops, trips, routes = rows("stops.txt"), rows("trips.txt"), rows("routes.txt")
        calendars, exceptions = rows("calendar.txt"), rows("calendar_dates.txt")
        agency = rows("agency.txt")
        if any(a.get("agency_timezone") != "Asia/Seoul" for a in agency):
            raise ValueError("Asia/Seoul GTFS만 지원합니다.")
        if not stops or not trips or not routes or not (calendars or exceptions):
            raise ValueError("GTFS 정류장/노선/운행일 파일이 누락되었습니다.")
        route_map = {r["route_id"]: r for r in routes}
        nodes = {
            "stop:" + s["stop_id"]: dict(
                name=s["stop_name"],
                kind="stop",
                latitude=float(s["stop_lat"]),
                longitude=float(s["stop_lon"]),
            )
            for s in stops
            if s.get("location_type", "0") in ("", "0")
        }
        by_trip = {}
        for row in rows("stop_times.txt"):
            if row.get("pickup_type", "0") not in ("", "0", "1") or row.get(
                "drop_off_type", "0"
            ) not in ("", "0", "1"):
                raise ValueError("예약 승하차는 지원하지 않습니다.")
            by_trip.setdefault(row["trip_id"], []).append(row)

    def seconds(text):
        h, m, s = map(int, text.split(":"))
        if h < 0 or not 0 <= m < 60 or not 0 <= s < 60:
            raise ValueError("잘못된 GTFS 시각")
        return h * 3600 + m * 60 + s

    # Enough preceding service days even for unusually large GTFS hour values.
    max_seconds = max(
        (seconds(r["departure_time"]) for rs in by_trip.values() for r in rs), default=0
    )
    day = start.date() - timedelta(days=max_seconds // 86400)
    transit = []
    active_any = False
    weekdays = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
    while day <= finish.date():
        date_key = day.strftime("%Y%m%d")
        active = {
            r["service_id"]
            for r in calendars
            if r["start_date"] <= date_key <= r["end_date"]
            and r[weekdays[day.weekday()]] == "1"
        }
        for r in exceptions:
            if r["date"] == date_key:
                if r["exception_type"] == "1":
                    active.add(r["service_id"])
                elif r["exception_type"] == "2":
                    active.discard(r["service_id"])
        active_any |= bool(active)
        midnight = datetime.combine(day, datetime.min.time(), KST)
        for trip in trips:
            if trip["service_id"] not in active:
                continue
            route = route_map[trip["route_id"]]
            if route["route_type"] not in ("1", "3"):
                continue
            seq = sorted(
                by_trip.get(trip["trip_id"], []), key=lambda r: int(r["stop_sequence"])
            )
            for a, b in zip(seq, seq[1:]):
                dep = midnight + timedelta(seconds=seconds(a["departure_time"]))
                arr = midnight + timedelta(seconds=seconds(b["arrival_time"]))
                if dep < start or arr > finish:
                    continue
                if arr <= dep:
                    raise ValueError("0초/역전 운행 구간: 원본 시각표 확인 필요")
                transit.append(
                    dict(
                        **{
                            "from": "stop:" + a["stop_id"],
                            "to": "stop:" + b["stop_id"],
                        },
                        departure=dep.isoformat(),
                        arrival=arr.isoformat(),
                        trip_id=date_key + ":" + trip["trip_id"],
                        route_id=trip["route_id"],
                        route_name=route.get("route_short_name")
                        or route.get("route_long_name"),
                        mode="subway" if route["route_type"] == "1" else "bus",
                        pickup_allowed=a.get("pickup_type") != "1",
                        dropoff_allowed=b.get("drop_off_type") != "1",
                    )
                )
        day += timedelta(days=1)
    if not active_any or not transit:
        raise ValueError(
            "요청 날짜/시간의 버스·지하철 운행이 없습니다. 과거 시각표의 날짜를 바꿔 사용하지 마세요."
        )
    return Network(
        nodes=nodes,
        transit=transit,
        metadata=dict(
            transit_source=str(Path(path).name),
            transit_type="scheduled_gtfs_not_realtime",
            valid_departure=departure,
            valid_horizon_minutes=horizon_minutes,
            transfer_rules="none_in_feed",
        ),
    )


def distance(a, b):
    lat1, lat2 = map(math.radians, (a["latitude"], b["latitude"]))
    dlat = lat2 - lat1
    dlon = math.radians(b["longitude"] - a["longitude"])
    return (
        6371000
        * 2
        * math.asin(
            min(
                1,
                math.sqrt(
                    math.sin(dlat / 2) ** 2
                    + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
                ),
            )
        )
    )


class OSMWalking:
    """Use a walk-specific GraphML, not the ETA bicycle graph."""

    def __init__(self, graphml, speed_mps=1.2, max_snap_m=100):
        import osmnx as ox

        if speed_mps <= 0:
            raise ValueError("도보 속도는 양수입니다.")
        self.ox = ox
        self.graph = ox.load_graphml(graphml)
        self.speed, self.max_snap = speed_mps, max_snap_m
        self.cache = {}

    def __call__(self, a, b):
        import networkx as nx

        key = (a["latitude"], a["longitude"], b["latitude"], b["longitude"])
        if key in self.cache:
            return self.cache[key]
        source, ds = self.ox.distance.nearest_nodes(
            self.graph, a["longitude"], a["latitude"], return_dist=True
        )
        target, de = self.ox.distance.nearest_nodes(
            self.graph, b["longitude"], b["latitude"], return_dist=True
        )
        if max(ds, de) > self.max_snap:
            return None
        try:
            path = nx.shortest_path(self.graph, source, target, weight="length")
        except nx.NetworkXNoPath:
            return None
        length = sum(
            min(float(v["length"]) for v in self.graph[u][v].values())
            for u, v in zip(path, path[1:])
        )
        result = dict(
            minutes=max(0.001, (length + ds + de) / self.speed / 60),
            geometry=[[a["longitude"], a["latitude"]]]
            + [[self.graph.nodes[n]["x"], self.graph.nodes[n]["y"]] for n in path]
            + [[b["longitude"], b["latitude"]]],
            source="osm_walk",
            snap_distance_m=[float(ds), float(de)],
        )
        self.cache[key] = result
        return result


def connect(
    network,
    stations,
    origin,
    destination,
    walking,
    walk_radius_m=800,
    bike_radius_m=5000,
    walk_neighbors=8,
    bike_neighbors=12,
):
    """Build a spatial candidate graph. Radius/neighbour caps are explicit approximations.

    stations = [{eta_id, inventory_id(optional), name, latitude, longitude}].
    Walking provider returns None for inaccessible pairs. Edges are directed.
    """
    from sklearn.neighbors import BallTree
    import numpy as np

    nodes = dict(network.nodes)
    nodes.update(
        origin=dict(origin, kind="endpoint"),
        destination=dict(destination, kind="endpoint"),
    )
    for s in stations:
        nodes["bike:" + str(s["eta_id"])] = dict(s, kind="bike")
    walks, bikes = [], []
    ids = list(nodes)
    coords = np.radians([[nodes[i]["latitude"], nodes[i]["longitude"]] for i in ids])
    tree = BallTree(coords, metric="haversine")
    for i, key in enumerate(ids):
        near, distances = tree.query_radius(
            coords[i : i + 1],
            r=walk_radius_m / 6371000,
            return_distance=True,
            sort_results=True,
        )
        candidates = [ids[j] for j in near[0] if ids[j] != key]
        if walk_neighbors is not None:
            candidates = candidates[:walk_neighbors]
        if key == "origin" and "destination" not in candidates:
            candidates.append("destination")
        for other in candidates:
            value = walking(nodes[key], nodes[other])
            if value is not None:
                walks.append(dict(value, **{"from": key, "to": other}))
    bike_ids = [n for n in nodes if nodes[n]["kind"] == "bike"]
    if bike_ids:
        bc = np.radians(
            [[nodes[i]["latitude"], nodes[i]["longitude"]] for i in bike_ids]
        )
        bt = BallTree(bc, metric="haversine")
        for i, key in enumerate(bike_ids):
            near = bt.query_radius(
                bc[i : i + 1],
                r=bike_radius_m / 6371000,
                return_distance=True,
                sort_results=True,
            )[0][0]
            candidates = [bike_ids[j] for j in near if bike_ids[j] != key]
            if bike_neighbors is not None:
                candidates = candidates[:bike_neighbors]
            for other in candidates:
                bikes.append(
                    dict(
                        **{"from": key, "to": other},
                        eta_from=str(nodes[key]["eta_id"]),
                        eta_to=str(nodes[other]["eta_id"]),
                        inventory_from=nodes[key].get("inventory_id"),
                    )
                )
    return Network(
        nodes,
        walks,
        bikes,
        network.transit,
        dict(
            network.metadata,
            walk_radius_m=walk_radius_m,
            bike_radius_m=bike_radius_m,
            walk_neighbors=walk_neighbors,
            bike_neighbors=bike_neighbors,
            approximate_candidate_graph=True,
        ),
    )
