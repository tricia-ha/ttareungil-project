"""Download official static timetables. Existing downloads are reused."""

from pathlib import Path
import json
import hashlib
from datetime import datetime, timezone
import requests
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "data" / "transit" / "extended_rail"
RECORDS = []


def fetch(url, name):
    path = FOLDER / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            content = r.content
        except requests.exceptions.SSLError:
            # Use Windows' certificate store, without disabling TLS verification.
            content = subprocess.check_output(
                [
                    "curl.exe",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--location",
                    "--max-time",
                    "60",
                    url,
                ]
            )
        path.write_bytes(content)
    if path.suffix in (".xlsx", ".zip") and not path.read_bytes().startswith(b"PK"):
        raise ValueError("공식 파일 대신 다른 내용이 반환됐습니다: " + name)
    RECORDS.append(
        {
            "file": name,
            "url": url,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )
    print(name, path.stat().st_size, flush=True)
    return path


if __name__ == "__main__":
    FOLDER.mkdir(parents=True, exist_ok=True)
    # Pinned releases: updating timetables is explicit, not a per-query API call.
    for idx, relative in (
        (25745, "202608/28/202608281a045c7905b490"),
        (25746, "202608/28/202608281a045c7cc64640"),
        (25702, "202608/21/202608211a02345f4b6660"),
    ):
        fetch(
            "https://www.korail.com/file/cubedata/COMMON/jfile/" + relative + ".xlsx",
            f"korail_{idx}.xlsx",
        )
    from urllib.parse import quote

    fetch(
        "https://www.airportrailroad.com/static/file/"
        + quote("열차시각표_251229.xlsx"),
        "arex_20251229.xlsx",
    )
    fetch(
        "https://www.airportrailroad.com/file/download/AF202610140000000001",
        "arex_20261023.xlsx",
    )
    fetch(
        "https://www.shinbundang.co.kr/dxline/dxline1.jsp", "discovery/shinbundang.html"
    )
    for i in range(1, 14):
        fetch(
            f"https://www.ui-line.com/html/intro/intro00/intro_00_{i:02d}.php?pGubn=T2",
            f"ui_{i:02d}.html",
        )
    for i in range(1, 12):
        fetch(
            f"https://www.sillimlrt.com/kr/html/sub01/010102{i:02d}.html",
            f"sillim_{i:02d}.html",
        )
    for i in range(1, 17):
        fetch(
            f"https://www.shinbundang.co.kr/dxline/dxline3_1.jsp?st_cd=SB{i:03d}",
            f"shinbundang_{i:02d}.html",
        )
    (FOLDER / "sources.json").write_text(
        json.dumps(
            {"recorded_at": datetime.now(timezone.utc).isoformat(), "files": RECORDS},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
