"""Display-only matching to actual OSM rail polylines, never routing times."""

import json
from pathlib import Path
import networkx as nx


class RailShapes:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.lines = {}
        self.cache = {}
        path = self.folder / "sources.json"
        self.files = (
            {
                r["line"]: r["file"]
                for r in json.loads(path.read_text(encoding="utf-8"))["files"]
            }
            if path.exists()
            else {}
        )

    def _line(self, line):
        from transit_geometry import metres

        if line in self.lines:
            return self.lines[line]
        if line not in self.files:
            return None
        data = json.loads((self.folder / self.files[line]).read_text(encoding="utf-8"))
        graph = nx.Graph()
        segments = []
        for feature in data.get("features", []):
            geometry = feature.get("geometry") or {}
            parts = (
                [geometry["coordinates"]]
                if geometry.get("type") == "LineString"
                else geometry.get("coordinates", [])
                if geometry.get("type") == "MultiLineString"
                else []
            )
            for part in parts:
                for a, b in zip(part, part[1:]):
                    a, b = tuple(a[:2]), tuple(b[:2])
                    if a == b:
                        continue
                    if not all(124 <= p[0] <= 132 and 33 <= p[1] <= 39 for p in (a, b)):
                        continue
                    graph.add_edge(a, b, weight=metres(a, b))
                    segments.append((a, b))
        components = {
            p: i
            for i, component in enumerate(nx.connected_components(graph))
            for p in component
        }
        self.lines[line] = (graph, segments, components)
        return self.lines[line]

    def match(self, line, start, end, max_snap=400):
        from transit_geometry import project, metres

        key = (line, tuple(start), tuple(end))
        if key in self.cache:
            return self.cache[key]
        item = self._line(line)
        if not item:
            return None
        graph, segments, components = item

        def near(target):
            candidates = []
            for a, b in segments:
                p = project([a, b], target)
                if p[0] <= max_snap:
                    candidates.append((p[0], a, b, p[3]))
            # Keep alternatives on disconnected parallel tracks, but never bridge
            # the gap between them with an artificial segment.
            counts = {}
            selected = []
            for candidate in sorted(candidates, key=lambda c: c[0]):
                component = components[candidate[1]]
                if counts.get(component, 0) >= 2:
                    continue
                counts[component] = counts.get(component, 0) + 1
                selected.append(candidate)
            return selected

        best = None
        for da, a, b, pa in near(start):
            for db, c, d, pb in near(end):
                if components[a] != components[c]:
                    continue
                candidates = []
                if {a, b} == {c, d}:
                    candidates.append([pa, pb])
                for u in (a, b):
                    for v in (c, d):
                        path = nx.shortest_path(graph, u, v, weight="weight")
                        candidates.append([pa] + [list(p) for p in path] + [pb])
                for points in candidates:
                    length = sum(metres(x, y) for x, y in zip(points, points[1:]))
                    if not 1 < length <= max(1800, metres(start, end) * 4):
                        continue
                    score = length + 5 * (da + db)
                    if best is None or score < best[0]:
                        best = (score, points)
        result = None
        if best:
            points = [p for i, p in enumerate(best[1]) if not i or p != best[1][i - 1]]
            result = {"type": "LineString", "coordinates": points}
        self.cache[key] = result
        return result
