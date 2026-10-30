"""Load only routing code for tests; no torch, checkpoint or predictor imports."""

from pathlib import Path
import ast
import hashlib, json, math, sqlite3
import numpy as np
import networkx as nx


class NumpyTree:
    def __init__(self, coordinates, metric):
        self.coordinates = np.asarray(coordinates)

    def query(self, queries, k=1):
        results = []
        indexes = []
        for q in queries:
            delta = self.coordinates - q
            hav = (
                np.sin(delta[:, 0] / 2) ** 2
                + np.cos(q[0])
                * np.cos(self.coordinates[:, 0])
                * np.sin(delta[:, 1] / 2) ** 2
            )
            distances = 2 * np.arcsin(np.sqrt(np.clip(hav, 0, 1)))
            order = np.argsort(distances)[:k]
            results.append(distances[order])
            indexes.append(order)
        return np.array(results), np.array(indexes)


def route_engine_class():
    path = Path(__file__).resolve().parents[1] / "src" / "route_runtime.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    keep = {"fingerprint", "_tags", "_route_bearing", "RouteEngine"}
    selected = ast.Module(
        body=[
            node
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in keep
        ],
        type_ignores=[],
    )
    context = dict(
        Path=Path,
        np=np,
        nx=nx,
        math=math,
        sqlite3=sqlite3,
        json=json,
        hashlib=hashlib,
        BallTree=NumpyTree,
    )
    exec(compile(selected, str(path), "exec"), context)
    return context["RouteEngine"]
