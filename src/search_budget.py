"""Monotone workload-based budgets; no candidate pruning or external calls."""

import json
import math
from functools import lru_cache
from pathlib import Path

CALIBRATION = Path(__file__).resolve().parents[1] / 'data/reference/search_budget_calibration.json'


def workload(nodes, stations, origin, destination, transit_edges, radius_m, mode):
    """Approximate access density for budget selection only, never route costs."""
    points = [v for k, v in nodes.items() if k not in ('origin', 'destination')
              and not k.startswith('bike:')]
    bicycles = list(stations) if mode not in ('transit', 'subway') else []

    def near(rows, point):
        scale = math.cos(math.radians(float(point['latitude'])))
        total = 0
        for row in rows:
            if row.get('latitude') in (None, '') or row.get('longitude') in (None, ''):
                continue
            dy = (float(row['latitude']) - float(point['latitude'])) * 111320
            dx = (float(row['longitude']) - float(point['longitude'])) * 111320 * scale
            total += dx * dx + dy * dy <= radius_m * radius_m
        return total

    ob, db = near(bicycles, origin), near(bicycles, destination)
    o, d = near(points, origin) + ob, near(points, destination) + db
    n = len(points) + len(bicycles)
    effort = (int(transit_edges) + n) * (o + 1) * (d + 1) + len(bicycles) * (ob + 1) * (db + 1)
    return dict(workload=max(1, effort), transit_edges=int(transit_edges), facilities=n,
                origin_candidates=o, destination_candidates=d,
                origin_bicycle_candidates=ob, destination_bicycle_candidates=db)


@lru_cache(maxsize=1)
def calibration():
    return json.loads(CALIBRATION.read_text(encoding='utf-8'))


def select_budget(metrics, mode='integrated', *, call_ceiling=45, time_ceiling=150, reference=None):
    reference = reference if reference is not None else calibration()
    profile = reference['profiles'].get('transit' if mode == 'subway' else mode, reference['profiles']['all'])
    low, high = map(float, (profile['low_workload'], profile['high_workload']))
    score = min(1.0, max(0.0, (math.log1p(metrics['workload']) - math.log1p(low)) /
                             max(1e-9, math.log1p(high) - math.log1p(low))))
    calls = min(45, max(15, int(call_ceiling)))
    seconds = min(150, max(30, float(time_ceiling if time_ceiling is not None else 150)))
    return dict(policy='workload_log_scale_v1', **metrics, complexity_score=round(score, 4),
                max_kakao_calls=15 + round((calls - 15) * score),
                max_seconds=round(30 + (seconds - 30) * score),
                daily_limit_unchanged=True, calibration_samples=profile['samples'])
