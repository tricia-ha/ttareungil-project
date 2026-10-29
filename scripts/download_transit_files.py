"""Download public static files without API credentials. No models are executed."""

from pathlib import Path
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from html import unescape
import requests

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "data" / "transit" / "files"


def portal_file(session, dataset_id):
    page = f"https://www.data.go.kr/data/{dataset_id}/fileData.do"
    response = session.get(page, timeout=40)
    response.raise_for_status()
    match = re.search(r'"contentUrl"\s*:\s*"([^"]+)"', response.text)
    if not match:
        raise ValueError(f"공개 파일 주소를 찾지 못했습니다: {page}")
    return page, unescape(match.group(1))


def download_static_files():
    FOLDER.mkdir(parents=True, exist_ok=True)
    records = []
    with requests.Session() as session:
        for dataset, name in [
            ("15098251", "subway_timetable.csv"),
            ("15099316", "subway_coordinates.csv"),
            ("15041335", "subway_line9_coordinates.csv"),
        ]:
            page, url = portal_file(session, dataset)
            path = FOLDER / name
            if not path.is_file():
                response = session.get(url, timeout=120)
                response.raise_for_status()
                if (
                    len(response.content) < 100
                    or b"<html" in response.content[:500].lower()
                ):
                    raise ValueError("파일 대신 오류 페이지가 반환되었습니다: " + name)
                part = path.with_suffix(path.suffix + ".part")
                part.write_bytes(response.content)
                part.replace(path)
            records.append(
                {
                    "file": name,
                    "source": page,
                    "download_url": url,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
            print(name, path.stat().st_size, "bytes", flush=True)
        page = "https://data.seoul.go.kr/dataList/OA-15067/F/1/datasetView.do"
        response = session.get(
            "https://data.seoul.go.kr/dataList/fileView.do",
            params={"infId": "OA-15067", "srvType": "F"},
            timeout=40,
        )
        response.raise_for_status()
        text = response.content.decode("cp949", errors="replace")
        # Each row contains a date-bearing file name and its documented download seq.
        candidates = []
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", text, re.S | re.I):
            day = re.search(r"(20\d{6})", row)
            seq = re.search(r"downloadFile\('([0-9]+)'\)", row)
            if day and seq and ".xlsx" in row:
                candidates.append((day.group(1), seq.group(1)))
        if not candidates:
            raise ValueError("서울시 버스 좌표 XLSX 파일을 찾지 못했습니다.")
        day, seq = max(candidates)
        name = "bus_coordinates.xlsx"
        path = FOLDER / name
        url = "https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do"
        if not path.is_file():
            response = session.post(
                url, data={"infId": "OA-15067", "seq": seq, "infSeq": "1"}, timeout=90
            )
            response.raise_for_status()
            if not response.content.startswith(b"PK"):
                raise ValueError(
                    "버스 좌표 다운로드가 XLSX가 아닙니다. 폼 정보를 확인하세요."
                )
            path.write_bytes(response.content)
        records.append(
            {
                "file": name,
                "source": page,
                "source_date": day,
                "seq": seq,
                "download_url": url,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        print(name, path.stat().st_size, "bytes", flush=True)
        name = "national_rail_stations.xlsx"
        path = FOLDER / name
        page = "https://data.kric.go.kr/rips/M_01_01/detail.do?id=32"
        url = "https://data.kric.go.kr/rips/dataset/download.file?type=filedata&id=32&operation=1"
        if not path.is_file():
            response = session.get(url, timeout=90)
            response.raise_for_status()
            if not response.content.startswith(b"PK"):
                raise ValueError("전국 역사 파일이 XLSX가 아닙니다.")
            path.write_bytes(response.content)
        records.append(
            {
                "file": name,
                "source": page,
                "download_url": url,
                "source_date": "2026-06-30",
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        print(name, path.stat().st_size, "bytes", flush=True)
    manifest = {
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "files": records,
    }
    (FOLDER / "sources.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect-bus-form", action="store_true")
    args = parser.parse_args()
    if args.inspect_bus_form:
        t = requests.get(
            "https://data.seoul.go.kr/dataList/fileView.do",
            params={"infId": "OA-15067", "srvType": "F"},
            timeout=40,
        ).text
        p = t.find("function downloadFile")
        print(t[p : p + 700])
        print("\n".join(re.findall(r"<input[^>]+>", t)))
    else:
        download_static_files()
