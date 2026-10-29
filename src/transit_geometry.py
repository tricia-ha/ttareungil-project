"""Static road/rail geometry for display; never changes routing times or scores."""

from pathlib import Path
from collections import Counter
import csv
import hashlib
import json
import math
import re
import sqlite3
import uuid


def rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            yield {
                str(k).replace("\ufeff", "").strip(): str(v or "")
                .replace("\ufeff", "")
                .strip()
                for k, v in row.items()
                if k is not None
            }


def metres(a, b):
    x = math.radians(b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    y = math.radians(b[1] - a[1])
    return 6371000 * math.hypot(x, y)


def parse_link_geometry(text):
    # TOPIS separates lon/lat with a dot: |127.12345.37.12345||...|.
    pairs = re.findall(r"(-?\d+\.\d+)[.,\s]+(-?\d+\.\d+)", text.replace("\ufeff", ""))
    points = [[float(x), float(y)] for x, y in pairs]
    if len(points) < 2 or any(
        not (124 <= x <= 132 and 33 <= y <= 39) for x, y in points
    ):
        return None
    return points


def project(points, target):
    best = None
    accumulated = 0.0
    scale = math.cos(math.radians(target[1]))
    for i, (a, b) in enumerate(zip(points, points[1:])):
        ax, ay = (a[0] - target[0]) * scale, a[1] - target[1]
        bx, by = (b[0] - target[0]) * scale, b[1] - target[1]
        dx, dy = bx - ax, by - ay
        ratio = (
            max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
            if dx * dx + dy * dy
            else 0.0
        )
        point = [a[0] + ratio * (b[0] - a[0]), a[1] + ratio * (b[1] - a[1])]
        length = metres(a, b)
        item = (metres(point, target), accumulated + ratio * length, i, point)
        if best is None or item[:2] < best[:2]:
            best = item
        accumulated += length
    return best


def trim(points, start, finish, max_snap=120):
    a, b = project(points, start), project(points, finish)
    if a is None or b is None or max(a[0], b[0]) > max_snap or b[1] <= a[1]:
        return None
    result = [a[3]] + points[a[2] + 1 : b[2] + 1] + [b[3]]
    return [p for i, p in enumerate(result) if i == 0 or p != result[i - 1]]


def prepare_geometry_database(folder, progress=print):
    folder = Path(folder)
    source = folder / "geometry_sources"
    required = (
        "bus_spatial_links.csv",
        "bus_route_links.csv",
        "bus_route_via_nodes.csv",
        "bus_link_mapping.csv",
    )
    fingerprint = hashlib.sha256()
    for name in required:
        path = source / name
        if not path.is_file():
            raise FileNotFoundError("형상 원자료가 없습니다: " + str(path))
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                fingerprint.update(block)
    signature = fingerprint.hexdigest()
    target = folder / "transit_geometry.sqlite"
    if target.is_file():
        with sqlite3.connect(target) as db:
            try:
                row = db.execute(
                    "SELECT value FROM metadata WHERE key=?", ("sources",)
                ).fetchone()
                if row and row[0] == signature:
                    return target
            except sqlite3.Error:
                pass
    temporary = target.with_name("geometry_build_" + uuid.uuid4().hex + ".sqlite")
    db = sqlite3.connect(temporary)
    try:
        db.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE links(id TEXT PRIMARY KEY,geometry TEXT);
            CREATE TABLE route_links(route TEXT,sequence INTEGER,link TEXT,PRIMARY KEY(route,sequence));
            CREATE TABLE stops(route TEXT,sequence INTEGER,stop TEXT,link_sequence INTEGER,
                               PRIMARY KEY(route,sequence,stop));
            CREATE TABLE segment_cache(key TEXT PRIMARY KEY,geometry TEXT);
        """)
        counts = Counter()
        for name in (required[3], required[0]):
            for row in rows(source / name):
                points = parse_link_geometry(row["GEOMETRY좌표"])
                if points and any(p != points[0] for p in points[1:]):
                    db.execute(
                        "INSERT OR REPLACE INTO links VALUES (?,?)",
                        (row["링크ID"], json.dumps(points, separators=(",", ":"))),
                    )
                    counts["links"] += 1
        db.commit()
        for row in rows(source / required[1]):
            db.execute(
                "INSERT OR REPLACE INTO route_links VALUES (?,?,?)",
                (row["노선ID"], int(row["링크순번"]), row["링크ID"]),
            )
            counts["route_links"] += 1
        db.commit()
        for row in rows(source / required[2]):
            if (
                len(row["노드ID"]) == 9
                and row["노드ID"].isdigit()
                and int(row["정류장순번"]) > 0
            ):
                db.execute(
                    "INSERT OR REPLACE INTO stops VALUES (?,?,?,?)",
                    (
                        row["노선ID"],
                        int(row["정류장순번"]),
                        row["노드ID"],
                        int(row["링크순번"]),
                    ),
                )
                counts["stops"] += 1
        db.execute("INSERT INTO metadata VALUES (?,?)", ("sources", signature))
        db.execute("INSERT INTO metadata VALUES (?,?)", ("counts", json.dumps(counts)))
        db.commit()
    finally:
        db.close()
    temporary.replace(target)
    if progress:
        progress("버스 노선 형상 색인 준비 완료: " + str(dict(counts)), flush=True)
    return target


class TransitGeometry:
    def __init__(self, folder):
        self.folder = Path(folder)
        path = self.folder / "transit_geometry.sqlite"
        self.db = sqlite3.connect(path, timeout=30) if path.is_file() else None
        self.cache = {}
        self.rail = []
        from rail_shapes import RailShapes

        self.shapes = RailShapes(self.folder / "geometry_sources" / "rail_lines")
        rail_path = self.folder / "geometry_sources" / "seoul_metro_routes_osm.json"
        if rail_path.is_file():
            self.rail = json.loads(rail_path.read_text(encoding="utf-8")).get(
                "elements", []
            )

    def close(self):
        if self.db:
            self.db.close()

    def subway(self, leg, nodes):
        # Only use an ordered, continuous shape of the requested line.
        # Never substitute straight station-to-station lines for missing tracks.
        line = str(leg.get("route_id", "")).replace("호선", "")
        start = [nodes[leg["from"]]["longitude"], nodes[leg["from"]]["latitude"]]
        end = [nodes[leg["to"]]["longitude"], nodes[leg["to"]]["latitude"]]
        matched = self.shapes.match(str(leg.get("route_id", "")), start, end)
        if matched:
            return matched
        candidates = []
        for relation in self.rail:
            if str(relation.get("tags", {}).get("ref", "")) != line:
                continue
            chain = []
            chains = []
            for member in relation.get("members", []):
                if member.get("type") != "way" or member.get("role", "") not in (
                    "",
                    "forward",
                    "backward",
                ):
                    continue
                part = [[p["lon"], p["lat"]] for p in member.get("geometry", [])]
                if len(part) < 2:
                    continue
                if chain:
                    if metres(chain[-1], part[-1]) < metres(chain[-1], part[0]):
                        part.reverse()
                    if metres(chain[-1], part[0]) > 5:
                        chains.append(chain)
                        chain = []
                chain.extend(part)
            if chain:
                chains.append(chain)
            for chain in chains:
                for points in (chain, chain[::-1]):
                    clipped = trim(points, start, end, max_snap=180)
                    if clipped:
                        length = sum(metres(a, b) for a, b in zip(clipped, clipped[1:]))
                        if length <= max(1500, metres(start, end) * 4):
                            candidates.append((length, clipped))
        if not candidates:
            return None
        return {
            "type": "LineString",
            "coordinates": min(candidates, key=lambda x: x[0])[1],
        }

    def bus(self, leg, nodes):
        if self.db is None:
            return None
        sequence = leg.get("stop_sequence")
        if sequence is None:
            return None
        rid = str(leg["route_id"])
        sequence = int(sequence) + 1
        a, b = leg["from"].removeprefix("bus:"), leg["to"].removeprefix("bus:")
        start = [nodes[leg["from"]]["longitude"], nodes[leg["from"]]["latitude"]]
        end = [nodes[leg["to"]]["longitude"], nodes[leg["to"]]["latitude"]]
        key = json.dumps([rid, sequence, a, b, start, end], separators=(",", ":"))
        if key in self.cache:
            return self.cache[key]
        cached = self.db.execute(
            "SELECT geometry FROM segment_cache WHERE key=?", (key,)
        ).fetchone()
        if cached:
            self.cache[key] = json.loads(cached[0])
            return self.cache[key]
        positions = []
        for order, sid in ((sequence, a), (sequence + 1, b)):
            found = self.db.execute(
                "SELECT link_sequence FROM stops WHERE route=? AND sequence=? AND stop=?",
                (rid, order, sid),
            ).fetchone()
            if not found:
                return None
            positions.append(found[0])
        if positions[1] < positions[0]:
            return None
        pieces = self.db.execute(
            """SELECT r.sequence,l.geometry FROM route_links r
            LEFT JOIN links l ON l.id=r.link WHERE r.route=? AND r.sequence BETWEEN ? AND ?
            ORDER BY r.sequence""",
            (rid, *positions),
        ).fetchall()
        if (
            not pieces
            or len(pieces) != positions[1] - positions[0] + 1
            or any(not p[1] for p in pieces)
        ):
            return None
        points = []
        gaps = set()
        for _, value in pieces:
            part = json.loads(value)
            if points:
                if metres(points[-1], part[-1]) < metres(points[-1], part[0]):
                    part.reverse()
                if metres(points[-1], part[0]) > 40:
                    gaps.add((tuple(points[-1]), tuple(part[0])))
            points.extend(
                part if not points else part[1:] if part[0] == points[-1] else part
            )
        points = trim(points, start, end)
        if not points or len(points) < 2:
            return None
        chunks = [[points[0]]]
        for a, b in zip(points, points[1:]):
            if (tuple(a), tuple(b)) in gaps:
                chunks.append([])
            chunks[-1].append(b)
        chunks = [part for part in chunks if len(part) > 1]
        if not chunks:
            return None
        geometry = (
            {"type": "LineString", "coordinates": chunks[0]}
            if len(chunks) == 1
            else {"type": "MultiLineString", "coordinates": chunks}
        )
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO segment_cache VALUES (?,?)",
                (key, json.dumps(geometry)),
            )
        self.cache[key] = geometry
        return geometry


def attach_transit_geometry(result, folder):
    """Postprocess only geometry; preserve all search times, scores and rankings."""
    resolver = TransitGeometry(folder)
    counts = Counter()
    try:
        seen = set()
        for route in result.get("routes", []) + result.get("mixed_routes", []):
            if id(route) in seen:
                continue
            seen.add(id(route))
            for leg in route["legs"]:
                mode = leg["mode"]
                if mode == "bicycle":
                    leg["geometry"] = leg.get("details", {}).get("route", {}).get(
                        "geometry"
                    ) or leg.get("geometry")
                elif mode in ("bus", "subway"):
                    geometry = getattr(resolver, mode)(leg, result["nodes"])
                    leg["geometry"] = geometry
                    leg["geometry_source"] = (
                        (
                            "seoul_topis_road_links"
                            if mode == "bus"
                            else "osm_rail_geometry"
                        )
                        if geometry
                        else None
                    )
                    leg["geometry_status"] = (
                        (
                            "partial"
                            if geometry["type"] == "MultiLineString"
                            else "matched"
                        )
                        if geometry
                        else "unavailable"
                    )
                    counts[mode + ("_matched" if geometry else "_missing")] += 1
                if mode in ("walk", "bus", "subway") and not leg.get("geometry"):
                    a = result["nodes"].get(leg.get("from"), {})
                    b = result["nodes"].get(leg.get("to"), {})
                    if all(k in p for p in (a, b) for k in ("longitude", "latitude")):
                        leg["display_geometry"] = {
                            "type": "LineString",
                            "coordinates": [
                                [a["longitude"], a["latitude"]],
                                [b["longitude"], b["latitude"]],
                            ],
                        }
                        leg["display_geometry_kind"] = (
                            "schematic_transfer"
                            if mode == "walk"
                            else "schematic_missing_shape"
                        )
        result["geometry_summary"] = dict(counts)
    finally:
        resolver.close()
    return result
