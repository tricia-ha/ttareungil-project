"""Fetch static route-shape source files from public Seoul T-data downloads."""

from pathlib import Path
import re
import json
import requests
import hashlib
import argparse

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "data" / "transit" / "geometry_sources"


def download(dataset_id, name, session):
    FOLDER.mkdir(parents=True, exist_ok=True)
    page = f"https://t-data.seoul.go.kr/dataprovide/trafficdataviewfile.do?data_id={dataset_id}"
    path = FOLDER / name
    if not path.is_file():
        r = session.get(page, timeout=40)
        r.raise_for_status()
        options = re.findall(r'<option value="([0-9]+)"', r.text)
        if not options:
            raise ValueError("다운로드 항목 없음: " + page)
        r = session.get(
            "https://t-data.seoul.go.kr/dataprovide/download.do",
            params={"id": options[0]},
            timeout=120,
        )
        r.raise_for_status()
        if b"<html" in r.content[:500].lower():
            raise ValueError("파일 대신 HTML이 반환되었습니다.")
        path.write_bytes(r.content)
    print(name, path.stat().st_size, "bytes", flush=True)
    return {
        "file": name,
        "source": page,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rail", action="store_true")
    args = parser.parse_args()
    if args.rail:
        FOLDER.mkdir(parents=True, exist_ok=True)
        target = FOLDER / "seoul_metro_routes_osm.json"
        if not target.is_file():
            query = """[out:json][timeout:120];relation["type"="route"]["route"~"^(subway|train)$"]
                     ["ref"~"^[1-9]$"](37.35,126.75,37.75,127.20);out geom;"""
            response = requests.get(
                "https://overpass-api.de/api/interpreter",
                params={"data": query},
                timeout=150,
            )
            response.raise_for_status()
            content = response.json()
            if content.get("remark"):
                raise ValueError(content["remark"])
            target.write_text(
                json.dumps(content, separators=(",", ":")), encoding="utf-8"
            )
        content = json.loads(target.read_text(encoding="utf-8"))
        print("OSM rail relations:", len(content.get("elements", [])), flush=True)
        raise SystemExit(0)
    with requests.Session() as session:
        rows = [
            download(i, name, session)
            for i, name in [
                (63, "bus_spatial_links.csv"),
                (57, "bus_route_links.csv"),
                (58, "bus_route_via_nodes.csv"),
                (13, "bus_nodes.csv"),
                (52, "bus_link_mapping.csv"),
            ]
        ]
    (FOLDER / "sources.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
