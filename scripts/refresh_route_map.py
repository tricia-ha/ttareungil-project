"""Refresh saved route geometry only; no prediction, search or API calls."""

from pathlib import Path
import json
import sys
import copy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from transit_geometry import attach_transit_geometry, prepare_geometry_database
from kakao_optimizer import map_html
from project_paths import RUNTIME_OUTPUT_DIR

if __name__ == "__main__":
    prepare_geometry_database(ROOT / "data" / "transit")
    folder = RUNTIME_OUTPUT_DIR / "scheduled"
    path = folder / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    before = copy.deepcopy(result)
    attach_transit_geometry(result, ROOT / "data" / "transit")
    for old, new in zip(before["routes"], result["routes"]):
        assert old["total_minutes"] == new["total_minutes"]
        for a, b in zip(old["legs"], new["legs"]):
            assert {
                k: v
                for k, v in a.items()
                if not k.startswith(("geometry", "display_geometry"))
            } == {
                k: v
                for k, v in b.items()
                if not k.startswith(("geometry", "display_geometry"))
            }
    backup = folder / "result.before_geometry.json"
    if not backup.exists():
        backup.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "routes.html").write_text(map_html(result), encoding="utf-8")
    print(result["geometry_summary"])
