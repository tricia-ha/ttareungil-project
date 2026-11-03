"""Remember resolved endpoints, but restore only fresh exact-coordinate API data."""

import json
import sqlite3
from pathlib import Path
from kakao_routes import walking_edge, distance


def coords(point):
    return [float(point["latitude"]), float(point["longitude"])]


class WalkingLinkCache:
    def __init__(self, path, reference=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS links (a TEXT,b TEXT,xy TEXT,PRIMARY KEY(a,b,xy))"
        )
        self.pending = set()
        self.reference = reference
        self.shared_cache = Path(path).parent
        self._shared = None

    @property
    def shared(self):
        if self._shared is None:
            from portable_transfers import SharedTransfers
            self._shared = SharedTransfers(self.reference, self.shared_cache)
        return self._shared

    def shared_edge(self, a, b, start, end):
        from portable_transfers import public_node
        if not public_node(a) or not public_node(b):
            return None
        return self.shared.get('kakao', start, end, a, b)

    def remember(self, a, b, start, end):
        xy = json.dumps([coords(start), coords(end)])
        self.pending.add((a, b, xy))

    def remember_result(self, path):
        path = Path(path)
        if not path.is_file():
            return
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            nodes = result.get("nodes", {})
            for route in result.get("routes", []) + result.get("mixed_routes", []):
                for leg in route.get("legs", []):
                    a, b = leg.get("from"), leg.get("to")
                    if leg.get("mode") == "walk" and a in nodes and b in nodes:
                        self.remember(a, b, nodes[a], nodes[b])
        except (ValueError, KeyError, TypeError, OSError):
            return

    def restore(self, network, client):
        radius = network.walk_policy.get("max_distance_m")
        if radius is None:
            return 0
        existing = {(e["from"], e["to"]) for e in network.walks}
        count = 0
        pairs = list(
            self.db.execute("SELECT a,b,xy FROM links ORDER BY a,b,xy")
        ) + sorted(self.pending)
        from portable_transfers import public_node
        if any(public_node(k) for k in network.nodes):
            pairs += list(self.shared.kakao_pairs())
        for a, b, xy in pairs:
            if (a, b) in existing or a not in network.nodes or b not in network.nodes:
                continue
            if a == b or a == "destination" or b == "origin":
                continue
            start, end = network.nodes[a], network.nodes[b]
            if start.get("kind") == end.get("kind") == "bike":
                continue
            if (
                json.loads(xy) != [coords(start), coords(end)]
                or distance(start, end) > radius
            ):
                continue
            cached = client.get_cached("walk", start, end)
            edge = walking_edge(*cached, a, b) if cached is not None else self.shared_edge(a, b, start, end)
            if edge is not None:
                network.walks.append(edge)
                existing.add((a, b))
                count += 1
        return count

    def close(self):
        try:
            with self.db:
                self.db.executemany(
                    "INSERT OR IGNORE INTO links VALUES (?,?,?)", self.pending
                )
        finally:
            self.db.close()
            if self._shared is not None:
                self._shared.close()
