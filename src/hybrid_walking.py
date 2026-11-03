"""Persistent pedestrian graph, with Kakao overrides and bounded fallback."""

from __future__ import annotations
from collections import OrderedDict
from pathlib import Path
import hashlib
import json
import math
import sqlite3
import threading
import time

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from kakao_routes import CallLimit, distance

_LOCAL = threading.RLock()
_GRAPHS = {}


class WalkGraph:
    def __init__(self, path):
        self.path = Path(path)
        con = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        try:
            con.execute("begin")
            nodes = con.execute("select id,lon,lat from nodes order by id").fetchall()
            uncertainty = (
                "uncertain"
                if "uncertain"
                in {r[1] for r in con.execute("pragma table_info(edges)")}
                else "1"
            )
            edges = con.execute(
                f"select u,v,meters,seconds,{uncertainty} from edges order by u,v"
            ).fetchall()
            self.tiles = [
                json.loads(r[0]) for r in con.execute("select bounds from tiles")
            ]
        finally:
            con.close()
        # Bus-only/forbidden roads are removed by the builder. Arterial
        # centreline approximations remain identifiable for verification.
        connected = {r[0] for r in edges} | {r[1] for r in edges}
        nodes = [r for r in nodes if r[0] in connected]
        if not nodes or not edges:
            raise ValueError("Empty pedestrian graph")
        semantic = hashlib.sha256(b'pedestrian-transfer-graph-v1')
        semantic.update(np.asarray(nodes, dtype='<f8').tobytes())
        semantic.update(np.asarray(edges, dtype='<f8').tobytes())
        semantic.update(json.dumps(sorted(self.tiles)).encode())
        self.content_signature = semantic.hexdigest()
        ids = {r[0]: i for i, r in enumerate(nodes)}
        self.xy = np.array([(r[1], r[2]) for r in nodes], dtype=float)
        self.scale = np.array([111320 * math.cos(math.radians(37.57)), 111320])
        self.tree = cKDTree(self.xy * self.scale)
        u = np.array([ids[r[0]] for r in edges])
        v = np.array([ids[r[1]] for r in edges])
        self.matrix = csr_matrix(
            ([r[3] for r in edges], (u, v)), shape=(len(nodes), len(nodes))
        )
        self.meters = csr_matrix(
            ([r[2] for r in edges], (u, v)), shape=self.matrix.shape
        )
        self.sources = OrderedDict()
        flagged = np.array([bool(r[4]) for r in edges])
        self.uncertain = csr_matrix(
            (np.ones(int(flagged.sum()), dtype=np.uint8), (u[flagged], v[flagged])),
            shape=self.matrix.shape,
        )
        self.lock = threading.RLock()
        self.signature = hashlib.sha256(
            json.dumps(
                [
                    "pedestrian_v3_verify_all_arterials",
                    sorted(self.tiles),
                    len(nodes),
                    len(edges),
                    self.path.stat().st_mtime_ns,
                ]
            ).encode()
        ).hexdigest()[:20]

    def covered(self, a, b):
        # Require a completed tile around both endpoints plus a local margin.
        # Uncovered pairs fall back to Kakao, not invented straight lines.
        margin = 0.002
        return any(
            south + margin <= min(a["latitude"], b["latitude"])
            and max(a["latitude"], b["latitude"]) <= north - margin
            and west + margin <= min(a["longitude"], b["longitude"])
            and max(a["longitude"], b["longitude"]) <= east - margin
            for south, west, north, east in self.tiles
        )

    def route(self, a, b, start, end):
        if not self.covered(a, b):
            return None
        ds, source = self.tree.query(
            np.array([a["longitude"], a["latitude"]]) * self.scale
        )
        de, target = self.tree.query(
            np.array([b["longitude"], b["latitude"]]) * self.scale
        )
        if max(ds, de) > 35:
            return None
        source, target = int(source), int(target)
        with self.lock:
            if source not in self.sources:
                dist, pred = dijkstra(
                    self.matrix,
                    directed=True,
                    indices=source,
                    limit=3600,
                    return_predecessors=True,
                )
                self.sources[source] = (dist, pred)
                if len(self.sources) > 16:
                    self.sources.popitem(last=False)
            self.sources.move_to_end(source)
            dist, pred = self.sources[source]
            if not math.isfinite(dist[target]):
                return None
            path = [target]
            while path[-1] != source:
                parent = int(pred[path[-1]])
                if parent < 0:
                    return None
                path.append(parent)
            path.reverse()
            # Batch sparse lookups, but retain Python summation order so path
            # distances and verification decisions match the scalar version.
            if len(path) > 1:
                starts = np.asarray(path[:-1], dtype=np.intp)
                ends = np.asarray(path[1:], dtype=np.intp)
                costs = np.asarray(self.meters[starts, ends]).reshape(-1).tolist()
                flags = np.asarray(self.uncertain[starts, ends]).reshape(-1).astype(bool).tolist()
            else:
                costs, flags = [], []
            length = float(ds + de) + sum(costs)
            straight = distance(a, b)
            major_meters = sum(value for value, flag in zip(costs, flags) if flag)
            # Large detours and large snaps require authoritative verification.
            if length > max(600, straight * 3) or max(ds, de) > 25:
                return None
            return dict(
                mode="walk",
                **{"from": start, "to": end},
                minutes=max(0.05, (float(dist[target]) + (ds + de) / 1.2) / 60),
                distance_m=length,
                source="osm_walk",
                geometry={
                    "type": "LineString",
                    "coordinates": [[a["longitude"], a["latitude"]]]
                    + self.xy[path].tolist()
                    + [[b["longitude"], b["latitude"]]],
                },
                snap_distance_m=[float(ds), float(de)],
                graph_version=self.signature,
                major_road_fraction=major_meters / max(1, length),
                needs_kakao_verification=(
                    any(flags)
                ),
            )


def get_graph(path):
    path = Path(path).resolve()
    if not path.is_file():
        return None
    wal = Path(str(path) + "-wal")
    version = (
        str(path),
        path.stat().st_mtime_ns,
        wal.stat().st_mtime_ns if wal.exists() else 0,
    )
    with _LOCAL:
        if version not in _GRAPHS:
            _GRAPHS.clear()
            try:
                _GRAPHS[version] = WalkGraph(path)
            except (ValueError, sqlite3.DatabaseError):
                return None
        return _GRAPHS[version]


class HybridWalking:
    """Fresh Kakao cache first; offline walking second; bounded API fallback."""

    def __init__(self, kakao, graph_path, cache_path):
        self.kakao = kakao
        self.graph = get_graph(graph_path)
        self.client = kakao.client
        self.cancel_check = None
        policy = Path(graph_path).parent / "verification_policy.json"
        if not policy.is_file() and Path(graph_path).parent.name == 'shared_walking':
            from project_paths import PROJECT_ROOT
            policy = PROJECT_ROOT / 'data/walking/verification_policy.json'
        self.extra_rules = (
            json.loads(policy.read_text(encoding="utf-8")).get("extra_rules", [])
            if policy.is_file()
            else []
        )
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cache_path, timeout=30)
        self.db.execute("pragma journal_mode=WAL")
        self.db.execute("pragma synchronous=NORMAL")
        self.db.execute(
            "create table if not exists osm_walks (key text primary key,payload text)"
        )
        self.db.execute(
            "create table if not exists walk_comparisons (key text primary key, osm_minutes real,kakao_minutes real,ratio real,checked_at real)"
        )
        self.counts = dict(
            osm_hits=0,
            kakao_overrides=0,
            kakao_fallbacks=0,
            large_discrepancies=0,
        )
        self.compared = set()

    def set_deadline(self, deadline):
        self.kakao.set_deadline(deadline)

    def _key(self, a, b):
        return hashlib.sha256(
            json.dumps(
                [
                    self.graph.signature,
                    a["latitude"],
                    a["longitude"],
                    b["latitude"],
                    b["longitude"],
                ]
            ).encode()
        ).hexdigest()

    def _osm(self, a, b, start, end, *, compute=True):
        if self.graph is None:
            return None
        key = self._key(a, b)
        row = self.db.execute(
            "select payload from osm_walks where key=?", (key,)
        ).fetchone()
        if row:
            value = json.loads(row[0])
            value.update({"from": start, "to": end})
            return value
        links = getattr(self.kakao, 'link_cache', None)
        if links is not None:
            shared = links.shared.get('osm', a, b, start, end,
                                      graph_identity=self.graph.content_signature)
            if shared is not None:
                shared['graph_version'] = self.graph.signature
                return shared
        if not compute:
            return None
        value = self.graph.route(a, b, start, end)
        if value:
            with self.db:
                self.db.execute(
                    "insert or replace into osm_walks values (?,?)",
                    (key, json.dumps(value)),
                )
        return value

    def _compare(self, a, b, osm, kakao):
        if not osm or not kakao:
            return
        key = self._key(a, b)
        if key in self.compared:
            return
        self.compared.add(key)
        ratio = float(kakao["minutes"]) / max(0.05, float(osm["minutes"]))
        gap = abs(float(kakao["minutes"]) - float(osm["minutes"]))
        if gap >= 2 and (ratio >= 1.5 or ratio <= 1 / 1.5):
            self.counts["large_discrepancies"] += 1
        with self.db:
            self.db.execute(
                "insert or replace into walk_comparisons values (?,?,?,?,?)",
                (key, osm["minutes"], kakao["minutes"], ratio, time.time()),
            )

    def peek(self, a, b, start, end):
        """Cheap priority lookup: cached data only, no new graph search/HTTP."""
        hit, authoritative = self.kakao.cached(a, b, start, end)
        if hit:
            # Comparisons/diagnostic writes belong to an actual resolution,
            # not to the hundreds of optional priority probes around a stop.
            self.counts['kakao_overrides'] += 1
            return True, authoritative
        offline = self._osm(a, b, start, end, compute=False)
        if offline:
            from walking_quality import reasons
            if not reasons(a, b, start, end, offline, self.extra_rules):
                self.counts['osm_hits'] += 1
                return True, offline
        return False, None

    def cached(self, a, b, start, end):
        return self._cached(a, b, start, end, compute_osm=True)

    def _cached(self, a, b, start, end, *, compute_osm):
        hit, authoritative = self.kakao.cached(a, b, start, end)
        if hit:
            self.counts["kakao_overrides"] += 1
            if self.graph is not None:
                row = self.db.execute(
                    "select payload from osm_walks where key=?", (self._key(a, b),)
                ).fetchone()
                if row:
                    self._compare(a, b, json.loads(row[0]), authoritative)
            return True, authoritative
        offline = self._osm(a, b, start, end, compute=compute_osm)
        if offline:
            from walking_quality import reasons

            checks = reasons(a, b, start, end, offline, self.extra_rules)
            if not checks:
                self.counts["osm_hits"] += 1
                return True, offline
        return False, None

    def __call__(self, a, b, start, end):
        if self.cancel_check and self.cancel_check():
            raise CallLimit("경로 탐색을 중단했습니다.")
        hit, edge = self.cached(a, b, start, end)
        if hit:
            return edge
        self.counts["kakao_fallbacks"] += 1
        return self.kakao(a, b, start, end)

    def close(self):
        count = self.db.execute("select count(*) from osm_walks").fetchone()[0]
        if count > 50000:
            with self.db:
                self.db.execute(
                    "delete from osm_walks where rowid in (select rowid from osm_walks order by rowid limit ?)",
                    (count - 50000,),
                )
        self.db.close()
