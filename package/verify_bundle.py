"""Check package files and run one offline prediction without a live API call."""

from pathlib import Path
import json

from eta_service import BicycleETAService


ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
MODEL = (ROOT / MANIFEST.get("artifact_dir", "artifacts")).resolve()
REQUIRED = [
    ROOT / "manifest.json",
    MODEL / "best.pt",
    MODEL / "preprocessor.json",
    MODEL / "calibrator.json",
    MODEL / "seoul_roads_v2_elevation.json.gz",
    MODEL / "filtered_train_stats" / "manifest.json",
    MODEL / "filtered_train_stats" / "od_full.parquet",
    ROOT / "data" / "stations_current.csv",
]


missing = [str(path) for path in REQUIRED if not path.is_file()]
if missing:
    raise FileNotFoundError(f"Missing package files: {missing}")

with BicycleETAService(ROOT) as service:
    result = service.predict(
        start_station_id="2106",
        end_station_id="2144",
        departure="2026-10-15T08:30:00",
    )
    assert result["eta"]["p50_min"] >= 0
    assert result["route"]["distance_m"] > 0
    assert len(result["route"]["geometry"]) >= 2
    print(
        "OK:", result["model"]["version"],
        f"P50={result['eta']['p50_min']:.2f}min",
        f"distance={result['route']['distance_m']:.0f}m",
    )
