"""Export only OSM routes whose keys match current public station coordinates."""

from pathlib import Path
from contextlib import closing
import argparse, csv, gzip, hashlib, json, math, re, shutil, sqlite3, sys, tempfile

ROOT = Path(__file__).resolve().parents[1]
NUMBERS = set(
    "route_distance_m network_distance_m access_distance_m base_duration_min descent_m max_grade uphill_2_4_m uphill_4_7_m uphill_7_10_m uphill_10plus_m downhill_m signal_count crossing_count intersection_count major_road_ratio bridge_m tunnel_m route_bearing_deg access_start_m access_end_m ascent_m max_slope_deg avg_uphill_slope_deg longest_climb_m turn_count signal_density_km intersection_density_km".split()
)
OPTIONAL_NUMBERS = {"access_start_m", "access_end_m"}


def public_payload(value):
    rows = json.loads(value)
    if not isinstance(rows, list) or not rows:
        return None
    result = []
    for row in rows:
        if row.get("route_source") != "osm_srtm_v2" or not re.fullmatch(
            "[0-9a-f]{24}", row.get("route_id", "")
        ):
            return None
        if not (NUMBERS - OPTIONAL_NUMBERS) <= row.keys():
            return None
        clean = {key: float(row[key]) for key in sorted(NUMBERS) if key in row}
        if not all(math.isfinite(v) for v in clean.values()):
            return None
        geometry = row.get("geometry", {})
        points = geometry.get("coordinates", [])
        if geometry.get("type") != "LineString" or len(points) < 2:
            return None
        if not all(
            isinstance(p, list)
            and len(p) == 2
            and all(isinstance(x, (int, float)) and math.isfinite(x) for x in p)
            for p in points
        ):
            return None
        clean.update(
            route_id=row["route_id"],
            profiles=["fast"],
            geometry={"type": "LineString", "coordinates": points},
            route_source="osm_srtm_v2",
        )
        for field, allowed in {
            "snap_method": {"nearest_reachable_node_bounded", "nearest_node"},
            "access_routing": {
                "straight_distance_walking_estimate",
                "walking_distance_only",
            },
        }.items():
            if row.get(field) in allowed:
                clean[field] = row[field]
        result.append(clean)
    return json.dumps(result, separators=(",", ":"), allow_nan=False)


def export(source, output, model=None):
    model = Path(model or ROOT / "models/eta_v5_2_1")
    signature = json.loads((model / "manifest.json").read_text(encoding="utf-8"))[
        "graph_signature"
    ]
    with (model / "data/stations_current.csv").open(encoding="utf-8-sig") as stream:
        coords = sorted(
            {
                (float(row["latitude"]), float(row["longitude"]))
                for row in csv.DictReader(stream)
            }
        )
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with closing(
        sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)
    ) as src:
        remaining = {key for (key,) in src.execute("SELECT cache_key FROM routes")}
        matched = set()
        prefixes = [
            json.dumps([signature, a, b], sort_keys=True, default=str)[:-1] + ", "
            for a, b in coords
        ]
        suffixes = [
            json.dumps([a, b], sort_keys=True, default=str)[1:] for a, b in coords
        ]
        for prefix in prefixes:
            for suffix in suffixes:
                key = hashlib.sha256((prefix + suffix).encode()).hexdigest()[:24]
                if key in remaining:
                    matched.add(key)
                    remaining.remove(key)
            if not remaining:
                break
        with tempfile.TemporaryDirectory(dir=output) as directory:
            target = Path(directory) / "public_routes.sqlite"
            count = 0
            with closing(sqlite3.connect(target)) as dst, dst:
                dst.execute(
                    "CREATE TABLE routes(cache_key TEXT PRIMARY KEY,payload TEXT)"
                )
                for key, value in src.execute(
                    "SELECT cache_key,payload FROM routes ORDER BY cache_key"
                ):
                    if key in matched:
                        payload = public_payload(value)
                        if payload is not None:
                            dst.execute(
                                "INSERT INTO routes VALUES(?,?)", (key, payload)
                            )
                            count += 1
            archive = output / "osm_station_routes.sqlite.gz"
            uncompressed_bytes = target.stat().st_size
            with target.open("rb") as stream, archive.open("wb") as dest:
                with gzip.GzipFile(
                    filename="", fileobj=dest, mode="wb", mtime=0, compresslevel=6
                ) as zipped:
                    shutil.copyfileobj(stream, zipped)
    meta = {
        "format": 1,
        "graph_signature": signature,
        "rows": count,
        "uncompressed_bytes": uncompressed_bytes,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "bytes": archive.stat().st_size,
        "source": "Computed OSM routes between public Seoul Bike stations; no users, query timestamps or API responses",
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
    }
    (output / "manifest.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return meta


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "data/reference")
    args = parser.parse_args()
    print(json.dumps(export(args.source, args.output)))
