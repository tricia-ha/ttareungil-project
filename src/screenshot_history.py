"""Local OCR with spatial, fail-closed grouping of one rental card at a time."""

from __future__ import annotations
import csv
import io
import math
import re
import threading
from datetime import datetime, timezone, timedelta
from functools import lru_cache

from project_paths import PROJECT_ROOT, CACHE_ROOT, STATION_CATALOG

KST = timezone(timedelta(hours=9))
_reader = None
_ocr_lock = threading.Lock()


@lru_cache(maxsize=1)
def catalogs():
    with STATION_CATALOG.open(encoding="utf-8-sig") as f:
        stations = {str(int(r["station_id"])): r for r in csv.DictReader(f)}
    lookup = (
        PROJECT_ROOT / "data" / "personalization" / "bicycle_number_type_lookup.csv"
    )
    bikes = {}
    if lookup.exists():
        with lookup.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["status"] == "matched":
                    bikes[r["app_bicycle_number"]] = {
                        "일반자전거": "regular",
                        "새싹자전거": "sprout",
                    }.get(r["bicycle_type"], "unknown")
    return stations, bikes


def recognize(data: bytes):
    """No external image upload. Lazy optional dependency and one shared CPU reader."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data or len(data) > 12 * 1024 * 1024:
        raise ValueError("PNG/JPEG 이미지 한 장을 12MB 이내로 올려 주세요.")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if (
                source.format not in ("PNG", "JPEG")
                or source.width * source.height > 20_000_000
            ):
                raise ValueError("PNG/JPEG, 최대 2천만 화소만 지원합니다.")
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise ValueError("읽을 수 없는 이미지입니다.") from None
    image.thumbnail((1200, 3000))
    with _ocr_lock:
        global _reader
        if _reader is None:
            from ocr_dependencies import load_easyocr

            easyocr = load_easyocr()
            _reader = easyocr.Reader(
                ["ko", "en"],
                gpu=False,
                model_storage_directory=str(CACHE_ROOT / "ocr_models"),
                verbose=False,
            )
        import numpy as np

        result = _reader.readtext(np.asarray(image), detail=1, paragraph=False)
    return [
        dict(
            box=[[float(x), float(y)] for x, y in box],
            text=text,
            confidence=float(conf),
        )
        for box, text, conf in result
    ], image.size


def _compact(s):
    return re.sub(r"\s+", "", s)


def _tokens(tokens):
    result = []
    for t in tokens:
        xs, ys = zip(*t["box"])
        result.append(
            dict(
                t,
                x=min(xs),
                y=min(ys),
                right=max(xs),
                bottom=max(ys),
                cy=(min(ys) + max(ys)) / 2,
                compact=_compact(t["text"]),
            )
        )
    return sorted(result, key=lambda t: (t["cy"], t["x"]))


def _one(values, field):
    if len(values) != 1:
        raise ValueError(field + "을 한 값으로 식별하지 못했습니다.")
    return values[0]


def _number_text(text, kind, card):
    """Correct confusable Latin letters only inside a full numeric-field pattern."""
    raw = _compact(text)
    digit = r"[0-9OoIl|]"
    patterns = {
        "time": rf"{digit}{{1,2}}[:.]?{digit}{{2}}",
        "date": rf"{digit}{{4}}[-./]{digit}{{1,2}}[-./]{digit}{{1,2}}",
        "minutes": rf"{digit}+(?:\.{digit}+)?분",
        "distance": rf"{digit}+(?:\.{digit}+)?[kK][mM]",
        "bike": rf"{digit}{{4,7}}",
        "station": rf"{digit}{{3,5}}",
    }
    if not re.fullmatch(patterns[kind], raw):
        return raw
    corrected = raw.translate(
        str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "|": "1"})
    )
    if corrected != raw:
        change = dict(field=kind, raw=text, normalized=corrected)
        if change not in card["corrections"]:
            card["corrections"].append(change)
    return corrected


def _row_tolerance(a, b):
    return max(2.0, min(a["bottom"] - a["y"], b["bottom"] - b["y"]) * 0.12)


def _at_or_below(value, label):
    return value["cy"] >= label["cy"] - _row_tolerance(value, label)


def normalize_station_name(value):
    return re.sub(r"[^0-9a-z가-힣]", "", str(value or "").casefold())


def resolve_stations(record, observed_names, stations=None):
    """Exact normalized name agreement; name candidates never change the selected ID."""
    if stations is None:
        stations = catalogs()[0]
    result = {}
    for side, field in [
        ("origin", "start_station_id"),
        ("destination", "end_station_id"),
    ]:
        raw = str(record.get(field, "")).strip()
        sid = str(int(raw)) if raw.isdigit() else raw
        selected = stations.get(sid)
        observed = str(observed_names.get(side, "")).strip()
        normalized = normalize_station_name(observed)
        candidates = [
            dict(
                station_id=k,
                station_name=s["station_name"],
                district=s.get("district", ""),
            )
            for k, s in stations.items()
            if normalized and normalize_station_name(s["station_name"]) == normalized
        ]
        expected = selected["station_name"] if selected else None
        if selected is None:
            status = "unknown_id"
        elif normalized and normalized == normalize_station_name(expected):
            status = "matched"
        elif candidates:
            status = "name_mismatch"
        else:
            status = "name_unverified"
        messages = dict(
            matched="번호와 사진 속 이름이 일치합니다.",
            unknown_id="목록에 없는 번호입니다. 사진의 번호를 수정하거나 다시 인식해 주세요. 이름 후보는 자동 연결하지 않습니다. 실제 신규·폐쇄 대여소라면 공식 목록 갱신 후 등록해야 합니다.",
            name_mismatch="번호가 가리키는 대여소와 사진 속 이름이 다릅니다. 등록을 보류합니다.",
            name_unverified="사진 속 이름을 목록과 확인하지 못했습니다. 원본을 보고 번호와 이름을 수정해 주세요.",
        )
        result[side] = dict(
            station_id=sid,
            mapped_name=expected,
            observed_name=observed,
            status=status,
            district=selected.get("district") if selected else None,
            catalog_as_of=selected.get("catalog_as_of") if selected else None,
            candidates=candidates[:10],
            message=messages[status],
        )
    return dict(
        stations=result,
        can_register=all(v["status"] == "matched" for v in result.values()),
    )


def validate_record(
    record, stations=None, bikes=None, now=None, require_known_stations=True
):
    """Revalidate edited records; derive P50 and bicycle type from local artifacts."""
    if stations is None or bikes is None:
        default_stations, default_bikes = catalogs()
        stations = default_stations if stations is None else stations
        bikes = default_bikes if bikes is None else bikes
    result = {
        k: record[k]
        for k in (
            "bicycle_number",
            "start_station_id",
            "end_station_id",
            "started_at",
            "returned_at",
            "actual_minutes",
            "distance_km",
        )
    }
    for field in ("start_station_id", "end_station_id"):
        value = str(record[field]).strip()
        if not value.isdigit() or (
            require_known_stations and str(int(value)) not in stations
        ):
            raise ValueError(
                "대여소 번호를 확인할 수 없습니다. 현재 목록에 없는 과거 대여소는 등록을 보류합니다."
            )
        result[field] = str(int(value))
    if result["start_station_id"] == result["end_station_id"]:
        raise ValueError("같은 대여소 왕복 기록은 이동시간 개인화에 사용하지 않습니다.")
    for field in ("started_at", "returned_at"):
        d = datetime.fromisoformat(record[field])
        if d.utcoffset() is None:
            d = d.replace(tzinfo=KST)
        result[field] = d.isoformat()
    start, end = (
        datetime.fromisoformat(result[k]) for k in ("started_at", "returned_at")
    )
    elapsed = (end - start).total_seconds() / 60
    minutes, distance = float(record["actual_minutes"]), float(record["distance_km"])
    if not all(math.isfinite(v) and v > 0 for v in (elapsed, minutes, distance)):
        raise ValueError("이용시간·거리 또는 대여·반납 순서를 확인해 주세요.")
    if end > (now or datetime.now(timezone.utc)):
        raise ValueError("미래 반납 기록은 등록할 수 없습니다.")
    # Two minute-resolution timestamps can differ from displayed duration by <1 minute.
    if abs(elapsed - minutes) > 1.01:
        raise ValueError(
            "대여·반납 시각과 이용시간이 맞지 않습니다. 다른 카드가 섞이지 않았는지 확인해 주세요."
        )
    bike = str(record["bicycle_number"]).strip().removeprefix("SPB-")
    if not re.fullmatch(r"\d{4,7}", bike):
        raise ValueError("자전거 번호를 확인해 주세요.")
    bike = str(int(bike))
    result.update(
        bicycle_number=bike,
        bike_type=bikes.get(bike, "unknown"),
        actual_minutes=minutes,
        distance_km=distance,
    )
    return result


def extract_cards(tokens, size, stations=None, bikes=None):
    """Anchor at a bike-number header; never fill a missing field from another card."""
    if stations is None or bikes is None:
        ds, db = catalogs()
        stations = ds if stations is None else stations
        bikes = db if bikes is None else bikes
    w, h = size
    ts = _tokens(tokens)
    anchors = [t for t in ts if re.search(r"자전거번호", t["compact"])]
    cards = []
    for index, anchor in enumerate(anchors):
        bottom = anchors[index + 1]["y"] if index + 1 < len(anchors) else h
        group = [t for t in ts if anchor["y"] <= t["cy"] < bottom]
        card = dict(
            index=index,
            bounds=[0, anchor["y"], w, bottom],
            valid=False,
            reviewable=False,
            errors=[],
            warnings=[],
            corrections=[],
            record=None,
        )
        try:
            # The identifier may occupy the same box or a second box on the header row.
            header = "".join(
                t["compact"]
                for t in sorted(group, key=lambda t: t["x"])
                if abs(t["cy"] - anchor["cy"])
                <= max(10, anchor["bottom"] - anchor["y"]) / 2
            )
            bike = _one(
                re.findall(r"자전거번호[:：]?([0-9OoIl|]{4,7})", header), "자전거 번호"
            )
            bike = _number_text(bike, "bike", card)
            duration_labels = [t for t in group if t["compact"] == "이용시간"]
            if not duration_labels:
                raise ValueError("이용시간 항목이 필요합니다.")
            duration_label = duration_labels[0]
            distance_labels = [
                t
                for t in group
                if t["compact"] == "이용거리" and t["cy"] > duration_label["cy"]
            ]
            if not distance_labels:
                raise ValueError("이용거리 항목이 필요합니다.")
            distance_label = distance_labels[0]
            if (
                len(duration_labels) > 1
                and duration_labels[1]["cy"] < distance_label["cy"]
            ):
                raise ValueError("다른 기록의 이용시간 항목이 섞였습니다.")
            details = [t for t in group if t["cy"] < duration_label["cy"]]
            station_tokens = []
            for t in details:
                if t["x"] >= w * 0.55:
                    continue
                m = re.fullmatch(r"([0-9OoIl|]{3,5})(?:\s+[^\d].*)?", t["text"].strip())
                if m and t["cy"] > anchor["bottom"]:
                    station_tokens.append(
                        (t, str(int(_number_text(m[1], "station", card))))
                    )
            if len(station_tokens) != 2:
                raise ValueError(
                    "출발·도착 대여소 두 곳이 완전히 보이도록 잘라 주세요."
                )
            dates = [
                (
                    t,
                    re.fullmatch(
                        r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})",
                        _number_text(t["text"], "date", card),
                    ),
                )
                for t in details
                if t["x"] >= w * 0.5
            ]
            dates = [(t, m) for t, m in dates if m]
            times = [
                (
                    t,
                    re.fullmatch(
                        r"(\d{1,2})[:.]?(\d{2})", _number_text(t["text"], "time", card)
                    ),
                )
                for t in details
                if t["x"] >= w * 0.5
            ]
            times = [(t, m) for t, m in times if m]
            if len(dates) != 2 or len(times) != 2:
                raise ValueError("대여·반납 날짜와 시각 두 쌍이 필요합니다.")
            stamps = []
            observed_names = {}
            for i, (st, sid) in enumerate(station_tokens):
                d, dm = dates[i]
                t, tm = times[i]
                boundary = (
                    station_tokens[i + 1][0]["y"] if i == 0 else duration_label["y"]
                )
                same_row = abs(t["cy"] - d["cy"]) <= _row_tolerance(t, d)
                after_date = t["cy"] > d["cy"] + _row_tolerance(t, d) or (
                    same_row and t["x"] >= d["right"] - _row_tolerance(t, d)
                )
                if abs(d["cy"] - st["cy"]) > max(
                    st["bottom"] - st["y"], d["bottom"] - d["y"]
                ) * 1.5 or not (after_date and t["cy"] < boundary):
                    raise ValueError("대여소와 날짜·시각의 위치가 맞지 않습니다.")
                if not re.search(r"[:.]", t["compact"]):
                    card["warnings"].append(
                        "시각 구분기호가 인식되지 않아 날짜 아래 위치를 기준으로 해석했습니다. 시각을 확인해 주세요."
                    )
                stamps.append(
                    datetime(
                        *map(int, dm.groups()), *map(int, tm.groups()), tzinfo=KST
                    ).isoformat()
                )
                # Preserve the name from the same station region, including inline names.
                inline = re.fullmatch(
                    r"[0-9OoIl|]{3,5}\s+([^\d].*)", st["text"].strip()
                )
                name_parts = [
                    v["compact"]
                    for v in details
                    if v["x"] < w * 0.55
                    and st["bottom"] <= v["cy"] < boundary
                    and v is not st
                ]
                observed_names["origin" if i == 0 else "destination"] = (
                    inline[1].strip() if inline else "".join(name_parts)
                )
            durations = [
                re.fullmatch(
                    r"(\d+(?:\.\d+)?)분", _number_text(t["text"], "minutes", card)
                )
                for t in group
                if t["x"] >= w * 0.5
                and _at_or_below(t, duration_label)
                and t["cy"] < distance_label["cy"]
            ]
            minutes = float(_one([m[1] for m in durations if m], "이용시간"))
            next_labels = [
                t["cy"]
                for t in group
                if t["cy"] > distance_label["cy"]
                and (
                    t["compact"].startswith(("이용칼로리", "이용갈로리", "칼로리"))
                    or t["compact"] == "이용시간"
                )
            ]
            limit = min(next_labels) if next_labels else bottom
            distance_values = [
                t
                for t in group
                if t["x"] >= w * 0.5
                and _at_or_below(t, distance_label)
                and t["cy"] < limit
                and re.fullmatch(
                    r"\d+(?:\.\d+)?km", _number_text(t["text"], "distance", card), re.I
                )
            ]
            if distance_values:
                limit = min(limit, distance_values[0]["bottom"] + 1)
            if any(
                re.fullmatch(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", t["compact"])
                for t in group
                if duration_label["cy"] < t["cy"] < limit
            ):
                raise ValueError("거리 영역에 다른 이용 기록의 날짜가 섞였습니다.")
            distances = [
                re.fullmatch(
                    r"(\d+(?:\.\d+)?)km",
                    _number_text(t["text"], "distance", card),
                    re.I,
                )
                for t in group
                if t["x"] >= w * 0.5
                and _at_or_below(t, distance_label)
                and t["cy"] < limit
            ]
            km = float(_one([m[1] for m in distances if m], "이용거리"))
            card["bounds"][3] = limit
            record = dict(
                bicycle_number=bike,
                start_station_id=station_tokens[0][1],
                end_station_id=station_tokens[1][1],
                started_at=stamps[0],
                returned_at=stamps[1],
                actual_minutes=minutes,
                distance_km=km,
            )
            card["record"] = validate_record(
                record, stations, bikes, require_known_stations=False
            )
            card["observed_station_names"] = observed_names
            card["reviewable"] = True
            card["station_mapping"] = resolve_stations(
                card["record"], observed_names, stations
            )
            card["valid"] = card["station_mapping"]["can_register"]
            if not card["valid"]:
                card["errors"].extend(
                    v["message"]
                    for v in card["station_mapping"]["stations"].values()
                    if v["status"] != "matched"
                )
            if card["corrections"]:
                card["warnings"].append(
                    "숫자 영역의 영문 오인식을 보정했습니다. 원본과 보정값을 확인해 주세요."
                )
            if any(t["confidence"] < 0.8 for t in group):
                card["warnings"].append(
                    "인식 신뢰도가 낮은 글자가 있습니다. 원본과 대조해 주세요."
                )
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            card["errors"].append(str(exc))
        cards.append(card)
    return dict(
        cards=cards,
        eligible_count=sum(c["valid"] for c in cards),
        reviewable_count=sum(c["reviewable"] for c in cards),
        status="review_required" if any(c["reviewable"] for c in cards) else "rejected",
        message="등록할 한 건을 선택하고 원본과 번호·이름을 대조해 주세요."
        if any(c["reviewable"] for c in cards)
        else "완전한 이용내역을 찾지 못했습니다. 자전거 번호부터 이용거리까지 포함해 주세요.",
        authenticity_verified=False,
    )
