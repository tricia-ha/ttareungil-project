"""Robust, regularized scale + offset calibration of personal P50 times."""

import math
from statistics import median


def fit_affine(rows, policy):
    if not rows:
        return dict(
            factor=1.0,
            offset_min=0.0,
            minimum_ratio=policy.minimum_factor,
            maximum_ratio=policy.maximum_factor,
            base_time_range_min=None,
            iterations=0,
            fit_effective_records=0.0,
            fit_shrinkage_weight=0.0,
        )
    x = [float(r[0]) for r in rows]
    y = [float(r[1]) for r in rows]
    scale = median(x)
    target = [b - a for a, b in zip(x, y)]
    center = median(target)
    mad = median(abs(v - center) for v in target)
    # Reuse the existing noise regularization; no record-count cutoff is added.
    delta = max(1.4826 * mad, policy.instability_scale * scale)
    a, b = 1.0, 0.0
    for iteration in range(40):
        residual = [v - ((a - 1) * t + b) for t, v in zip(x, target)]
        residual_center = median(residual)
        noise = 1.4826 * median(abs(r - residual_center) for r in residual)
        reliability = 1 / (1 + (noise / (policy.instability_scale * scale)) ** 2)
        prior = policy.prior_strength / reliability
        weights = [min(1.0, delta / max(abs(r), 1e-12)) for r in residual]
        total = sum(weights)
        mean_x = sum(w * t for w, t in zip(weights, x)) / total
        mean_r = sum(w * r for w, r in zip(weights, target)) / total
        variance = sum(w * (t - mean_x) ** 2 for w, t in zip(weights, x))
        covariance = sum(
            w * (t - mean_x) * (r - mean_r) for w, t, r in zip(weights, x, target)
        )
        slope = covariance / (variance + prior * scale * scale)
        next_a = max(policy.minimum_factor, min(policy.maximum_factor, 1 + slope))
        shift = mean_r * total / (total + prior)
        next_b = shift - (next_a - 1) * mean_x
        change = max(abs(next_a - a), abs(next_b - b) / scale)
        a, b = next_a, next_b
        if change < 1e-8:
            break
    ratios = [v / t for t, v in zip(x, y)]
    # Bound extrapolation by observed ratios and the existing policy bounds.
    # Clipping between positive lines preserves increasing, positive ETA.
    return dict(
        factor=a,
        offset_min=b,
        minimum_ratio=min(policy.minimum_factor, min(ratios)),
        maximum_ratio=max(policy.maximum_factor, max(ratios)),
        base_time_range_min=[min(x), max(x)],
        iterations=iteration + 1,
        fit_effective_records=total * reliability,
        fit_shrinkage_weight=total / (total + prior),
    )


def personalized_minutes(base, profile):
    base = float(base)
    a = float(profile.get("factor", 1.0))
    b = float(profile.get("offset_min", 0.0))
    low = float(profile.get("minimum_ratio", 0.7))
    high = float(profile.get("maximum_ratio", 1.5))
    if (
        not all(math.isfinite(v) for v in (base, a, b, low, high))
        or base <= 0
        or not 0 < low <= 1 <= high
    ):
        raise ValueError("개인화 시간 또는 보정 범위를 확인해 주세요.")
    return max(base * low, min(base * high, a * base + b))
