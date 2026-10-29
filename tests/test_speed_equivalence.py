"""Equivalence checks: faster lookups and duplicate transit submissions only."""

from pathlib import Path
import ast
import json
import math
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import numpy as np
import pandas as pd
from history_lookup import IndexedHistoryTable
from multimodal import Network, Router, Config
from multimodal_cache import CachedBikeProvider
from prediction_identity import prediction_code_identity

START = "2026-10-25T10:00:00+09:00"


class HistoryEquivalenceTests(unittest.TestCase):
    def test_matches_merge_order_duplicates_missing_and_dtypes(self):
        table = pd.DataFrame(
            {
                "a": ["1", "2", "3"],
                "b": ["x", "y", "z"],
                "count": np.array([10, 20, 30], dtype=np.int64),
                "median": np.array([1.0, 2.0, 3.0], dtype=np.float32),
            }
        )
        for keys in (["a"], ["a", "b"]):
            lookup = IndexedHistoryTable(table, keys)
            query = pd.DataFrame(
                {
                    "a": ["3", "new", "1", "3"],
                    "b": ["z", "q", "x", "z"],
                    "value": [9, 8, 7, 6],
                },
                index=[50, 11, 10, 8],
            )
            if keys == ["a"]:
                query = query.drop(columns="b")
            expected = query.merge(table, on=keys, how="left", validate="many_to_one")
            pd.testing.assert_frame_equal(lookup.join(query), expected)
            pd.testing.assert_frame_equal(
                lookup.join(query.iloc[:0]),
                query.iloc[:0].merge(
                    table, on=keys, how="left", validate="many_to_one"
                ),
            )

    def test_nulls_overlaps_and_key_dtypes_use_exact_merge_semantics(self):
        for table, query in (
            (
                pd.DataFrame({"a": ["1", None], "v": [1, 2]}),
                pd.DataFrame({"a": [None, "1", "new"]}),
            ),
            (pd.DataFrame({"a": [1, 2], "v": [1, 2]}), pd.DataFrame({"a": [1.0, 3.0]})),
            (
                pd.DataFrame({"a": ["1", "2"], "v": [1, 2]}),
                pd.DataFrame({"a": ["2"], "v": [5]}),
            ),
        ):
            pd.testing.assert_frame_equal(
                IndexedHistoryTable(table, ["a"]).join(query),
                query.merge(table, on="a", how="left", validate="many_to_one"),
            )
        with self.assertRaises(pd.errors.MergeError):
            IndexedHistoryTable(pd.DataFrame({"a": ["1", "1"], "v": [1, 2]}), ["a"])

    def test_every_history_feature_matches_original_formula(self):
        import random

        rng = random.Random(7)
        od = pd.DataFrame(
            {
                "start_station_id": ["1", "2", "3"],
                "end_station_id": ["2", "3", "4"],
                "od_count": [12, 33, 0],
                "od_median_min": [7.0, 5.0, np.nan],
                "od_iqr_min": [2.0, 1.0, np.nan],
            }
        )
        hierarchy = {
            "origin": pd.DataFrame(
                {"start_station_id": ["1", "2"], "sum": [15.0, 5.0], "count": [10, 4]}
            ),
            "destination": pd.DataFrame(
                {"end_station_id": ["2", "3"], "sum": [20.0, 10.0], "count": [12, 5]}
            ),
            "district_pair": pd.DataFrame(
                {
                    "start_district": ["a", "b"],
                    "end_district": ["b", "c"],
                    "sum": [40.0, 12.0],
                    "count": [20, 6],
                }
            ),
        }
        scopes = {
            "origin": ["start_station_id"],
            "destination": ["end_station_id"],
            "district_pair": ["start_district", "end_district"],
        }
        source = ast.parse((ROOT / "src/eta_service.py").read_text(encoding="utf-8"))
        cls = next(
            n
            for n in source.body
            if isinstance(n, ast.ClassDef) and n.name == "BicycleETAService"
        )
        current = next(
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == "_add_history"
        )
        # Derive the old implementation's merge statements without importing a
        # neural model. Only the table access is changed; formulas are identical.
        old_source = (
            ast.unparse(current)
            .replace(
                "self._od_lookup.join(frame)",
                "frame.merge(self.od_full, on=OD_KEYS, how='left', validate='many_to_one')",
            )
            .replace(
                "self._hierarchy_lookups[name].join(result)",
                "result.merge(self.hierarchy[name].rename(columns={'sum':'total_sum','count':'total_count'}), on=keys, how='left', validate='many_to_one')",
            )
        )
        namespace = {
            "pd": pd,
            "np": np,
            "math": math,
            "OD_KEYS": ["start_station_id", "end_station_id"],
            "OD_SHRINKAGE_K": 20.0,
            "SCOPES": scopes,
        }
        exec(
            compile(
                ast.Module(body=[current], type_ignores=[]), "<indexed-history>", "exec"
            ),
            namespace,
        )
        fast = namespace["_add_history"]
        exec(compile(old_source, "<merge-history>", "exec"), namespace)
        old = namespace["_add_history"]
        obj = types.SimpleNamespace(
            od_full=od,
            hierarchy=hierarchy,
            stats_manifest={"global_train_median": 8.0},
            _od_lookup=IndexedHistoryTable(od, ["start_station_id", "end_station_id"]),
            _hierarchy_lookups={
                name: IndexedHistoryTable(
                    value.rename(columns={"sum": "total_sum", "count": "total_count"}),
                    scopes[name],
                )
                for name, value in hierarchy.items()
            },
        )
        for _ in range(30):
            query = pd.DataFrame(
                [
                    {
                        "start_station_id": str(rng.randint(1, 5)),
                        "end_station_id": str(rng.randint(2, 6)),
                        "start_district": rng.choice("abc"),
                        "end_district": rng.choice("bcd"),
                        "base_duration_min": rng.uniform(1, 25),
                    }
                    for _ in range(5)
                ]
            )
            pd.testing.assert_frame_equal(
                fast(obj, query), old(obj, query), check_exact=True
            )


class SearchEquivalenceTests(unittest.TestCase):
    def test_same_vehicle_continuations_and_alighting_remain_identical(self):
        dt = datetime.fromisoformat(START)

        def edge(a, b, dep, arr, trip):
            return dict(
                mode="subway",
                **{"from": a, "to": b},
                trip_id=trip,
                departure=(dt + timedelta(minutes=dep)).isoformat(),
                arrival=(dt + timedelta(minutes=arr)).isoformat(),
            )

        net = Network(
            {k: {} for k in ("O", "S", "B", "Z")},
            transit=[
                edge("O", "S", 1, 5, "T"),
                edge("S", "B", 6, 10, "T"),
                edge("B", "S", 11, 15, "T"),
                edge("S", "Z", 20, 25, "T"),
                edge("S", "Z", 16, 24, "U"),
            ],
        )
        outputs = [
            Router(
                net,
                None,
                Config(top_k=3, horizon_minutes=35, reuse_transit_suffixes=enabled),
            ).search("O", "Z", START)
            for enabled in (False, True)
        ]
        self.assertEqual(outputs[0]["routes"], outputs[1]["routes"])

    def test_later_lower_penalty_state_is_not_incorrectly_reused(self):
        # Arriving earlier must not suppress a later path with a lower penalty.
        start = datetime.fromisoformat(START)
        net = Network(
            {k: {} for k in ("O", "A", "B", "S", "Z")},
            walks=[
                {"from": "O", "to": "B", "minutes": 1},
                {"from": "B", "to": "S", "minutes": 3},
                {"from": "A", "to": "S", "minutes": 1},
            ],
            bikes=[{"from": "O", "to": "A"}],
            transit=[
                {
                    "from": "S",
                    "to": "Z",
                    "mode": "bus",
                    "trip_id": "T",
                    "departure": (start + timedelta(minutes=6)).isoformat(),
                    "arrival": (start + timedelta(minutes=10)).isoformat(),
                }
            ],
        )
        results = []
        for enabled in (False, True):
            r = Router(
                net,
                lambda e, t: {"minutes": 1, "p_empty": 1.0},
                Config(
                    top_k=1,
                    horizon_minutes=15,
                    risk_penalty_minutes=5,
                    reuse_transit_suffixes=enabled,
                ),
            ).search("O", "Z", START)
            results.append(r["routes"])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1][0]["score_minutes"], 10)

    def test_repeated_boarding_submissions_are_reused_not_routes_deleted(self):
        start = datetime.fromisoformat(START)
        net = Network(
            {k: {} for k in ("O", "Z")},
            transit=[
                {
                    "from": "O",
                    "to": "Z",
                    "mode": "bus",
                    "trip_id": f"T{i}",
                    "departure": (start + timedelta(minutes=10 + i)).isoformat(),
                    "arrival": (start + timedelta(minutes=15 + i)).isoformat(),
                }
                for i in range(8)
            ],
            bikes=[{"from": "O", "to": "Z"}],
        )
        outputs = []
        for enabled in (False, True):
            outputs.append(
                Router(
                    net,
                    lambda e, t: {"minutes": 50},
                    Config(top_k=3, horizon_minutes=30, reuse_transit_suffixes=enabled),
                ).search("O", "Z", START)
            )
        self.assertEqual(outputs[0]["routes"], outputs[1]["routes"])
        self.assertLess(
            outputs[1]["transit_edges_examined"], outputs[0]["transit_edges_examined"]
        )
        self.assertGreater(outputs[1]["reused_transit_expansions"], 0)

    def test_random_networks_match_all_original_top_three_routes(self):
        import random

        rng = random.Random(25)
        start = datetime.fromisoformat(START)
        for case in range(60):
            nodes = {k: {} for k in ("O", "A", "B", "C", "S", "Z")}
            net = Network(
                nodes,
                walks=[
                    {"from": "O", "to": "A", "minutes": rng.randint(1, 4)},
                    {"from": "A", "to": "S", "minutes": rng.randint(1, 6)},
                    {"from": "B", "to": "S", "minutes": rng.randint(1, 4)},
                    {"from": "C", "to": "S", "minutes": rng.randint(1, 4)},
                    {"from": "O", "to": "Z", "minutes": 35},
                ],
                bikes=[{"from": "O", "to": "B"}, {"from": "A", "to": "C"}],
            )
            times = {(e["from"], e["to"]): rng.randint(1, 10) for e in net.bikes}
            for i in range(5):
                dep = rng.randint(8, 23)
                net.transit.append(
                    {
                        "from": "S",
                        "to": "Z",
                        "mode": "bus" if i % 2 else "subway",
                        "trip_id": f"T{i}",
                        "boarding_seconds": rng.choice([0, 15, 120]),
                        "departure": (start + timedelta(minutes=dep)).isoformat(),
                        "arrival": (
                            start + timedelta(minutes=dep + rng.randint(2, 8))
                        ).isoformat(),
                    }
                )
            outputs = []
            for enabled in (False, True):

                def predict(e, t):
                    minute = datetime.fromisoformat(t).minute
                    return {
                        "minutes": times[e["from"], e["to"]]
                        * (0.5 if minute % 3 == 0 else 1.0),
                        "p_empty": 0.0,
                    }

                outputs.append(
                    Router(
                        net,
                        predict,
                        Config(
                            top_k=3, horizon_minutes=40, reuse_transit_suffixes=enabled
                        ),
                    ).search("O", "Z", START)
                )
            with self.subTest(case=case):
                self.assertEqual(outputs[0]["routes"], outputs[1]["routes"])
                self.assertTrue(outputs[0]["search_complete"])
                self.assertTrue(outputs[1]["search_complete"])


class PredictionCacheTests(unittest.TestCase):
    def test_verified_build_reuses_existing_prediction_context(self):
        from scheduled_runtime import runtime_code_identity

        legacy = "ec2495ab3da889e9648a6352eef47f8429acdf18fba636d3e4f30db186c814c1"

        class Provider:
            def __init__(self):
                self.calls = 0

            def __call__(self, edge, departure):
                self.calls += 1
                return {"minutes": 5.0}

        edge = {"from": "A", "to": "B"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pred.sqlite"
            original = CachedBikeProvider(
                Provider(),
                path,
                {"version": 2, "code": legacy, "weather": "same-issue"},
            )
            original(edge, START)
            original.close()
            provider = Provider()
            updated = CachedBikeProvider(
                provider,
                path,
                {
                    "version": 2,
                    "code": runtime_code_identity(),
                    "weather": "same-issue",
                },
            )
            try:
                self.assertEqual(updated(edge, START)["minutes"], 5.0)
                self.assertEqual(provider.calls, 0)
                self.assertEqual(updated.hits, 1)
            finally:
                updated.close()

    def test_datetime_bucket_keeps_timezone_representation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = CachedBikeProvider(None, Path(tmp) / "pred.sqlite", {})
            try:
                local = datetime.fromisoformat(START)
                utc = local.astimezone(timezone.utc)
                self.assertTrue(cache._bucket(local).endswith("+09:00"))
                self.assertTrue(cache._bucket(utc).endswith("+00:00"))
            finally:
                cache.close()

    def test_zero_prefetch_budget_has_no_lookup_or_prediction(self):
        class Provider:
            def predict_eta_batch(self, *args):
                raise AssertionError("unexpected inference")

        with tempfile.TemporaryDirectory() as tmp:
            cache = CachedBikeProvider(Provider(), Path(tmp) / "pred.sqlite", {})
            try:
                with patch.object(
                    cache,
                    "_lookup",
                    side_effect=AssertionError("unexpected SQL lookup"),
                ):
                    self.assertEqual(
                        cache.prefetch([{"from": "A", "to": "B"}], START, 0), 0
                    )
            finally:
                cache.close()

    def test_search_code_edits_do_not_change_prediction_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            # Test-only fixture writes, never any real model artifact.
            for name in (
                "eta_service.py",
                "route_runtime.py",
                "kma_weather_forecast_provider.py",
                "history_lookup.py",
            ):
                (folder / name).write_text("value=1\n", encoding="utf-8")
            (folder / "multimodal.py").write_text(
                "class Router: pass\nclass V521BikeProvider:\n value=1\n",
                encoding="utf-8",
            )
            before = prediction_code_identity(folder)
            (folder / "multimodal.py").write_text(
                "class Router:\n changed=2\nclass V521BikeProvider:\n value=1\n",
                encoding="utf-8",
            )
            self.assertEqual(before, prediction_code_identity(folder))
            (folder / "history_lookup.py").write_text("value=2\n", encoding="utf-8")
            self.assertNotEqual(before, prediction_code_identity(folder))


if __name__ == "__main__":
    unittest.main()
