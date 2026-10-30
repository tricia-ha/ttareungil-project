"""Prepare editable partial cards without inventing missing observations."""

import re
from screenshot_history import _tokens, _number_text


def prepare_manual_review(result, tokens, size):
    ts = _tokens(tokens)
    w, h = size
    cards = result["cards"]
    if not cards:
        # A failed bike-header OCR can still leave an identifiable single rental.
        labels = [t["compact"] for t in ts]
        evidence = (
            any(re.fullmatch(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}", v) for v in labels)
            or sum(
                bool(re.fullmatch(r"\d{3,5}", t["compact"]))
                for t in ts
                if t["x"] < w * 0.5
            )
            >= 2
        )
        if labels.count("이용시간") == 1 and labels.count("이용거리") == 1 and evidence:
            cards.append(
                dict(
                    index=0,
                    bounds=[0, 0, w, h],
                    valid=False,
                    reviewable=False,
                    record=None,
                    errors=[
                        "자전거 번호 제목을 읽지 못했습니다. 원본을 보고 직접 입력해 주세요."
                    ],
                    warnings=[],
                    corrections=[],
                )
            )
    for card in cards:
        left, top, right, bottom = card["bounds"]
        group = [t for t in ts if top <= t["cy"] < bottom]
        labels = {t["compact"] for t in group}
        card["manual_editable"] = bool(
            card.get("reviewable") or {"이용시간", "이용거리"} & labels
        )
        if not card["manual_editable"] or card.get("record"):
            continue
        partial = {}
        header = [t for t in group if "자전거번호" in t["compact"]]
        if len(header) == 1:
            found = re.findall(
                r"자전거번호[:：]?([0-9OoIl|]{4,7})", header[0]["compact"]
            )
            if len(found) == 1:
                partial["bicycle_number"] = _number_text(found[0], "bike", card)
        for key, kind, pattern in [
            ("actual_minutes", "minutes", r"(\d+(?:\.\d+)?)분"),
            ("distance_km", "distance", r"(\d+(?:\.\d+)?)km"),
        ]:
            values = [
                re.fullmatch(pattern, _number_text(t["text"], kind, card), re.I)
                for t in group
                if t["x"] >= w * 0.5
            ]
            values = [float(m[1]) for m in values if m]
            if len(values) == 1:
                partial[key] = values[0]
        card["partial_record"] = partial
    result["manual_reviewable_count"] = sum(bool(c["manual_editable"]) for c in cards)
    return result
