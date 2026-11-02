"""Download pedestrian-only OSM tiles once; resume from completed tiles."""

from pathlib import Path
import argparse
import gzip
import hashlib
import json
import math
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from project_paths import PROJECT_ROOT


def build(
    folder,
    max_tiles=12,
    endpoint="https://overpass-api.de/api/interpreter",
    rebuild_cached=False,
):
    import requests

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / "tiles"
    raw.mkdir(exist_ok=True)
    database = folder / (
        "seoul_walk.rebuild.sqlite" if rebuild_cached else "seoul_walk.sqlite"
    )
    con = sqlite3.connect(database, timeout=30)
    con.execute("pragma journal_mode=WAL")
    con.executescript("""
      create table if not exists nodes(id integer primary key,lon real,lat real);
      create table if not exists edges(u integer,v integer,meters real,seconds real,uncertain integer default 1,primary key(u,v)) without rowid;
      create table if not exists tiles(id text primary key,bounds text,downloaded_at real);
      create table if not exists metadata(key text primary key,value text);
    """)
    if "uncertain" not in {r[1] for r in con.execute("pragma table_info(edges)")}:
        con.execute("alter table edges add column uncertain integer default 1")
        con.commit()
    tiles = [(r, c) for r in range(3) for c in range(4)]
    tiles = [(1, 1), (2, 2)] + [x for x in tiles if x not in [(1, 1), (2, 2)]]
    processed = 0
    for r, c in tiles:
        south = 37.41 + r * 0.10
        west = 126.76 + c * 0.11
        bounds = [
            round(south, 5),
            round(west, 5),
            round(south + 0.12, 5),
            round(west + 0.13, 5),
        ]
        key = hashlib.sha256(json.dumps(bounds).encode()).hexdigest()[:16]
        if con.execute("select 1 from tiles where id=?", (key,)).fetchone():
            continue
        if processed >= max_tiles:
            break
        path = raw / (key + ".json.gz")
        if rebuild_cached and not path.is_file():
            continue
        if path.is_file():
            with gzip.open(path, "rt", encoding="utf-8") as f:
                data = json.load(f)
        else:
            query = (
                '[out:json][timeout:55];way["highway"]["area"!="yes"]["highway"!~"motorway|trunk|construction|proposed|raceway"]["foot"!~"^(no|private)$"]["access"!~"^(no|private)$"]('
                + ",".join(map(str, bounds))
                + ");(._;>;);out body;"
            )
            print("Downloading walk tile", bounds, flush=True)
            try:
                response = requests.get(
                    endpoint,
                    params={"data": query},
                    timeout=(10, 65),
                    headers={"User-Agent": "BicycleAcademicPrototype/1.0"},
                )
                response.raise_for_status()
                data = response.json()
                if data.get("remark"):
                    raise ValueError("Incomplete Overpass response")
            except (requests.RequestException, ValueError) as exc:
                print("Tile deferred", bounds, type(exc).__name__, flush=True)
                processed += 1
                continue
            # Atomic checkpoint: never resume a truncated response.
            temp = path.with_suffix(".partial")
            with gzip.open(temp, "wt", encoding="utf-8") as f:
                json.dump(data, f)
            temp.replace(path)
        nodes = {e["id"]: e for e in data["elements"] if e["type"] == "node"}
        rows = []
        for way in data["elements"]:
            if way["type"] != "way":
                continue
            tags = way.get("tags", {})
            highway = tags.get("highway", "")
            if highway in {
                "busway",
                "bus_guideway",
                "motorway",
                "motorway_link",
                "trunk",
                "trunk_link",
                "construction",
                "proposed",
                "raceway",
            }:
                continue
            if tags.get("foot") in {"no", "private"} or tags.get("access") in {
                "no",
                "private",
            }:
                continue
            uncertain = int(
                highway
                in {
                    "primary",
                    "primary_link",
                    "secondary",
                    "secondary_link",
                    "tertiary",
                    "tertiary_link",
                    "cycleway",
                }
            )
            refs = way.get("nodes", [])
            reverse = tags.get("oneway:foot") == "-1"
            forward_only = tags.get("oneway:foot") in ("yes", "1", "true")
            for a, b in zip(refs, refs[1:]):
                if a not in nodes or b not in nodes:
                    continue
                x, y = nodes[a], nodes[b]
                dx = math.radians(x["lon"] - y["lon"]) * math.cos(
                    math.radians((x["lat"] + y["lat"]) / 2)
                )
                dy = math.radians(x["lat"] - y["lat"])
                length = 6371000 * math.hypot(dx, dy)
                if not math.isfinite(length) or length <= 0:
                    continue
                speed = 0.8 if tags.get("highway") == "steps" else 1.2
                seconds = length / speed
                if not reverse:
                    rows.append((a, b, length, seconds, uncertain))
                if reverse or not forward_only:
                    rows.append((b, a, length, seconds, uncertain))
        with con:
            con.executemany(
                "insert or replace into nodes values(?,?,?)",
                [(i, n["lon"], n["lat"]) for i, n in nodes.items()],
            )
            con.executemany(
                """insert into edges(u,v,meters,seconds,uncertain) values(?,?,?,?,?)
                on conflict(u,v) do update set meters=excluded.meters,seconds=excluded.seconds,uncertain=excluded.uncertain
                where excluded.seconds<edges.seconds or (excluded.seconds=edges.seconds and excluded.uncertain<edges.uncertain)""",
                rows,
            )
            con.execute(
                "insert into tiles values(?,?,?)",
                (key, json.dumps(bounds), time.time()),
            )
            con.execute(
                "insert or replace into metadata values('source','OpenStreetMap / Overpass pedestrian-only filter')"
            )
        processed += 1
        print(
            "Saved",
            bounds,
            "nodes",
            len(nodes),
            "directed links",
            len(rows),
            flush=True,
        )
        if processed < max_tiles:
            time.sleep(1)
    con.execute("pragma wal_checkpoint(TRUNCATE)")
    print(
        json.dumps(
            {
                "tiles": con.execute("select count(*) from tiles").fetchone()[0],
                "nodes": con.execute("select count(*) from nodes").fetchone()[0],
                "edges": con.execute("select count(*) from edges").fetchone()[0],
                "sqlite_mb": round(database.stat().st_size / 1048576, 2),
            }
        ),
        flush=True,
    )
    con.close()
    if rebuild_cached:
        rebuilt = sqlite3.connect(database)
        count = rebuilt.execute("select count(*) from tiles").fetchone()[0]
        rebuilt.close()
        target = folder / "seoul_walk.sqlite"
        previous_count = 0
        if target.exists():
            previous = sqlite3.connect(target)
            previous_count = previous.execute("select count(*) from tiles").fetchone()[0]
            previous.close()
        if count == 0 or count < previous_count:
            raise ValueError("Cached source tiles are incomplete; existing walking graph was preserved")
        database.replace(folder / "seoul_walk.sqlite")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-tiles", type=int, default=12)
    parser.add_argument("--endpoint", default="https://overpass-api.de/api/interpreter")
    parser.add_argument(
        "--rebuild-cached",
        action="store_true",
        help="Rebuild atomically from saved responses without network requests",
    )
    parser.add_argument(
        "--folder", type=Path, default=PROJECT_ROOT / "data" / "walking"
    )
    args = parser.parse_args()
    if args.max_tiles < 1:
        parser.error("--max-tiles must be positive")
    build(args.folder, args.max_tiles, args.endpoint, args.rebuild_cached)
