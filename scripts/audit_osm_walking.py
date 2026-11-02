"""Fixed-seed, stratified OSM/Kakao comparison; no retries or dropped failures."""

from pathlib import Path
import argparse
import json
import random
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hybrid_walking import get_graph, HybridWalking
from kakao_routes import KakaoClient
from scheduled_runtime import KakaoWalking
from project_paths import (
    PROJECT_ROOT,
    RUNTIME_CACHE_DIR,
    RUNTIME_OUTPUT_DIR,
    load_project_env,
)


def audit(seed=20261102, count=12, max_calls=12, reuse_sample=False):
    import numpy as np
    from scipy.spatial import cKDTree

    load_project_env()
    graph = get_graph(PROJECT_ROOT / "data/walking/seoul_walk.sqlite")
    if graph is None:
        raise ValueError("No completed pedestrian graph is available")
    files = sorted(
        (RUNTIME_OUTPUT_DIR / "runs").glob("*/result.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise ValueError(
            "A saved route result with the public station catalog is required"
        )
    network = json.loads(files[0].read_text(encoding="utf-8"))["nodes"]
    groups = {"rail": [], "bus": [], "bike": []}
    for key, value in sorted(network.items()):
        kind = (
            "rail"
            if key.startswith("subway:")
            else "bus"
            if key.startswith("bus:")
            else "bike"
            if key.startswith("bike:")
            else None
        )
        if kind and "latitude" in value:
            groups[kind].append((key, value))
    rng = random.Random(seed)
    pairs = []
    categories = [("rail", "bus"), ("rail", "bike"), ("bus", "bike"), ("bus", "bus")]
    populations = {}
    for i, (a_kind, b_kind) in enumerate(categories):
        a_items, b_items = groups[a_kind], groups[b_kind]
        b_xy = (
            np.array([[v["longitude"], v["latitude"]] for _, v in b_items])
            * graph.scale
        )
        index = cKDTree(b_xy)
        eligible = []
        for a, value in a_items:
            xy = np.array([value["longitude"], value["latitude"]]) * graph.scale
            for j in index.query_ball_point(xy, 600):
                b, target = b_items[j]
                if a == b or (a_kind == b_kind and a > b):
                    continue
                if np.linalg.norm(xy - b_xy[j]) < 30:
                    continue
                if graph.covered(value, target):
                    eligible.append((a, b, value, target))
        category = a_kind + "_" + b_kind
        populations[category] = len(eligible)
        take = count // 4 + int(i < count % 4)
        chosen = rng.sample(eligible, min(take, len(eligible)))
        for a, b, value, target in chosen:
            if rng.random() < 0.5:
                a, b, value, target = b, a, target, value
            pairs.append((category, a, b, value, target))
    if reuse_sample:
        previous = json.loads(
            (
                RUNTIME_OUTPUT_DIR / "diagnostics" / "osm_kakao_walk_comparison.json"
            ).read_text(encoding="utf-8")
        )
        pairs = [
            (r["category"], r["from"], r["to"], network[r["from"]], network[r["to"]])
            for r in previous["records"]
        ]
        populations = previous["populations"]
    RUNTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client = KakaoClient(
        RUNTIME_CACHE_DIR / "scheduled_kakao_walk.sqlite",
        max_calls=max_calls,
        daily_limit=1000,
    )
    walking = HybridWalking(
        KakaoWalking(client),
        PROJECT_ROOT / "data/walking/seoul_walk.sqlite",
        RUNTIME_CACHE_DIR / "osm_walks.sqlite",
    )
    records = []
    try:
        for category, a, b, value, target in pairs:
            record = {
                "category": category,
                "from": a,
                "to": b,
                "from_name": value.get("name", value.get("station_name", a)),
                "to_name": target.get("name", target.get("station_name", b)),
            }
            osm = walking._osm(value, target, a, b)
            record["osm_minutes"] = osm["minutes"] if osm else None
            record["osm_requires_verification"] = bool(
                osm and osm.get("needs_kakao_verification")
            )
            try:
                authoritative = walking.kakao(value, target, a, b)
                record["kakao_minutes"] = (
                    authoritative["minutes"] if authoritative else None
                )
                record["status"] = (
                    "paired"
                    if osm and authoritative
                    else "osm_unavailable"
                    if authoritative
                    else "kakao_no_route"
                )
                walking._compare(value, target, osm, authoritative)
            except Exception as exc:
                record["status"] = type(exc).__name__
            if record.get("kakao_minutes") is not None and osm:
                record["difference_minutes"] = osm["minutes"] - record["kakao_minutes"]
                record["absolute_difference_minutes"] = abs(
                    record["difference_minutes"]
                )
                record["ratio_osm_to_kakao"] = osm["minutes"] / record["kakao_minutes"]
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
        paired = [r for r in records if r["status"] == "paired"]
        summary = {
            "seed": seed,
            "selected": len(pairs),
            "populations": populations,
            "selection": "random directed pairs in 30-600m straight-line range within completed OSM tiles; selected before route success is known",
            "paired": len(paired),
            "failures": [r for r in records if r["status"] != "paired"],
            "api_usage": client.stats(),
            "records": records,
        }
        if paired:
            gaps = [r["absolute_difference_minutes"] for r in paired]
            summary.update(
                mean_absolute_difference_minutes=statistics.mean(gaps),
                median_absolute_difference_minutes=statistics.median(gaps),
                max_absolute_difference_minutes=max(gaps),
                large_difference_count=sum(
                    r["absolute_difference_minutes"] >= 2
                    and (
                        r["ratio_osm_to_kakao"] >= 1.5
                        or r["ratio_osm_to_kakao"] <= 1 / 1.5
                    )
                    for r in paired
                ),
            )
        summary["completed_tiles"] = graph.tiles
        summary["sample_reused"] = reuse_sample
        output = (
            RUNTIME_OUTPUT_DIR
            / "diagnostics"
            / (
                "osm_kakao_walk_comparison_after.json"
                if reuse_sample
                else "osm_kakao_walk_comparison.json"
            )
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("REPORT", output, flush=True)
    finally:
        walking.close()
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261102)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--reuse-sample", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.count <= 30 or not 1 <= args.max_calls <= 30:
        parser.error("count and max-calls must be 1..30")
    audit(args.seed, args.count, args.max_calls, args.reuse_sample)
