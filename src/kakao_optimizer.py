"""Budgeted candidate routing: Kakao walk/transit + V5.2.1 bicycle ETA."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import heapq
import itertools
import json
import math
from pathlib import Path
import time

from kakao_routes import (
    KakaoError,
    CallLimit,
    point,
    distance,
    walking_edge,
    transit_edges,
)
from multimodal import timestamp


@dataclass
class KakaoConfig:
    max_stations: int = 8
    stations_per_anchor: int = 2
    station_radius_m: float = 1200
    max_anchors: int = 8
    max_transit_queries: int = 10
    max_walk_queries: int = 14
    walk_radius_m: float = 900
    bike_radius_m: float = 6000
    bike_neighbors: int = 3
    max_bike_predictions: int = 32
    horizon_minutes: float = 180
    max_search_seconds: float = 45
    max_expansions: int = 20000
    labels_per_node: int = 12
    top_k: int = 3
    max_station_transfer_walks: int = 4
    require_known_inventory: bool = False
    max_empty_probability: float | None = None
    bike_quantile: str = "p50"

    def validate(self):
        if self.bike_quantile not in ("p50", "p80", "p90"):
            raise ValueError("bike_quantile은 p50/p80/p90입니다.")
        for k, v in vars(self).items():
            if k in (
                "require_known_inventory",
                "max_empty_probability",
                "bike_quantile",
            ):
                continue
            if not math.isfinite(float(v)) or v <= 0:
                raise ValueError(f"{k}는 양수여야 합니다.")
        if (
            self.max_empty_probability is not None
            and not 0 <= self.max_empty_probability <= 1
        ):
            raise ValueError("품절 확률 한도는 0~1입니다.")


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def build_network(
    client, stations, origin, destination, config=None, progress=print, checkpoint=None
):
    """Full API routes remain whole edges, preserving their access and transfers.

    Station candidates come from endpoints and seed-transit boarding/alighting
    locations. Other station pairs are queried in round-robin order so first,
    last and middle bicycle usage all receive a share of the small budget.
    """
    c = config or KakaoConfig()
    c.validate()
    origin, destination = point(origin), point(destination)
    nodes = dict(origin=origin, destination=destination)
    edges, errors, queried = [], [], set()
    counts = dict(walk=0, publictraffic=0)

    def fetch(kind, a, b):
        key = kind, a, b
        limit = c.max_walk_queries if kind == "walk" else c.max_transit_queries
        if key in queried or counts[kind] >= limit:
            return []
        queried.add(key)
        counts[kind] += 1
        try:
            payload, at = client.get(kind, nodes[a], nodes[b])
            if kind == "walk":
                edge = walking_edge(payload, at, a, b)
                found = [edge] if edge else []
            else:
                found = transit_edges(payload, at, a, b)
            edges.extend(found)
            return found
        except (CallLimit, KakaoError) as exc:
            # Authentication/quota failures stop preparation immediately. Each
            # earlier success is already committed in the client checkpoint.
            if client.stopped:
                raise
            errors.append(dict(kind=kind, start=a, end=b, message=str(exc)))
            if progress:
                progress(str(exc), flush=True)
            return []

    if progress:
        progress("1/3 출발지→목적지의 도보·대중교통 후보 조회", flush=True)
    seed = fetch("publictraffic", "origin", "destination")
    fetch("walk", "origin", "destination")
    anchors = [origin, destination]
    for edge in seed:
        for step in edge["steps"]:
            coords = step["geometry"]["coordinates"]
            if step["mode"] in ("bus", "subway") and coords:
                for xy in (coords[0], coords[-1]):
                    candidate = point(dict(latitude=xy[1], longitude=xy[0]))
                    if (
                        all(distance(candidate, old) > 150 for old in anchors)
                        and len(anchors) < c.max_anchors
                    ):
                        anchors.append(candidate)
    # Accept a DataFrame or list of model station records. Never invent IDs.
    rows = (
        stations.to_dict("records") if hasattr(stations, "to_dict") else list(stations)
    )
    available = []
    for row in rows:
        active = row.get("active", True)
        if str(active).lower() in ("false", "0", "inactive", "n"):
            continue
        p = point(row)
        sid = str(row["station_id"])
        if sid.endswith(".0"):
            sid = sid[:-2]
        available.append(
            dict(
                p,
                station_id=sid,
                name=str(row.get("station_name", row.get("name", sid))),
            )
        )
    nearest = [
        sorted(((distance(anchor, row), row) for row in available), key=lambda x: x[0])
        for anchor in anchors
    ]
    # Round-robin prevents a single transit alternative consuming all stations.
    for rank in range(c.stations_per_anchor):
        for group in nearest:
            if rank < len(group) and group[rank][0] <= c.station_radius_m:
                row = group[rank][1]
                key = "bike:" + row["station_id"]
                if key not in nodes and len(nodes) - 2 < c.max_stations:
                    nodes[key] = row
    bike_nodes = [k for k in nodes if k.startswith("bike:")]
    if progress:
        progress(f"2/3 후보 대여소 {len(bike_nodes)}개 연결", flush=True)
    # Bike endpoints use actual OSM routes only when the ETA provider runs.
    for a in bike_nodes:
        neighbors = sorted(
            (b for b in bike_nodes if b != a),
            key=lambda b: distance(nodes[a], nodes[b]),
        )
        for b in neighbors[: c.bike_neighbors]:
            if distance(nodes[a], nodes[b]) <= c.bike_radius_m:
                edges.append(
                    dict(
                        mode="bicycle",
                        **{"from": a, "to": b},
                        eta_from=nodes[a]["station_id"],
                        eta_to=nodes[b]["station_id"],
                    )
                )
    # Forward and reverse walks are separate API calls. Direction is not copied.
    first = sorted(bike_nodes, key=lambda k: distance(origin, nodes[k]))
    last = sorted(bike_nodes, key=lambda k: distance(destination, nodes[k]))
    walks = []
    for a, b in itertools.zip_longest(first, last):
        if a is not None:
            walks.append(("origin", a))
        if b is not None:
            walks.append((b, "destination"))
    transfer_walks = sorted(
        ((a, b) for a in bike_nodes for b in bike_nodes if a != b),
        key=lambda ab: distance(nodes[ab[0]], nodes[ab[1]]),
    )
    walks += transfer_walks[: c.max_station_transfer_walks]
    for a, b in walks:
        if distance(nodes[a], nodes[b]) <= c.walk_radius_m:
            fetch("walk", a, b)
    # Endpoint-near station groups plus intermediate station pairs. A transit
    # macro contains its own walks, so no additional access time is added.
    n = min(3, len(bike_nodes))
    groups = [
        iter((a, "destination") for a in first[:n]),
        iter(("origin", b) for b in last[:n]),
        iter((a, b) for a, b in itertools.product(first[:n], last[:n]) if a != b),
        iter(
            sorted(
                ((a, b) for a in bike_nodes for b in bike_nodes if a != b),
                key=lambda ab: distance(origin, nodes[ab[0]])
                + distance(nodes[ab[1]], destination),
            )
        ),
    ]
    while groups and counts["publictraffic"] < c.max_transit_queries:
        remaining = []
        for group in groups:
            pair = next(group, None)
            if pair is not None:
                remaining.append(group)
                a, b = pair
                if distance(nodes[a], nodes[b]) >= 700:
                    fetch("publictraffic", a, b)
        groups = remaining
    network = dict(
        nodes=nodes,
        edges=edges,
        metadata=dict(
            source="kakao_candidates",
            schema_version=1,
            created_at=datetime.now().astimezone().isoformat(),
            query_counts=counts,
            api=client.stats(),
            errors=errors,
            anchors=anchors,
            transit_time_semantics="API estimate at fetch time; no requested departure time",
            candidate_limits=vars(c),
            approximate=True,
        ),
    )
    if checkpoint:
        save_json(checkpoint, network)
    if progress:
        progress(
            f"교통망 저장: 도보 {sum(e['mode'] == 'walk' for e in edges)} / 대중교통 {sum(e['mode'] == 'transit' for e in edges)} / 따릉이 후보 {sum(e['mode'] == 'bicycle' for e in edges)}",
            flush=True,
        )
    return network


def search(network, bike_provider, departure, config=None, progress=print):
    c = config or KakaoConfig()
    c.validate()
    start = timestamp(departure)
    dt = datetime.fromisoformat(departure) if isinstance(departure, str) else departure

    def iso(t):
        return datetime.fromtimestamp(t, dt.tzinfo).isoformat()

    nodes = network["nodes"]
    if "origin" not in nodes or "destination" not in nodes:
        raise ValueError("출발지·목적지가 없는 교통망입니다.")
    adjacency = defaultdict(list)
    for edge in network["edges"]:
        if edge["from"] not in nodes or edge["to"] not in nodes:
            raise ValueError("교통망 연결의 노드가 없습니다.")
        if edge["mode"] not in ("walk", "transit", "bicycle"):
            raise ValueError("지원하지 않는 이동수단입니다.")
        if edge["mode"] != "bicycle":
            m = float(edge["minutes"])
            if not math.isfinite(m) or m <= 0:
                raise ValueError("이동시간은 양수여야 합니다.")
        adjacency[edge["from"]].append(edge)
    # Spend the bounded inference time on candidates that make spatial progress
    # first. This changes evaluation order only; it does not remove candidates.
    for node, outgoing in adjacency.items():
        outgoing.sort(
            key=lambda edge: (
                0 if edge["mode"] != "bicycle" else 1,
                float(edge.get("minutes", 0))
                if edge["mode"] != "bicycle"
                else distance(nodes[node], nodes[edge["to"]])
                + distance(nodes[edge["to"]], nodes["destination"]),
            )
        )
    # Simple candidate paths and bounded labels. No fixed bicycle rental limit.
    counter = itertools.count()
    heap = [(0.0, next(counter), "origin", [], frozenset(["origin"]), False)]
    label_counts = defaultdict(int)
    results, signatures, bike_cache, errors = [], set(), {}, []
    predictions = expanded = pruned = 0
    limited = False
    termination_reason = None
    begun = time.monotonic()

    def materialize(legs, total):
        flat = []
        t = start
        for leg in legs:
            edge = dict(leg, start=iso(t), arrival=iso(t + leg["minutes"] * 60))
            # API macro steps are explanatory: totalTime remains authoritative.
            # They receive no fabricated individual scheduled timestamps.
            flat.append(edge)
            t += leg["minutes"] * 60
        return dict(
            total_minutes=total,
            arrival=iso(start + total * 60),
            legs=flat,
            bike_rentals=sum(l["mode"] == "bicycle" for l in flat),
            transit_boardings=sum(l.get("boardings", 0) for l in flat),
            walking_minutes=sum(
                l["minutes"]
                if l["mode"] == "walk"
                else sum(
                    s["minutes"] for s in l.get("steps", []) if s["mode"] == "walk"
                )
                for l in flat
            ),
            time_basis="bicycle_" + c.bike_quantile + "_plus_kakao_estimates",
            whole_route_p80=False,
            fare_total_available=False,
        )

    # Direct baselines remain available even if model inference uses the budget.
    for edge in adjacency["origin"]:
        if (
            edge["to"] == "destination"
            and edge["mode"] != "bicycle"
            and edge["minutes"] <= c.horizon_minutes
        ):
            results.append(materialize([edge], edge["minutes"]))
    while heap:
        if (
            expanded >= c.max_expansions
            or time.monotonic() - begun >= c.max_search_seconds
        ):
            limited = True
            termination_reason = (
                "max_expansions"
                if expanded >= c.max_expansions
                else "max_search_seconds"
            )
            break
        elapsed, _, node, legs, visited, after_bike = heapq.heappop(heap)
        expanded += 1
        if node == "destination":
            results.append(materialize(legs, elapsed))
            continue
        # Different arrival buckets preserve some time-dependent bike choices;
        # label pruning is approximate, not a proof of global optimality.
        state = (node, int(elapsed // 5), after_bike)
        if label_counts[state] >= c.labels_per_node:
            pruned += 1
            continue
        label_counts[state] += 1
        for edge in adjacency[node]:
            if time.monotonic() - begun >= c.max_search_seconds:
                limited = True
                termination_reason = termination_reason or "max_search_seconds"
                break
            to = edge["to"]
            if to in visited or (edge["mode"] == "bicycle" and after_bike):
                continue
            details = None
            if edge["mode"] == "bicycle":
                dep = iso(start + elapsed * 60)
                precheck = (
                    bike_provider.precheck_inventory(edge, dep)
                    if hasattr(bike_provider, "precheck_inventory")
                    else None
                )
                if precheck is not None:
                    pre_risk = precheck.get("p_empty")
                    if c.require_known_inventory and pre_risk is None:
                        continue
                    if (
                        pre_risk is not None
                        and c.max_empty_probability is not None
                        and pre_risk > c.max_empty_probability
                    ):
                        continue
                key = (node, to, dep)
                if key not in bike_cache:
                    if predictions >= c.max_bike_predictions:
                        limited = True
                        termination_reason = (
                            termination_reason or "max_bike_predictions"
                        )
                        continue
                    predictions += 1
                    if progress and (predictions == 1 or predictions % 10 == 0):
                        progress(
                            f"3/3 따릉이 {c.bike_quantile.upper()} 추론 {predictions}/{c.max_bike_predictions}",
                            flush=True,
                        )
                    try:
                        value = bike_provider(edge, dep)
                        minutes = float(value["minutes"])
                        if not math.isfinite(minutes) or minutes <= 0:
                            raise ValueError("따릉이 예측시간이 양수가 아닙니다.")
                        risk = value.get("p_empty")
                        if risk is not None and (
                            not math.isfinite(risk) or not 0 <= risk <= 1
                        ):
                            raise ValueError("재고 확률은 0~1이어야 합니다.")
                        bike_cache[key] = value
                    except Exception as exc:
                        bike_cache[key] = None
                        # Exception text may contain an upstream URL/key.
                        errors.append(
                            dict(start=node, end=to, error_type=type(exc).__name__)
                        )
                details = bike_cache[key]
                if details is None:
                    continue
                risk = details.get("p_empty")
                if c.require_known_inventory and risk is None:
                    continue
                if (
                    risk is not None
                    and c.max_empty_probability is not None
                    and risk > c.max_empty_probability
                ):
                    continue
                minutes = float(details["minutes"])
            else:
                minutes = edge["minutes"]
            new_elapsed = elapsed + minutes
            if new_elapsed > c.horizon_minutes:
                continue
            leg = dict(edge, minutes=minutes)
            if details is not None:
                leg.update(
                    details=details, geometry=details.get("route", {}).get("geometry")
                )
            # A direct rerental at the same node cannot split A->B->C into
            # artificially shorter modeled rides. Walk/transit can connect rides.
            next_after = edge["mode"] == "bicycle"
            heapq.heappush(
                heap,
                (
                    new_elapsed,
                    next(counter),
                    to,
                    legs + [leg],
                    visited | {to},
                    next_after,
                ),
            )
    unique = []
    for route in sorted(results, key=lambda r: r["total_minutes"]):
        signature = tuple(
            (l["mode"], l["from"], l["to"], l.get("route_option"))
            for l in route["legs"]
        )
        if signature not in signatures:
            signatures.add(signature)
            unique.append(route)
    # Retain the walking and transit baselines separately even outside top-k.
    baselines = [r for r in unique if len(r["legs"]) == 1 and not r["bike_rentals"]]
    metadata = network.get("metadata", {})
    partial = limited or bool(errors) or bool(metadata.get("errors"))
    return dict(
        status="partial" if partial else ("ok" if unique else "no_route"),
        departure=iso(start),
        routes=unique[: c.top_k],
        baselines=baselines,
        nodes=nodes,
        network_metadata=metadata,
        expanded_states=expanded,
        pruned_labels=pruned,
        bike_predictions=predictions,
        prediction_errors=errors[:30],
        budget_exhausted=limited,
        termination_reason=termination_reason,
        elapsed_seconds=time.monotonic() - begun,
        optimality_guaranteed=False,
        whole_route_p80=False,
        bicycle_time_quantile=c.bike_quantile,
        limitations=[
            "선택한 대여소·API 후보 안의 근사 탐색입니다.",
            "대중교통은 조회 당시 예상시간이며 요청 출발시각·미래 대기시간·막차를 검증하지 않습니다.",
            "따릉이 구간별 예측시간을 합산한 계획시간이며 전체 경로의 분위수는 아닙니다.",
            "추가 대여·반납시간은 더하지 않습니다.",
            "재고 모델 미연결 시 대여 가능성은 확인되지 않습니다. 반납 여유는 평가하지 않습니다.",
            "대중교통 구간별 요금은 합산하지 않습니다. 전체 환승요금은 미계산입니다.",
        ],
    )


def map_html(result):
    """Leaflet output; coordinates are always GeoJSON [longitude,latitude]."""
    data = json.dumps(result, ensure_ascii=False, allow_nan=False).replace(
        "<", "\\u003c"
    )
    return (
        """<!doctype html><html lang="ko"><meta charset="utf-8"><title>따릉이 통합 경로</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{margin:0;font:14px sans-serif}#panel{padding:12px;max-height:24vh;overflow:auto}#map{height:72vh}button{margin:4px;padding:8px}</style>
<div id="panel"><div id="caption"></div><div>실선: 도로·선로 / 회색 점선: 도보 / 보라색 점선: 역내 환승 개략 / 주황색 점선: 형상 미확보 구간 개략</div><div id="routes"></div><div id="detail"></div></div><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const data="""
        + data
        + """;const map=L.map('map').setView([37.56,126.98],12);
const bikeBasis=(data.bicycle_time_quantile||'p80').toUpperCase();
const modeNames={integrated:'통합 경로',subway:'지하철 경로',bicycle:'따릉이 경로'};
document.title=modeNames[data.search_mode]||'따릉이 통합 경로';
document.getElementById('caption').textContent=data.search_mode==='subway'?'지하철 시간표 + 도보':data.search_mode==='bicycle'?'따릉이 '+bikeBasis+' + 도보':'따릉이 '+bikeBasis+(data.timetable_file?' + 대중교통 시간표':' + 카카오 예상시간 · 미래 배차시간 미반영');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap contributors'}).addTo(map);
let group=L.featureGroup().addTo(map);const colors={walk:'#777',bicycle:'#178943',bus:'#2475d5',subway:'#8a45c4'};
const routeKey=r=>JSON.stringify(r.legs.filter(l=>!['wait','alight'].includes(l.mode)).map(l=>[l.mode,l.from,l.to,l.trip_id||null]));
const displayRoutes=[...data.routes],seenRoutes=new Set(displayRoutes.map(routeKey));
for(const r of data.mixed_routes||[]){const key=routeKey(r);if(!seenRoutes.has(key)){seenRoutes.add(key);displayRoutes.push(r)}}
function draw(i){group.clearLayers();const r=displayRoutes[i];let text=[];
for(const l of r.legs){text.push(l.mode+' '+l.minutes.toFixed(1)+'분'+(l.geometry_status==='unavailable'?' (노선 형상 미확보)':l.geometry_status==='partial'?' (노선 형상 일부 누락)':''));
const parts=l.mode==='transit'?l.steps:[l];for(const p of parts){const geom=p.geometry||p.display_geometry;const schematic=!p.geometry&&!!p.display_geometry;const transfer=schematic&&p.display_geometry_kind==='schematic_transfer';if(geom&&geom.coordinates&&(geom.type==='MultiLineString'?geom.coordinates.some(c=>c.length>1):geom.coordinates.length>1)){L.geoJSON({type:'Feature',geometry:geom,properties:{}},{style:{color:schematic?(transfer?'#9146b5':'#d47a19'):colors[p.mode]||'#444',weight:schematic?4:5,dashArray:schematic?'3 8':p.mode==='walk'?'5 7':null}}).bindTooltip(schematic?(transfer?'역내 환승 연결(개략·실내 통로 아님)':'형상 미확보: 역·정류장 연결 개략도'):(p.route_name||p.mode)).addTo(group);}}}
for(const id of ['origin','destination']){const p=data.nodes[id];L.marker([p.latitude,p.longitude]).bindTooltip(p.name||id).addTo(group);}
document.getElementById('detail').textContent=text.join(' → ');if(group.getBounds().isValid())map.fitBounds(group.getBounds(),{padding:[20,20]});}
displayRoutes.forEach((r,i)=>{const b=document.createElement('button');b.textContent=(r.bike_rentals&&r.transit_boardings?'혼합 ':'')+(i+1)+': '+r.total_minutes.toFixed(1)+'분 / 따릉이 '+r.bike_rentals+'회';b.onclick=()=>draw(i);document.getElementById('routes').appendChild(b);});
if(displayRoutes.length)draw(0);else document.getElementById('detail').textContent='경로 없음: 실행 결과의 오류·후보 범위를 확인하세요.';
</script></html>"""
    )
