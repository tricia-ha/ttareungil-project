"""Use installed web packages, with an optional ABI-checked local runtime."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def prepare():
    try:
        import fastapi, uvicorn, qrcode

        return
    except ImportError:
        pass
    from project_paths import private_root
    from ocr_dependencies import compatible_development_wheels

    folder = private_root() / "web_dependencies"
    if folder.is_dir() and compatible_development_wheels(folder):
        sys.path.insert(0, str(folder))
