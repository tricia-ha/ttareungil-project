"""Fixed search budgets and historical workload diagnostics."""

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


def select_budget(metrics=None, mode='integrated', *, call_ceiling=30, time_ceiling=100, reference=None):
    """Fixed ceilings, independent of candidate density and legacy calibration.

    Smaller explicit limits are respected. Legacy adaptive arguments remain
    accepted so saved notebooks cannot silently restore the old policy.
    """
    mode_calls, mode_seconds = {"transit": (20, 50), "subway": (20, 50),
                                "bicycle": (10, 30)}.get(mode, (30, 100))
    calls = min(mode_calls, int(call_ceiling))
    seconds = float(time_ceiling if time_ceiling is not None else mode_seconds)
    if calls <= 0 or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('탐색 한도는 양수여야 합니다.')
    seconds = min(float(mode_seconds), seconds)
    return dict(policy='fixed_limits_v1', max_kakao_calls=calls,
                max_seconds=seconds, daily_limit_unchanged=True)
