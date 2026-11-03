"""Pedestrian graph boundaries, persisted costs, and authoritative overrides."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from hybrid_walking import WalkGraph, HybridWalking


@pytest.fixture
def graph_path(tmp_path):
    path = tmp_path / "walk.sqlite"
    c = sqlite3.connect(path)
    c.executescript("""
        create table nodes(id integer primary key,lon real,lat real);
        create table edges(u integer,v integer,meters real,seconds real,uncertain integer default 0,primary key(u,v));
        create table tiles(id text,bounds text);
    """)
    c.executemany(
        "insert into nodes values(?,?,?)",
        [(1, 127.0, 37.5), (2, 127.0, 37.501), (3, 127.0, 37.502)],
    )
    c.executemany(
        "insert into edges(u,v,meters,seconds) values(?,?,?,?)",
        [(1, 2, 111.0, 92.5), (2, 3, 111.0, 92.5)],
    )
    c.execute(
        "insert into tiles values('x',?)", (json.dumps([37.49, 126.99, 37.52, 127.02]),)
    )
    c.commit()
    c.close()
    return path


def point(lat):
    return dict(latitude=lat, longitude=127.0)


def test_direction_and_real_geometry_no_invented_reverse(graph_path):
    graph = WalkGraph(graph_path)
    edge = graph.route(point(37.5), point(37.502), "A", "B")
    assert edge["distance_m"] == pytest.approx(222)
    assert edge["minutes"] == pytest.approx(185 / 60)
    assert edge["geometry"]["type"] == "LineString"
    assert graph.route(point(37.502), point(37.5), "B", "A") is None


def test_uncovered_or_distant_snap_is_not_a_straight_line(graph_path):
    graph = WalkGraph(graph_path)
    assert graph.route(point(37.53), point(37.502), "A", "B") is None
    assert (
        graph.route(dict(latitude=37.5, longitude=127.005), point(37.502), "A", "B")
        is None
    )


def test_same_road_node_has_finite_access_without_sparse_index_errors(graph_path):
    graph = WalkGraph(graph_path)
    edge = graph.route(point(37.5), point(37.500001), 'A', 'B')
    assert edge is not None and edge['minutes'] > 0
    assert 0 < edge['distance_m'] < 1
    assert edge['major_road_fraction'] == 0
    assert not edge['needs_kakao_verification']


class Kakao:
    def __init__(self):
        self.edge = None
        self.calls = 0
        self.client = SimpleNamespace(calls=0, max_calls=30)

    def cached(self, *args):
        return (self.edge is not None, self.edge)

    def __call__(self, *args):
        self.calls += 1
        return self.edge

    def set_deadline(self, deadline):
        pass


def test_offline_cost_is_persistent_and_kakao_override_wins(graph_path, tmp_path):
    kakao = Kakao()
    cache = tmp_path / "new-environment" / "cache.sqlite"
    walking = HybridWalking(kakao, graph_path, cache)
    hit, edge = walking.cached(point(37.5), point(37.502), "A", "B")
    assert hit and edge["source"] == "osm_walk"
    assert kakao.calls == 0
    kakao.edge = dict(edge, minutes=8, source="kakao_walk")
    hit, corrected = walking.cached(point(37.5), point(37.502), "A", "B")
    assert hit and corrected["minutes"] == 8
    assert walking.counts["large_discrepancies"] == 1
    walking.close()
    con = sqlite3.connect(cache)
    assert con.execute("select count(*) from osm_walks").fetchone()[0] == 1
    assert con.execute("select kakao_minutes from walk_comparisons").fetchone()[0] == 8
    con.close()


def test_failed_offline_route_can_fall_back_without_fabrication(graph_path, tmp_path):
    kakao = Kakao()
    walking = HybridWalking(kakao, graph_path, tmp_path / "cache.sqlite")
    assert walking(point(37.502), point(37.5), "B", "A") is None
    assert kakao.calls == 1
    walking.close()


def test_priority_peek_never_computes_a_missing_route(graph_path, tmp_path, monkeypatch):
    walking = HybridWalking(Kakao(), graph_path, tmp_path / 'peek.sqlite')
    original = walking.graph.route
    calls = []

    def route(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(walking.graph, 'route', route)
    a, b = point(37.5), point(37.502)
    assert walking.peek(a, b, 'A', 'B') == (False, None)
    assert not calls
    hit, edge = walking.cached(a, b, 'A', 'B')
    assert hit and len(calls) == 1
    assert walking.peek(a, b, 'A', 'B') == (True, edge)
    assert len(calls) == 1
    walking.kakao.edge = dict(edge, minutes=8, source='kakao_walk')
    assert walking.peek(a, b, 'A', 'B')[1]['minutes'] == 8
    walking.close()


def test_arterial_road_shortcut_requires_kakao(graph_path, tmp_path):
    con = sqlite3.connect(graph_path)
    con.execute("update edges set uncertain=1")
    con.commit()
    con.close()
    kakao = Kakao()
    walking = HybridWalking(kakao, graph_path, tmp_path / "uncertain.sqlite")
    assert walking(point(37.5), point(37.502), "A", "B") is None
    assert kakao.calls == 1
    walking.close()


def test_new_policy_rechecks_an_already_saved_osm_value(graph_path, tmp_path):
    kakao = Kakao()
    cache = tmp_path / "quality.sqlite"
    walking = HybridWalking(kakao, graph_path, cache)
    a, b = point(37.5), point(37.502)
    value = walking._osm(a, b, "A", "B")
    value["geometry"]["coordinates"] = [
        [127.0 + (i % 2) * 0.0001, 37.5 + i * 0.0001] for i in range(10)
    ]
    with walking.db:
        walking.db.execute(
            "insert or replace into osm_walks values (?,?)",
            (walking._key(a, b), json.dumps(value)),
        )
    walking.close()
    (tmp_path / "verification_policy.json").write_text(
        json.dumps({"extra_rules": ["complex_walk"]}), encoding="utf-8"
    )
    updated = HybridWalking(kakao, graph_path, cache)
    assert updated(a, b, "A", "B") is None
    assert kakao.calls == 1
    updated.close()


def test_cached_builder_excludes_bus_lane_and_preserves_foot_direction(tmp_path):
    import gzip
    import hashlib
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "walk_builder_test",
        Path(__file__).resolve().parents[1] / "scripts/build_osm_walking.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = tmp_path / "assets"
    raw = folder / "tiles"
    raw.mkdir(parents=True)
    bounds = [37.51, 126.87, 37.63, 127.0]
    key = hashlib.sha256(json.dumps(bounds).encode()).hexdigest()[:16]
    elements = [
        dict(type="node", id=i, lon=126.93, lat=37.55 + i * 0.0001) for i in [1, 2, 3]
    ]
    elements.extend(
        [
            dict(
                type="way",
                id=10,
                nodes=[1, 2],
                tags={"highway": "footway", "oneway:foot": "yes"},
            ),
            dict(type="way", id=11, nodes=[2, 3], tags={"highway": "busway"}),
            dict(type="way", id=12, nodes=[1, 3], tags={"highway": "secondary"}),
        ]
    )
    with gzip.open(raw / (key + ".json.gz"), "wt", encoding="utf-8") as f:
        json.dump({"elements": elements}, f)
    module.build(folder, max_tiles=1, rebuild_cached=True)
    con = sqlite3.connect(folder / "seoul_walk.sqlite")
    rows = set(con.execute("select u,v,uncertain from edges"))
    con.close()
    assert rows == {(1, 2, 0), (1, 3, 1), (3, 1, 1)}
