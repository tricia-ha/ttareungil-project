"""Small synthetic forward-only comparison; not real-user accuracy evidence."""

from pathlib import Path
import sys, json, math, csv
from statistics import fmean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import numpy as np
from personalization import PersonalizationPolicy, _weighted_median
from personal_affine import fit_affine, personalized_minutes
from project_paths import RUNTIME_OUTPUT_DIR


def legacy(rows, p):
    values = [
        max(-p.max_abs_log_ratio, min(p.max_abs_log_ratio, math.log(y / x)))
        for x, y in rows
    ]
    weights = [x / max(r[0] for r in rows) for x, y in rows]
    center = _weighted_median(values, weights)
    mad = _weighted_median([abs(v - center) for v in values], weights)
    effective = (sum(weights) ** 2 / sum(w * w for w in weights)) / (
        1 + (1.4826 * mad / p.instability_scale) ** 2
    )
    return max(
        p.minimum_factor,
        min(
            p.maximum_factor,
            math.exp(effective / (effective + p.prior_strength) * center),
        ),
    )


def run():
    p = PersonalizationPolicy()
    results = []
    for name, a, b, outliers, narrow in [
        ("scale_only", 0.8, 0, False, False),
        ("offset_only", 1, -3, False, False),
        ("scale_and_offset", 0.85, -2, False, False),
        ("narrow_duration", 0.85, -2, False, True),
        ("occasional_long_rides", 0.9, -2, True, False),
    ]:
        rng = np.random.default_rng(20261029)
        x = rng.uniform(4, 40, 180) if not narrow else rng.uniform(9.8, 10.2, 180)
        y = np.maximum(0.5, a * x + b + rng.normal(0, 0.4, len(x)))
        if outliers:
            y[::17] += 20
        rows = []
        old = []
        new = []
        base = []
        for i, (t, actual) in enumerate(zip(x, y)):
            if i >= 20:
                old.append(abs(actual - t * legacy(rows, p)))
                new.append(abs(actual - personalized_minutes(t, fit_affine(rows, p))))
                base.append(abs(actual - t))
            rows.append((float(t), float(actual)))
        fit = fit_affine(rows, p)
        results.append(
            dict(
                scenario=name,
                test_records=len(new),
                base_mae_min=fmean(base),
                multiplicative_mae_min=fmean(old),
                affine_mae_min=fmean(new),
                factor=fit["factor"],
                offset_min=fit["offset_min"],
            )
        )
    folder = RUNTIME_OUTPUT_DIR / "personal_affine_comparison"
    folder.mkdir(parents=True, exist_ok=True)
    report = dict(
        evaluation="synthetic_forward_only_first20_warmup",
        real_user_validation=False,
        results=results,
        policy=dict(
            prior_strength=p.prior_strength, instability_scale=p.instability_scale
        ),
    )
    (folder / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (folder / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    run()
