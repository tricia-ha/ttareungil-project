"""Package existing training weather and its exact query coordinates for reuse."""

from pathlib import Path
import argparse, hashlib, json
import pandas as pd


def main():
    p = argparse.ArgumentParser()
    p.add_argument("training_csv")
    args = p.parse_args()
    source = Path(args.training_csv)
    out = Path(__file__).resolve().parents[1] / "data" / "weather"
    out.mkdir(parents=True, exist_ok=True)
    frame = pd.read_csv(source, encoding="utf-8-sig")
    frame["valid_at"] = pd.to_datetime(frame.valid_at)
    frame["available_at"] = pd.to_datetime(frame.available_at)
    points = frame[["location", "latitude", "longitude"]].drop_duplicates()
    if points.location.duplicated().any():
        raise ValueError("A location has conflicting query coordinates.")
    frame.to_parquet(out / "training_weather.parquet", index=False)
    manifest = dict(
        source="open_meteo_archive_training_data",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        locations=points.to_dict("records"),
        start=str(frame.valid_at.min()),
        end=str(frame.valid_at.max()),
        rows=len(frame),
        timezone="Asia/Seoul",
        wind_speed_unit="ms",
        models_parameter=None,
        notes="Training request used archive default model selection; exact historical service revisions are not pinned.",
    )
    (out / "training_weather_locations.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        len(frame),
        "rows,",
        len(points),
        "locations;",
        (out / "training_weather.parquet").stat().st_size,
        "bytes",
    )


if __name__ == "__main__":
    main()
