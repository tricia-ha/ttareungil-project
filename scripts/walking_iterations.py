"""Three fixed-seed rounds of walk comparison with a total new-call budget."""

from pathlib import Path
import argparse
import json
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np
from scipy.spatial import cKDTree
from hybrid_walking import get_graph, HybridWalking
from scheduled_runtime import KakaoWalking
from kakao_routes import KakaoClient
from walking_quality import features, large_error, learn, evaluate
from project_paths import (
    PROJECT_ROOT,
    RUNTIME_CACHE_DIR,
    RUNTIME_OUTPUT_DIR,
    load_project_env,
)


def run(seed=20261103, max_new_calls=60):
    load_project_env()
    graph = get_graph(PROJECT_ROOT / "data/walking/seoul_walk.sqlite")
    if graph is None:
        raise ValueError("Build OSM walking data first")
    files = sorted(
        (RUNTIME_OUTPUT_DIR / "runs").glob("*/result.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    nodes = json.loads(files[0].read_text(encoding="utf-8"))["nodes"]
    groups = {"rail": [], "bus": [], "bike": []}
    for key, v in sorted(nodes.items()):
        kind = (
            "rail"
            if key.startswith("subway:")
            else "bus"
            if key.startswith("bus:")
            else "bike"
            if key.startswith("bike:")
            else None
        )
        if kind and "latitude" in v:
            groups[kind].append((key, v))
    output = RUNTIME_OUTPUT_DIR / "diagnostics" / f"walking_iterations_{seed}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    if old and old.get("graph_signature") != graph.signature:
        raise ValueError(
            "Graph changed; use a different seed to preserve the prior experiment"
        )
    rng = random.Random(seed)
    rounds = [[] for _ in range(3)]
    seen = set()

    def physical(a, b):
        return tuple(
            sorted(
                (
                    tuple(round(a[k], 5) for k in ("latitude", "longitude")),
                    tuple(round(b[k], 5) for k in ("latitude", "longitude")),
                )
            )
        )

    prior = RUNTIME_OUTPUT_DIR / "diagnostics" / "osm_kakao_walk_comparison.json"
    if prior.exists():
        for row in json.loads(prior.read_text(encoding="utf-8"))["records"]:
            if row["from"] in nodes and row["to"] in nodes:
                seen.add(physical(nodes[row["from"]], nodes[row["to"]]))
    populations = {}
    for ak, bk in [("rail", "bus"), ("rail", "bike"), ("bus", "bike"), ("bus", "bus")]:
        source, target = groups[ak], groups[bk]
        xy = (
            np.array([[v["longitude"], v["latitude"]] for _, v in target]) * graph.scale
        )
        tree = cKDTree(xy)
        pools = [[], [], []]
        for a, v in source:
            pt = np.array([v["longitude"], v["latitude"]]) * graph.scale
            for j in tree.query_ball_point(pt, 900):
                b, w = target[j]
                if a == b or (ak == bk and a > b):
                    continue
                d = float(np.linalg.norm(pt - xy[j]))
                if d < 30 or not graph.covered(v, w):
                    continue
                pools[0 if d < 300 else 1 if d < 600 else 2].append((a, b))
        populations[ak + "_" + bk] = [len(p) for p in pools]
        for round_index in range(3):
            for pool, take in zip(pools, [2, 2, 1]):
                rng.shuffle(pool)
                chosen = 0
                for a, b in pool:
                    sig = physical(nodes[a], nodes[b])
                    if sig in seen:
                        continue
                    seen.add(sig)
                    if rng.random() < 0.5:
                        a, b = b, a
                    rounds[round_index].append(
                        dict(category=ak + "_" + bk, **{"from": a, "to": b})
                    )
                    chosen += 1
                    if chosen == take:
                        break
    report = dict(
        seed=seed,
        graph_signature=graph.signature,
        selected_rounds=rounds,
        populations=populations,
        selection="4 connection types x (2 short + 2 medium + 1 long) each round; 30-900m; within completed tiles; physically disjoint pairs; selected before route outcomes",
        error_definition="absolute >=2min OR absolute>=1min and symmetric ratio>=1.5",
        protected_rule="All ambiguous arterial-road paths require Kakao",
        records=old.get("records", []),
        rounds=[],
    )
    previous = {(r["round"], r["from"], r["to"]): r for r in report["records"]}
    previous_new_calls = old.get(
        "total_new_api_calls", old.get("api_usage_this_run", {}).get("new_api_calls", 0)
    )
    client = KakaoClient(
        RUNTIME_CACHE_DIR / "scheduled_kakao_walk.sqlite",
        max_calls=max_new_calls,
        daily_limit=1000,
    )
    walking = HybridWalking(
        KakaoWalking(client),
        PROJECT_ROOT / "data/walking/seoul_walk.sqlite",
        RUNTIME_CACHE_DIR / "osm_walks.sqlite",
    )
    rules = []
    stop = False

    def save():
        report["api_usage_this_run"] = client.stats()
        report["total_new_api_calls"] = previous_new_calls + client.calls
        report["finished_at_unix"] = time.time()
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    try:
        for index, sample in enumerate(rounds, 1):
            rules_before = list(rules)
            print(
                "ROUND",
                index,
                "selected",
                len(sample),
                "extra rules",
                rules_before,
                flush=True,
            )
            for pair in sample:
                a, b = pair["from"], pair["to"]
                if (index, a, b) in previous:
                    continue
                v, w = nodes[a], nodes[b]
                edge = walking._osm(v, w, a, b)
                record = dict(
                    pair,
                    round=index,
                    from_name=v.get("name", v.get("station_name", a)),
                    to_name=w.get("name", w.get("station_name", b)),
                    osm_minutes=edge["minutes"] if edge else None,
                    osm_meters=edge.get("distance_m") if edge else None,
                    features=features(v, w, a, b, edge),
                )
                try:
                    hit, kakao = walking.kakao.cached(v, w, a, b)
                    if not hit:
                        if (
                            client.calls >= max_new_calls
                            or client.stats()["local_daily_calls"] >= 900
                        ):
                            report["stopped"] = "budget_reserve"
                            stop = True
                            break
                        kakao = walking.kakao(v, w, a, b)
                    record["kakao_minutes"] = kakao["minutes"] if kakao else None
                    record["kakao_meters"] = kakao.get("distance_m") if kakao else None
                    record["status"] = (
                        "paired"
                        if edge and kakao
                        else "osm_unavailable"
                        if kakao
                        else "kakao_no_route"
                    )
                    record["large_error"] = bool(
                        edge
                        and kakao
                        and large_error(edge["minutes"], kakao["minutes"])
                    )
                    walking._compare(v, w, edge, kakao)
                except Exception as exc:
                    record["status"] = type(exc).__name__
                    record["large_error"] = False
                    report["stopped"] = type(exc).__name__
                    stop = True
                report["records"].append(record)
                save()
                if stop:
                    break
            records = [r for r in report["records"] if r["round"] == index]
            summary = dict(
                round=index,
                extra_rules_before=rules_before,
                evaluation=evaluate(records, rules_before),
            )
            accumulated = [r for r in report["records"] if r["round"] <= index]
            if index < 3:
                rules, table = learn(accumulated)
                summary.update(
                    candidate_extra_rules_after=rules, condition_analysis=table
                )
            else:
                summary["condition_analysis"] = learn(accumulated)[1]
                baseline = evaluate(records, [])
                current = summary["evaluation"]
                useful = (
                    current["retained_large_errors"] < baseline["retained_large_errors"]
                )
                report["recommended_extra_rules"] = rules_before if useful else []
                report["recommendation_reason"] = (
                    "Extra conditions caught additional large errors in the fixed third round"
                    if useful
                    else "No demonstrated gain in the third round; retain the protected baseline and collect more evidence"
                )
            report["rounds"].append(summary)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            save()
            if stop:
                break
        report["complete"] = not stop and len(report["records"]) == sum(
            map(len, rounds)
        )
        if report["complete"]:
            report["overall_condition_analysis"] = learn(report["records"])[1]
            report["overall_baseline"] = evaluate(report["records"], [])
        save()
        print("REPORT", output, flush=True)
    finally:
        walking.close()
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261103)
    parser.add_argument("--max-new-calls", type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.max_new_calls <= 60:
        parser.error("max-new-calls must be 1..60")
    run(args.seed, args.max_new_calls)
