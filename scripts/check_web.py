"""Check web files and imports without starting a server or a model."""

from pathlib import Path
import sys
from web_runtime import prepare

prepare()
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
try:
    import fastapi, uvicorn, qrcode, PIL
    from PIL import Image
    from web_server import create_app

    for name in (
        "index.html",
        "app.js",
        "app.css",
        "base.css",
        "assets/logo.png",
        "assets/full.webp",
        "assets/head.webp",
    ):
        if not (root / "web" / name).is_file():
            raise FileNotFoundError(name)
    create_app()
    print("Web files and runtime imports OK. No model or network request was started.")
except Exception as exc:
    print(
        "Web check failed: "
        + type(exc).__name__
        + ". Run 웹_설치.cmd and check the project files.",
        file=sys.stderr,
    )
    raise SystemExit(1)
