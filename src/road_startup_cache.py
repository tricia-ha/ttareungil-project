"""Reuse locally generated road objects; never distribute runtime pickle files."""

from pathlib import Path
import hashlib
import json
import marshal
import os
import pickle
import sqlite3
import sys
import tempfile
import time
import types

import networkx as nx
import numpy as np
import sklearn
from route_runtime import RouteEngine, _tags

FORMAT = 1
MAX_BYTES = 768 * 1024 * 1024
STATE_KEYS = ("graph", "nodes", "tree", "max_snap_m", "directed")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def function_identity(function):
    def normalize(code):
        return code.replace(
            co_filename="",
            co_firstlineno=0,
            co_consts=tuple(
                normalize(v) if isinstance(v, types.CodeType) else v
                for v in code.co_consts
            ),
        )

    return hashlib.sha256(
        marshal.dumps(
            (
                normalize(function.__code__),
                function.__defaults__,
                function.__kwdefaults__,
            )
        )
    ).hexdigest()


def identity(source, signature):
    return dict(
        format=FORMAT,
        source=digest(source),
        signature=signature,
        builder=dict(
            init=function_identity(RouteEngine.__init__),
            tags=function_identity(
                RouteEngine.__init__.__globals__.get("_tags", _tags)
            ),
        ),
        loader=digest(__file__),
        python=sys.version.split()[0],
        networkx=nx.__version__,
        numpy=np.__version__,
        sklearn=sklearn.__version__,
    )


def paths(folder, key):
    tag = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:24]
    base = Path(folder) / ("road-runtime-" + tag)
    return base.with_suffix(".pickle"), base.with_suffix(".json")


def read_state(folder, key):
    data, meta = paths(folder, key)
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        size = data.stat().st_size
        if (
            info["identity"] != key
            or not 0 < size <= MAX_BYTES
            or info["bytes"] != size
        ):
            return None
        if digest(data) != info["sha256"]:
            return None
        # Only a private, locally built cache is read. Web input cannot select it.
        with data.open("rb") as stream:
            state = pickle.load(stream)
        if set(state) != set(STATE_KEYS):
            return None
        if not isinstance(state["graph"], nx.MultiDiGraph) or not isinstance(
            state["directed"], nx.DiGraph
        ):
            return None
        if len(state["nodes"]) != len(state["graph"]):
            return None
        return state
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        EOFError,
        pickle.UnpicklingError,
        ImportError,
        AttributeError,
    ):
        return None


class BoundedWriter:
    def __init__(self, stream):
        self.stream, self.size = stream, 0

    def write(self, value):
        self.size += memoryview(value).nbytes
        if self.size > MAX_BYTES:
            raise OSError("Road startup cache exceeds its size limit")
        return self.stream.write(value)


def write_state(folder, key, engine):
    data, meta = paths(folder, key)
    temporary = []
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        for suffix in (".pickle.tmp", ".json.tmp"):
            handle, name = tempfile.mkstemp(
                prefix="road-runtime-", suffix=suffix, dir=folder
            )
            os.close(handle)
            temporary.append(Path(name))
        with temporary[0].open("wb") as stream:
            pickle.dump(
                {name: getattr(engine, name) for name in STATE_KEYS},
                BoundedWriter(stream),
                protocol=5,
            )
        info = dict(
            identity=key, bytes=temporary[0].stat().st_size, sha256=digest(temporary[0])
        )
        temporary[1].write_text(json.dumps(info), encoding="utf-8")
        os.replace(temporary[0], data)
        os.replace(temporary[1], meta)
        # Keep two locally generated versions for updates/rollback; no DBs are removed.
        versions = sorted(
            Path(folder).glob("road-runtime-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in versions[2:]:
            try:
                previous = json.loads(old.read_text(encoding="utf-8"))
                if previous.get("identity", {}).get("format") == FORMAT:
                    old.with_suffix(".pickle").unlink(missing_ok=True)
                    old.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        return True
    except (OSError, pickle.PicklingError):
        return False
    finally:
        for path in temporary:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def load_routes(source, folder, signature):
    import gzip

    begun = time.perf_counter()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    key = identity(source, signature)
    state = read_state(folder, key)
    if state is not None:
        engine = RouteEngine.__new__(RouteEngine)
        engine.__dict__.update(state)
        engine.signature = signature
        engine.database = sqlite3.connect(
            folder / "routes_v2.sqlite", check_same_thread=False
        )
        engine.database.execute(
            "CREATE TABLE IF NOT EXISTS routes(cache_key TEXT PRIMARY KEY,payload TEXT)"
        )
        engine.database.commit()
        hit, written = True, False
    else:
        with gzip.open(source, "rt", encoding="utf-8") as stream:
            saved = json.load(stream)
        graph = nx.node_link_graph(saved["graph"], edges="edges")
        del saved
        engine = RouteEngine(graph, folder / "routes_v2.sqlite", signature=signature)
        hit, written = False, write_state(folder, key, engine)
    engine.startup_cache = dict(
        hit=hit, written=written, seconds=time.perf_counter() - begun
    )
    print("OSM road preparation: " + json.dumps(engine.startup_cache), flush=True)
    return engine
