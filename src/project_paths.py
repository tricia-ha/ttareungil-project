"""Portable assets; optional sibling private directory keeps local state out of sharing."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def private_root(root=None):
    root = Path(root or PROJECT_ROOT).resolve()
    return root.parent / (root.name + "_local")


def mutable_root(name, root=None):
    root = Path(root or PROJECT_ROOT).resolve()
    private = private_root(root) / name
    return private if private.is_dir() else root / name


CACHE_ROOT = mutable_root("cache")
OUTPUT_ROOT = mutable_root("outputs")
ETA_MODEL_DIR = PROJECT_ROOT / "models" / "eta_v5_2_1"
INVENTORY_MODEL_DIR = PROJECT_ROOT / "models" / "inventory_final"
INVENTORY_DATA_DIR = PROJECT_ROOT / "data" / "inventory"
INVENTORY_SNAPSHOT_DIR = (
    private_root() / "inventory"
    if (private_root() / "inventory").is_dir()
    else INVENTORY_DATA_DIR
)
INVENTORY_REPORT_DIR = PROJECT_ROOT / "reports" / "inventory"
RUNTIME_DATA_DIR = PROJECT_ROOT / "data" / "runtime"
RUNTIME_CACHE_DIR = CACHE_ROOT / "runtime"
RUNTIME_OUTPUT_DIR = OUTPUT_ROOT / "runtime"
TRANSIT_DATA_DIR = PROJECT_ROOT / "data" / "transit"
DEFAULT_GTFS_PATH = TRANSIT_DATA_DIR / "seoul_gtfs.zip"
STATION_CATALOG = ETA_MODEL_DIR / "data" / "stations_current.csv"
STATION_CROSSWALK = INVENTORY_DATA_DIR / "station_crosswalk.csv"

PATH_ENV_KEYS = frozenset(
    {
        "TRANSIT_GTFS_PATH",
        "MULTIMODAL_NETWORK_PATH",
        "MULTIMODAL_INVENTORY_PATH",
        "MULTIMODAL_CROSSWALK_PATH",
        "BICYCLE_CACHE_DIR",
        "BICYCLE_WEATHER_CACHE",
        "BICYCLE_KMA_CACHE",
    }
)


def resolve_project_path(value):
    """Resolve user paths relative to the project, not the launch directory."""
    path = Path(value).expanduser()
    resolved = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    for name in ("cache", "outputs"):
        original = (PROJECT_ROOT / name).resolve()
        if resolved.is_relative_to(original):
            return mutable_root(name) / resolved.relative_to(original)
    return resolved


def load_project_env():
    """Read optional local .env, without overriding existing environment values."""
    import os

    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        path = private_root() / ".env"
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in (
            "KAKAO_REST_API_KEY",
            "KMA_API_KEY",
            "KMA_AUTH_KEY",
            "KMA_HISTORY_API_KEY",
            "SEOUL_API_KEY",
            "BICYCLE_PERSONALIZATION_ID",
            "PERSONALIZATION_HISTORY_ALPHA",
            "PERSONALIZATION_HISTORY_METHOD",
            "PERSONALIZATION_HISTORY_MODE",
            "WEB_ALLOWED_HOSTS",
            "WEB_MAX_KAKAO_CALLS",
            "NGROK_AUTHTOKEN",
            "NGROK_DOMAIN",
            *PATH_ENV_KEYS,
        ):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if value:
            os.environ.setdefault(key, value)
    for key in PATH_ENV_KEYS:
        if os.environ.get(key, "").strip():
            os.environ[key] = str(resolve_project_path(os.environ[key]))
