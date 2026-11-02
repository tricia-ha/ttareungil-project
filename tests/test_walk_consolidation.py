from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from walk_consolidation import consolidate_route


def walk(a, b, start, arrival, minutes, source="kakao_walk", **extra):
    return dict(
        mode="walk",
        **{"from": a, "to": b},
        start=start,
        arrival=arrival,
        minutes=minutes,
        source=source,
        **extra,
    )


def route(legs, total=10):
    return dict(
        legs=legs,
        arrival=legs[-1]["arrival"],
        total_minutes=total,
        score_minutes=total,
        transit_boardings=0,
    )


NODES = {
    key: {"latitude": 37.5 + i * 0.001, "longitude": 127.0}
    for i, key in enumerate(("origin", "A", "B", "destination"))
}


def test_ordinary_consecutive_walks_are_one_display_leg_without_new_call():
    value, stats = consolidate_route(
        route(
            [
                walk("origin", "A", "2026-11-02T10:00:00+09:00", "2026-11-02T10:03:00+09:00", 3),
                walk("A", "destination", "2026-11-02T10:03:00+09:00", "2026-11-02T10:10:00+09:00", 7),
            ]
        ),
        NODES,
    )
    assert len(value["legs"]) == 1
    assert value["legs"][0]["source"] == "combined_walk"
    assert value["legs"][0]["minutes"] == 10
    assert stats == {
        "merged_walk_legs": 1,
        "direct_replacements": 0,
        "new_direct_calls": 0,
    }


def test_published_platform_transfer_stays_separate():
    value, stats = consolidate_route(
        route(
            [
                walk("origin", "A", "2026-11-02T10:00:00+09:00", "2026-11-02T10:03:00+09:00", 3),
                walk(
                    "A",
                    "destination",
                    "2026-11-02T10:03:00+09:00",
                    "2026-11-02T10:10:00+09:00",
                    7,
                    source="published_rail_transfer_time",
                    platform_transfer=True,
                ),
            ]
        ),
        NODES,
    )
    assert len(value["legs"]) == 2
    assert stats["merged_walk_legs"] == 0


def test_cached_shorter_destination_walk_replaces_relay_and_updates_eta():
    class Walking:
        calls = 0

        def cached(self, a, b, start, end):
            return True, {
                "from": start,
                "to": end,
                "minutes": 6,
                "source": "kakao_walk",
            }

        def __call__(self, *args):
            self.calls += 1
            raise AssertionError("cache hit must not call API")

    walking = Walking()
    value, stats = consolidate_route(
        route(
            [
                walk("origin", "A", "2026-11-02T10:00:00+09:00", "2026-11-02T10:03:00+09:00", 3),
                walk("A", "destination", "2026-11-02T10:03:00+09:00", "2026-11-02T10:10:00+09:00", 7),
            ]
        ),
        NODES,
        walking,
        allow_new=True,
    )
    assert walking.calls == 0
    assert len(value["legs"]) == 1
    assert value["total_minutes"] == 6
    assert stats["direct_replacements"] == 1


def test_shorter_direct_walk_before_vehicle_preserves_evaluated_departure_time():
    class Walking:
        def cached(self, a, b, start, end):
            return True, {"from": start, "to": end, "minutes": 4, "source": "kakao_walk"}

    legs = [
        walk("origin", "A", "2026-11-02T10:00:00+09:00", "2026-11-02T10:03:00+09:00", 3),
        walk("A", "B", "2026-11-02T10:03:00+09:00", "2026-11-02T10:08:00+09:00", 5),
        {
            "mode": "bicycle",
            "from": "B",
            "to": "destination",
            "start": "2026-11-02T10:08:00+09:00",
            "arrival": "2026-11-02T10:10:00+09:00",
            "minutes": 2,
        },
    ]
    value, _ = consolidate_route(route(legs), NODES, Walking())
    assert [leg["mode"] for leg in value["legs"]] == ["walk", "wait", "bicycle"]
    assert value["legs"][1]["arrival"] == "2026-11-02T10:08:00+09:00"
    assert value["total_minutes"] == 10


def test_walk_roundtrip_becomes_wait_not_movement():
    value, _ = consolidate_route(
        route(
            [
                walk("origin", "A", "2026-11-02T10:00:00+09:00", "2026-11-02T10:03:00+09:00", 3),
                walk("A", "origin", "2026-11-02T10:03:00+09:00", "2026-11-02T10:10:00+09:00", 7),
            ]
        ),
        NODES,
    )
    assert len(value["legs"]) == 1
    assert value["legs"][0]["mode"] == "wait"
    assert value["total_minutes"] == 10
