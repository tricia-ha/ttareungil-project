"""Static checks only: no model loading, training, inference or network calls."""

from pathlib import Path
import ast
import csv
import hashlib
import json
import sys
import argparse
from project_files import required_paths

ROOT = Path(__file__).resolve().parents[1]


def check(dependencies=False):
    eta = ROOT / "models/eta_v5_2_1"
    inventory = ROOT / "models/inventory_final"
    required = [
        eta / "manifest.json",
        eta / "best.pt",
        eta / "preprocessor.json",
        eta / "calibrator.json",
        eta / "seoul_roads_v2_elevation.json.gz",
        eta / "data/stations_current.csv",
        eta / "filtered_train_stats/od_full.parquet",
        eta / "filtered_train_stats/hier_origin.parquet",
        eta / "filtered_train_stats/hier_destination.parquet",
        eta / "filtered_train_stats/hier_district_pair.parquet",
        inventory / "model_metadata.json",
        inventory / "weekly_rates.csv",
        inventory / "station_activity.csv",
        ROOT / "data/inventory/station_crosswalk.csv",
        ROOT / "notebooks/02_시간표최적화.ipynb",
        ROOT / "data/transit/README.md",
    ]
    required += [ROOT / name for name in required_paths()]
    required += [
        ROOT / "data/transit/files" / name
        for name in (
            "bus_coordinates.xlsx",
            "subway_timetable.csv",
            "subway_coordinates.csv",
            "subway_line9_coordinates.csv",
            "national_rail_stations.xlsx",
            "sources.json",
        )
    ]
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(path)
    rail_folder = ROOT / "data/transit/geometry_sources/rail_lines"
    for item in json.loads((rail_folder / "sources.json").read_text(encoding="utf-8"))[
        "files"
    ]:
        path = (rail_folder / item["file"]).resolve()
        if not path.is_relative_to(rail_folder.resolve()) or not path.is_file():
            raise ValueError("Missing or invalid rail shape file: " + item["file"])
    manifest = json.loads((eta / "manifest.json").read_text(encoding="utf-8-sig"))
    if "v5_2_1" not in manifest["model_version"]:
        raise ValueError("ETA version is not V5.2.1")
    if (eta / manifest["artifact_dir"]).resolve() != eta.resolve():
        raise ValueError("ETA artifact directory mismatch")
    metadata = json.loads(
        (inventory / "model_metadata.json").read_text(encoding="utf-8-sig")
    )
    with (inventory / "weekly_rates.csv").open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != metadata["weekly_rates_sha256"]:
        raise ValueError("Inventory rate/metadata hash mismatch")
    with (inventory / "weekly_rates.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = sum(1 for _ in csv.DictReader(f))
    if rows != metadata["scope_stations"] * 168:
        raise ValueError("Inventory rate table is incomplete")
    with (ROOT / "data/personalization/bicycle_number_type_lookup.csv").open(
        encoding="utf-8-sig", newline=""
    ) as f:
        bike_rows = list(csv.DictReader(f))
    if not bike_rows or len({r["app_bicycle_number"] for r in bike_rows}) != len(
        bike_rows
    ):
        raise ValueError("Bicycle type lookup is empty or has duplicate numbers")
    weather = json.loads(
        (ROOT / "data/weather/training_weather_locations.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        len(weather.get("locations", [])) != 26
        or len({p["location"] for p in weather["locations"]}) != 26
    ):
        raise ValueError("Training weather must retain the 26 original query points")
    with (eta / "data/stations_current.csv").open(
        encoding="utf-8-sig", newline=""
    ) as f:
        station_rows = list(csv.DictReader(f))
    if not station_rows or len({int(r["station_id"]) for r in station_rows}) != len(
        station_rows
    ):
        raise ValueError("Station catalog is empty or has duplicate station numbers")
    for folder in ("src", "scripts", "tests", "examples"):
        for path in (ROOT / folder).glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for path in (ROOT / "notebooks").glob("*.ipynb"):
        nb = json.loads(path.read_text(encoding="utf-8-sig"))
        # Historical experiments may contain IPython magic; only active notebooks
        # are strict Python. Keep archived experiment outputs and code intact.
        if path.parent.name == "eta":
            continue
        for i, cell in enumerate(nb["cells"]):
            if cell["cell_type"] == "code":
                source = cell["source"]
                ast.parse(
                    "".join(source) if isinstance(source, list) else source,
                    filename=f"{path.name}:cell{i}",
                )
    handoff = ROOT / "HANDOFF_MANIFEST.json"
    if handoff.exists():
        for item in json.loads(handoff.read_text(encoding="utf-8"))["files"]:
            path = (ROOT / item["path"]).resolve()
            if not path.is_relative_to(ROOT.resolve()):
                raise ValueError("Invalid handoff manifest path")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != item["sha256"]:
                if item["path"].startswith("notebooks/"):
                    print(
                        "Notebook changed since sharing (inputs/outputs may change): "
                        + item["path"]
                    )
                else:
                    raise ValueError("Handoff file modified: " + item["path"])
        print("Handoff SHA256 checked (notebook edits allowed)")
    if dependencies:
        import importlib.metadata as package_metadata
        from packaging.requirements import Requirement

        problems = []
        for line in (
            (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        ):
            if not line.strip() or line.startswith("#"):
                continue
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            try:
                version = package_metadata.version(requirement.name)
                if version not in requirement.specifier:
                    problems.append(
                        f"{requirement.name}: {version} not in {requirement.specifier}"
                    )
            except package_metadata.PackageNotFoundError:
                problems.append(requirement.name + ": missing")
        if problems:
            raise ValueError("Dependencies: " + "; ".join(problems))
        # Versions alone do not catch binary ABI problems. Import in this process;
        # no service constructor, checkpoint, graph or inference is used.
        for name in (
            "numpy",
            "pandas",
            "torch",
            "torchvision",
            "sklearn",
            "pyarrow",
            "pydantic",
            "networkx",
            "holidays",
            "openpyxl",
            "bs4",
        ):
            __import__(name)
        print("Runtime dependency imports OK; no model constructed.")
    print("Static checks OK; models and APIs were not executed.")
    print("ETA:", manifest["model_version"])
    print(
        "Inventory:",
        metadata["selected_model"],
        metadata["scope_stations"],
        "stations",
        rows,
        "rates",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dependencies", action="store_true")
    options = parser.parse_args()
    ROOT = options.root.resolve()
    try:
        check(options.dependencies)
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(1)
