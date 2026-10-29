"""Cache OSM rail geometry exports; no keys or per-route network requests."""

from pathlib import Path
from datetime import datetime, timezone
import requests
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "data" / "transit" / "geometry_sources" / "rail_lines"
FILES = {
    **{f"{i}호선": f"line_{i}.geojson" for i in range(1, 10)},
    "경의중앙선": "gyeongui_jungang_line.geojson",
    "수인분당선": "suin_bundang_line.geojson",
    "경춘선": "gyeongchun_line.geojson",
    "공항철도": "airport_line.geojson",
    "신분당선": "shinbundang_line.geojson",
    "우이신설선": "ui_sinseol_light_rail_line.geojson",
    "신림선": "sillim_line.geojson",
}

if __name__ == "__main__":
    FOLDER.mkdir(parents=True, exist_ok=True)
    manifest = FOLDER / "sources.json"
    previous = (
        json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    )
    with requests.Session() as session:
        commit = previous.get("commit")
        if not commit:
            r = session.get(
                "https://api.github.com/repos/jebowe3/seoul_transportation/commits/main",
                timeout=30,
            )
            r.raise_for_status()
            commit = r.json()["sha"]
        records = []
        for line, filename in FILES.items():
            url = f"https://raw.githubusercontent.com/jebowe3/seoul_transportation/{commit}/{filename}"
            path = FOLDER / filename
            if not path.exists():
                r = session.get(url, timeout=35)
                r.raise_for_status()
                data = r.json()
                if data.get("type") != "FeatureCollection":
                    raise ValueError("Not GeoJSON: " + filename)
                path.write_bytes(r.content)
            data = json.loads(path.read_text(encoding="utf-8"))
            records.append(
                {
                    "line": line,
                    "file": filename,
                    "url": url,
                    "features": len(data["features"]),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
            print(line, records[-1]["features"], flush=True)
    manifest.write_text(
        json.dumps(
            {
                "commit": commit,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "attribution": "OpenStreetMap contributors; OSM geometry export by jebowe3",
                "geometry_date": "unknown; repository snapshot only, not guaranteed current",
                "files": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
