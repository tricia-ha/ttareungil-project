from pathlib import Path
import gzip
import json
import sys
import tempfile
import io
import pickle
import unittest
from unittest.mock import patch, MagicMock

import networkx as nx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import road_startup_cache as cache
from project_service import ProjectService, RouteQuery


class RoadStartupTests(unittest.TestCase):
    def test_large_pickle_buffers_are_written_without_changing_values(self):
        array = np.arange(200000, dtype=np.float64).reshape(100000, 2)
        stream = io.BytesIO()
        writer = cache.BoundedWriter(stream)
        pickle.dump(array, writer, protocol=5)
        np.testing.assert_array_equal(pickle.loads(stream.getvalue()), array)
        self.assertEqual(writer.size, len(stream.getvalue()))

    def test_builder_identity_uses_loaded_code_and_ignores_checkout_path(self):
        original = cache.RouteEngine.__init__
        relocated = type(original)(
            original.__code__.replace(co_filename="another-checkout/route_runtime.py"),
            original.__globals__,
            original.__name__,
            original.__defaults__,
        )
        self.assertEqual(
            cache.function_identity(original), cache.function_identity(relocated)
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "roads.json.gz"
        self.folder = self.root / "runtime"
        self.graph = nx.MultiDiGraph()
        for i in (1, 2, 3):
            self.graph.add_node(
                i, y=37.5 + (i - 1) * 0.0005, x=127 + (i - 1) * 0.0005, elevation=10 + i
            )
        for a, b, length in [(1, 2, 100), (2, 3, 100), (1, 3, 250)]:
            self.graph.add_edge(a, b, key=0, length=length, highway="residential")
        self.save_source()

    def save_source(self):
        with gzip.open(self.source, "wt", encoding="utf-8") as f:
            json.dump({"graph": nx.node_link_data(self.graph, edges="edges")}, f)

    def load(self, signature="graph"):
        engine = cache.load_routes(self.source, self.folder, signature)
        self.addCleanup(engine.close)
        return engine

    def test_reuses_exact_graph_tree_and_computed_route(self):
        original = self.load()
        restored = self.load()
        self.assertFalse(original.startup_cache["hit"])
        self.assertTrue(restored.startup_cache["hit"])
        self.assertEqual(original.nodes, restored.nodes)
        self.assertEqual(
            list(original.graph.edges(keys=True, data=True)),
            list(restored.graph.edges(keys=True, data=True)),
        )
        self.assertEqual(
            list(original.directed.edges(data=True)),
            list(restored.directed.edges(data=True)),
        )
        self.assertEqual(
            original._snap(37.5001, 127.0001), restored._snap(37.5001, 127.0001)
        )
        expected = original.candidates(37.5, 127, 37.501, 127.001)
        # Force both engines to compute, instead of reading the same route row.
        original.database.execute("DELETE FROM routes")
        original.database.commit()
        self.assertEqual(expected, restored.candidates(37.5, 127, 37.501, 127.001))

    def test_source_signature_and_runtime_versions_invalidate_cache(self):
        self.load()
        self.assertFalse(self.load("new-signature").startup_cache["hit"])
        self.graph.nodes[2]["elevation"] = 20
        self.save_source()
        changed = self.load()
        self.assertFalse(changed.startup_cache["hit"])
        self.assertEqual(changed.graph.nodes[2]["elevation"], 20)
        with patch.object(cache.nx, "__version__", "different-version"):
            self.assertFalse(self.load().startup_cache["hit"])
        self.assertLessEqual(len(list(self.folder.glob("road-runtime-*.json"))), 2)

    def test_corruption_rebuilds_without_removing_route_database(self):
        engine = self.load()
        engine.database.execute("INSERT INTO routes VALUES('keep','[]')")
        engine.database.commit()
        data, meta = cache.paths(self.folder, cache.identity(self.source, "graph"))
        with data.open("ab") as f:
            f.write(b"broken")
        restored = self.load()
        self.assertFalse(restored.startup_cache["hit"])
        self.assertEqual(
            restored.database.execute(
                "SELECT payload FROM routes WHERE cache_key='keep'"
            ).fetchone(),
            ("[]",),
        )

    def test_invalid_metadata_is_not_deserialized(self):
        self.load()
        key = cache.identity(self.source, "graph")
        data, meta = cache.paths(self.folder, key)
        info = json.loads(meta.read_text())
        info["identity"]["signature"] = "foreign"
        meta.write_text(json.dumps(info))
        with patch.object(
            cache.pickle, "load", side_effect=AssertionError("must not load")
        ):
            self.assertIsNone(cache.read_state(self.folder, key))

    def test_optional_write_failure_does_not_break_service(self):
        with patch.object(cache, "write_state", return_value=False):
            engine = self.load()
        self.assertFalse(engine.startup_cache["written"])
        self.assertTrue(engine.candidates(37.5, 127, 37.501, 127.001))

    def test_size_limit_cleans_up_temporary_files(self):
        with patch.object(cache, "MAX_BYTES", 8):
            engine = self.load()
        self.assertFalse(engine.startup_cache["written"])
        self.assertFalse(list(self.folder.glob("*.tmp")))

    def test_prepared_wrapper_retrieval_does_not_wait_for_inference_lock(self):
        service = ProjectService.__new__(ProjectService)
        expected = object()
        service._guarded_model = expected
        service._model_lock = MagicMock()
        self.assertIs(service.shared_model(), expected)
        service._model_lock.__enter__.assert_not_called()

    def test_only_cold_model_reports_preparation_stage(self):
        service = ProjectService.__new__(ProjectService)
        model = object()
        service.shared_model = lambda: model
        service.db_path = self.root / "unused.sqlite"
        query = RouteQuery(
            action="route",
            origin=dict(latitude=37.5, longitude=127),
            destination=dict(latitude=37.51, longitude=127),
            departure="auto",
            use_kma=False,
            use_inventory=False,
        )
        with (
            patch.dict("os.environ", {"KAKAO_REST_API_KEY": "test-key"}),
            patch(
                "scheduled_runtime.run_scheduled_optimization", return_value={}
            ) as run,
        ):
            for ready in (False, True):
                service.eta_service = model if ready else None
                stages = []
                service.route(query, on_progress=stages.append)
                self.assertEqual(stages, [] if ready else ["model"])
                self.assertIs(run.call_args.kwargs["eta_service"], model)


if __name__ == "__main__":
    unittest.main()
