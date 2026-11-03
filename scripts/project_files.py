"""Required local files for the optimization project."""

from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
ETA_FILES = (
    "manifest.json",
    "best.pt",
    "preprocessor.json",
    "calibrator.json",
    "seoul_roads_v2_elevation.json.gz",
    "data/stations_current.csv",
    "filtered_train_stats/manifest.json",
    "filtered_train_stats/od_full.parquet",
    "filtered_train_stats/hier_origin.parquet",
    "filtered_train_stats/hier_destination.parquet",
    "filtered_train_stats/hier_district_pair.parquet",
)
INVENTORY_FILES = ("model_metadata.json", "weekly_rates.csv", "station_activity.csv")
TRANSIT_FILES = (
    "bus_raw.zip",
    "bus_web_timetables.json",
    "bus_section_times.json.gz",
    "bus_section_validation.json",
    "rail_transfer_times.csv",
    "rail_transfer_times.source.json",
    "files/bus_coordinates.xlsx",
    "bus_route_nodes.dat",
    "prepared.json",
    "README.md",
    "transit_geometry.sqlite",
    "files/subway_coordinate_corrections.json",
    "files/bus_coordinate_corrections.json",
)


def required_paths():
    return (
        collected_timetable_paths()
        + [f"models/eta_v5_2_1/{p}" for p in ETA_FILES]
        + [f"models/inventory_final/{p}" for p in INVENTORY_FILES]
        + [
            "data/inventory/station_crosswalk.csv",
            "data/inventory/station_location_updates.json",
            "notebooks/02_시간표최적화.ipynb",
            "requirements.txt",
            "README.md",
            "src/project_service.py",
            "src/route_progress.py",
            "src/road_startup_cache.py",
            "src/transit_seed.py",
            "src/search_budget.py",
            "src/portable_transfers.py",
            "data/reference/walking_transfers.json",
            "data/reference/walking_transfers.sqlite.gz",
            "data/reference/search_budget_calibration.json",
            "src/transit_coordinates.py",
            "src/station_coordinates.py",
            "src/map_schedule_collection.py",
            "src/collected_rail.py",
            "src/rail_access.py",
            "src/bus_web_timetables.py",
            "src/bus_section_times.py",
            "scripts/collect_bus_timetables.py",
            "scripts/prepare_bus_section_times.py",
            "scripts/evaluate_bus_section_times.py",
            "버스시간표_수집.cmd",
            "notebooks/04_버스시간표수집.ipynb",
            "scripts/collect_map_timetables.py",
            "scripts/check_timetable_fallback.py",
            "시간표_수집.cmd",
            "notebooks/03_시간표수집.ipynb",
            "docs/TIMETABLE_COLLECTION.md",
            "examples/python_usage.py",
            "notebooks/01_기능실행.ipynb",
            "docs/USAGE.md",
            "docs/SCREENSHOT_IMPORT.md",
            "docs/PERSONALIZATION.md",
            "설치.cmd",
            "노트북_실행.cmd",
            "프로젝트_점검.cmd",
            "scripts/install_project.py",
            "scripts/create_environment.py",
            "requirements-web.txt",
            "웹_설치.cmd",
            "웹_실행.cmd",
            "웹_종료.cmd",
            "외부접속_실행.cmd",
            "src/web_server.py",
            "src/web_accounts.py",
            "scripts/serve.py",
            "scripts/web_runtime.py",
            "scripts/check_web.py",
            "scripts/public_tunnel.py",
            "scripts/stop_web.ps1",
            "docs/WEB.md",
            "ngrok_설정.cmd",
            "src/web_tunnel_config.py",
            "web/index.html",
            "web/base.css",
            "web/app.css",
            "web/app.js",
            "web/assets/logo.png",
            "web/assets/full.webp",
            "web/assets/head.webp",
            "src/candidate_edges.py",
            "src/scheduled_runtime.py",
            "src/eta_service.py",
            "src/shared_assets.py",
            "data/reference/manifest.json",
            "data/reference/osm_station_routes.sqlite.gz",
            "data/reference/NOTICE.md",
            "src/history_lookup.py",
            "src/prediction_identity.py",
            "src/personalization.py",
            "src/personal_affine.py",
            "src/inventory_service.py",
            "src/transit_schedule.py",
            "src/kma_weather_forecast_provider.py",
        ]
        + [
            "requirements-ocr.txt",
            "OCR_설치.cmd",
            "scripts/prepare_ocr.py",
            "models/ocr/manifest.json",
            "models/ocr/craft_mlt_25k.pth",
            "models/ocr/korean_g2.pth",
            "models/ocr/NOTICE.md",
            "models/ocr/LICENSE-EasyOCR.txt",
            "models/ocr/LICENSE-CRAFT.txt",
            "src/ocr_dependencies.py",
            "src/screenshot_history.py",
            "src/screenshot_manual.py",
            "src/open_meteo_history.py",
            "src/historical_weather.py",
            "data/weather/training_weather.parquet",
            "data/weather/training_weather_locations.json",
            "data/personalization/bicycle_number_type_lookup.csv",
        ]
        + [f"data/transit/{p}" for p in TRANSIT_FILES]
        + [
            f"data/transit/files/{p}"
            for p in (
                "bus_coordinates.xlsx",
                "subway_timetable.csv",
                "subway_coordinates.csv",
                "subway_line9_coordinates.csv",
                "national_rail_stations.xlsx",
                "sources.json",
                "bus_coordinates_supplement.csv",
                "bus_coordinates_supplement.provenance.json",
            )
        ]
        + [
            "data/transit/extended_rail/manifest.json",
            "data/transit/extended_rail/normalized.csv",
            "data/transit/geometry_sources/rail_lines/sources.json",
        ]
        + [
            f"data/transit/geometry_sources/{p}"
            for p in (
                "bus_spatial_links.csv",
                "bus_route_links.csv",
                "bus_route_via_nodes.csv",
                "bus_link_mapping.csv",
            )
        ]
        + [
            f"data/transit/extended_rail/{p}"
            for p in (
                "korail_25745.xlsx",
                "korail_25746.xlsx",
                "korail_25702.xlsx",
                "arex_20251229.xlsx",
                "arex_20261023.xlsx",
                "discovery/shinbundang.html",
            )
        ]
        + [
            f"data/transit/extended_rail/{prefix}_{i:02d}.html"
            for prefix, count in (("shinbundang", 16), ("ui", 13), ("sillim", 11))
            for i in range(1, count + 1)
        ]
    )


def collected_timetable_paths():
    root = ROOT / "data/transit/map_timetables"
    paths = [root / "manifest.json", root / "lanes.json.gz"]
    if paths[0].exists():
        manifest = json.loads(paths[0].read_text(encoding="utf-8"))
        version = (root / manifest["version"]).resolve()
        if not version.is_relative_to(root.resolve()):
            raise ValueError("Invalid collected timetable version")
        paths += [version / "snapshot.json", version / "stops.json"]
        for tables in manifest["tables"].values():
            for name in tables.values():
                path = (version / name).resolve()
                if not path.is_relative_to(version):
                    raise ValueError("Invalid collected timetable table")
                paths.append(path)
    return [str(path.relative_to(ROOT)) for path in paths]
