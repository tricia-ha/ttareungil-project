"""Required local files for the optimization project."""

from pathlib import Path

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
    "bus_route_nodes.dat",
    "prepared.json",
    "README.md",
    "transit_geometry.sqlite",
)


def required_paths():
    return (
        [f"models/eta_v5_2_1/{p}" for p in ETA_FILES]
        + [f"models/inventory_final/{p}" for p in INVENTORY_FILES]
        + [
            "data/inventory/station_crosswalk.csv",
            "notebooks/02_시간표최적화.ipynb",
            "requirements.txt",
            "README.md",
            "src/project_service.py",
            "src/route_progress.py",
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
