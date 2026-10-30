"""Mock-only regressions for installation, mixed routes and feature semantics."""

from pathlib import Path
import sys, unittest, ast, types
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from multimodal import Network, Router, Config
from scheduled_runtime import (
    ScheduledConfig,
    build_scheduled_network,
    forecast_issue_key,
)
from candidate_edges import LazyConnections
from kakao_routes import CallLimit

START = "2026-10-20T10:00:00+09:00"


class ReviewRegressions(unittest.TestCase):
    def test_dependency_check_reaches_success_without_loading_models(self):
        import check_project
        from packaging.requirements import Requirement

        versions = {}
        for line in (ROOT / "requirements.txt").read_text().splitlines():
            if not line.strip():
                continue
            req = Requirement(line)
            versions[req.name] = next(
                s.version for s in req.specifier if s.operator in (">=", "==")
            )
        names = (
            "numpy",
            "pandas",
            "torch",
            "torchvision",
            "sklearn",
            "pyarrow",
            "pydantic",
            "networkx",
            "holidays",
            "openpyxl",
            "bs4",
        )
        with (
            patch("importlib.metadata.version", side_effect=lambda n: versions[n]),
            patch.dict(sys.modules, {n: types.ModuleType(n) for n in names}),
        ):
            check_project.check(True)

    def test_catch_bus_using_real_arrival_not_rounded_minute(self):
        n = Network(
            {x: {} for x in ("O", "A", "Z")},
            walks=[{"from": "O", "to": "A", "minutes": 1 / 3}],
            transit=[
                {
                    "from": "A",
                    "to": "Z",
                    "mode": "bus",
                    "trip_id": "B",
                    "boarding_seconds": 15,
                    "departure": "2026-10-20T10:00:40+09:00",
                    "arrival": "2026-10-20T10:10:40+09:00",
                }
            ],
        )
        for step in (60, 1):
            r = Router(n, None, Config(step_seconds=step, top_k=1)).search(
                "O", "Z", START
            )
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["routes"][0]["legs"][0]["grid_buffer_seconds"], 0)

    def test_missing_connectors_make_result_partial(self):
        n = Network(
            {"O": {}, "Z": {}},
            walks=[{"from": "O", "to": "Z", "minutes": 1}],
            metadata={"connector_limit_reached": True},
        )
        r = Router(n, None, Config(top_k=1)).search("O", "Z", START)
        self.assertEqual(r["status"], "partial")
        self.assertFalse(r["search_complete"])

    def make_network(self, count=20):
        stations = [
            {"station_id": str(i), "latitude": 37.5 + i * 0.0001, "longitude": 127.0}
            for i in range(count - 1)
        ]
        stations.append(
            {"station_id": str(count - 1), "latitude": 37.55, "longitude": 127.0}
        )
        n = build_scheduled_network(
            Network({}),
            stations,
            {"latitude": 37.5, "longitude": 127.0},
            {"latitude": 37.55, "longitude": 127.0},
            lambda *a: None,
            ScheduledConfig(),
        )
        return n

    def test_all_stations_and_non_nearest_od_remain(self):
        n = self.make_network()
        self.assertEqual(sum(v.get("kind") == "bike" for v in n.nodes.values()), 20)
        self.assertIn(
            "bike:19", {e["to"] for e in LazyConnections(n).bikes_from("bike:0")}
        )
        self.assertFalse(n.metadata["station_top_n_pruning"])
        self.assertFalse(n.metadata["bike_od_pruning"])

    def test_previous_disconnected_destination_is_reachable(self):
        n = self.make_network(6)
        r = Router(
            n,
            lambda *a: {"minutes": 5, "p_empty": 0},
            Config(top_k=1),
            walking_provider=lambda *a: None,
        ).search("origin", "destination", START)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["routes"][0]["bike_rentals"], 1)
        self.assertEqual(r["heuristic_mode"], "zero_admissible_lazy_graph")

    def test_lazy_walk_budget_is_reported(self):
        n = Network(
            {
                "origin": {"latitude": 37.5, "longitude": 127.0},
                "destination": {"latitude": 37.501, "longitude": 127.0},
            },
            walk_policy={"max_distance_m": 900},
        )

        def blocked(*a):
            raise CallLimit("budget")

        r = Router(n, None, Config(top_k=1), walking_provider=blocked).search(
            "origin", "destination", START
        )
        self.assertEqual(r["status"], "partial")
        self.assertEqual(r["termination_reason"], "connector_limit")

    def test_known_route_survives_slow_optional_walk_lookup(self):
        nodes = {
            "origin": {"latitude": 37.5, "longitude": 127.0},
            "near": {"latitude": 37.5001, "longitude": 127.0, "kind": "stop"},
            "destination": {"latitude": 37.502, "longitude": 127.0},
        }
        net = Network(
            nodes,
            walks=[{"from": "origin", "to": "destination", "minutes": 3}],
            walk_policy={"max_distance_m": 900},
        )
        clock = [0.0]

        def slow_walk(a, b, start, end):
            clock[0] += 2.0
            return {"from": start, "to": end, "minutes": 1}

        with patch("multimodal.time.monotonic", side_effect=lambda: clock[0]):
            result = Router(
                net, None, Config(top_k=1, max_seconds=1), walking_provider=slow_walk
            ).search("origin", "destination", START)
        self.assertEqual(result["routes"][0]["total_minutes"], 3)
        self.assertEqual(result["known_route_count"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["search_complete"])
        self.assertEqual(result["termination_reason"], "max_seconds")

    def test_known_walk_and_bus_chain_survives_lookup_timeout(self):
        nodes = {
            name: {"latitude": 37.5 + i * 0.001, "longitude": 127.0, "kind": "stop"}
            for i, name in enumerate(("origin", "near", "A", "B", "destination"))
        }
        net = Network(
            nodes,
            walks=[
                {"from": "origin", "to": "A", "minutes": 1},
                {"from": "B", "to": "destination", "minutes": 1},
            ],
            transit=[
                {
                    "from": "A",
                    "to": "B",
                    "mode": "bus",
                    "trip_id": "B1",
                    "boarding_seconds": 15,
                    "departure": "2026-10-20T10:02:00+09:00",
                    "arrival": "2026-10-20T10:05:00+09:00",
                }
            ],
            walk_policy={"max_distance_m": 900},
        )
        clock = [0.0]

        def slow_walk(*args):
            clock[0] += 2.0
            return None

        with patch("multimodal.time.monotonic", side_effect=lambda: clock[0]):
            result = Router(
                net, None, Config(top_k=1, max_seconds=1), walking_provider=slow_walk
            ).search("origin", "destination", START)
        self.assertEqual(result["routes"][0]["total_minutes"], 6)
        self.assertEqual(result["routes"][0]["transit_boardings"], 1)
        self.assertEqual(result["status"], "partial")

    def test_full_search_can_replace_known_route_with_better_route(self):
        nodes = {
            "origin": {"latitude": 37.5, "longitude": 127.0},
            "near": {"latitude": 37.501, "longitude": 127.0, "kind": "stop"},
            "destination": {"latitude": 37.502, "longitude": 127.0},
        }
        net = Network(
            nodes,
            walks=[{"from": "origin", "to": "destination", "minutes": 10}],
            walk_policy={"max_distance_m": 900},
        )

        def walking(a, b, start, end):
            if (start, end) in (("origin", "near"), ("near", "destination")):
                return {"from": start, "to": end, "minutes": 1}

        result = Router(net, None, Config(top_k=1), walking_provider=walking).search(
            "origin", "destination", START
        )
        self.assertEqual(result["known_route_count"], 1)
        self.assertEqual(result["routes"][0]["total_minutes"], 2)
        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(result["status"], "ok")

    def test_bike_station_to_station_walks_remain_excluded(self):
        nodes = {
            name: {
                "latitude": 37.5 + i * 0.001,
                "longitude": 127.0,
                "kind": "bike",
                "station_id": name,
            }
            for i, name in enumerate(("A", "B"))
        }
        calls = []

        def walking(*args):
            calls.append(args)
            return {"from": "A", "to": "B", "minutes": 1}

        net = Network(nodes, walk_policy={"max_distance_m": 900})
        self.assertEqual(LazyConnections(net, walking).walks_from("A"), [])
        self.assertEqual(calls, [])

    def test_two_midjourney_bicycle_transfers_with_full_station_catalog(self):
        from routing_errors import NoBicycleRoute

        stations = [
            {"station_id": str(i), "latitude": 38.2 + i * 0.0001, "longitude": 128.0}
            for i in range(16)
        ]
        stations += [
            {
                "station_id": str(16 + i),
                "latitude": 37.52 + i * 0.02,
                "longitude": 127.0,
            }
            for i in range(4)
        ]
        nodes = {
            k: {"latitude": lat, "longitude": 127.0, "kind": "stop"}
            for k, lat in zip("OABCDZ", (37.5, 37.52, 37.54, 37.56, 37.58, 37.60))
        }

        def at(m):
            return f"2026-10-20T10:{m:02d}:00+09:00"

        transit = [
            {
                "from": a,
                "to": b,
                "departure": at(dep),
                "arrival": at(arr),
                "mode": mode,
                "trip_id": a + b,
            }
            for a, b, dep, arr, mode in [
                ("O", "A", 1, 5, "bus"),
                ("B", "C", 12, 17, "subway"),
                ("D", "Z", 25, 30, "bus"),
            ]
        ]
        net = build_scheduled_network(
            Network(nodes, transit=transit),
            stations,
            nodes["O"],
            nodes["Z"],
            lambda *a: None,
            ScheduledConfig(),
        )

        def eta(edge, departure):
            if (edge["eta_from"], edge["eta_to"]) not in [("16", "17"), ("18", "19")]:
                raise NoBicycleRoute("synthetic unavailable OD")
            return {"minutes": 5, "p_empty": 0}

        r = Router(
            net, eta, Config(top_k=1, max_seconds=3), walking_provider=lambda *a: None
        ).search("origin", "destination", START)
        self.assertTrue(r["routes"])
        self.assertEqual(r["routes"][0]["bike_rentals"], 2)
        self.assertEqual(r["routes"][0]["transit_boardings"], 3)

    def test_wind_wraparound_keeps_north_wind_north(self):
        import numpy as np, pandas as pd
        from collections.abc import Mapping

        module = ast.parse((ROOT / "src/eta_service.py").read_text(encoding="utf-8"))
        fields = next(
            ast.literal_eval(n.value)
            for n in module.body
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "WEATHER_API_VARS"
                for t in n.targets
            )
        )
        cls = next(
            n
            for n in module.body
            if isinstance(n, ast.ClassDef) and n.name == "BicycleETAService"
        )
        fn = next(
            n
            for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == "_attach_weather"
        )
        scope = {"np": np, "pd": pd, "Mapping": Mapping, "WEATHER_API_VARS": fields}
        exec(
            compile(ast.Module(body=[fn], type_ignores=[]), "<feature-method>", "exec"),
            scope,
        )
        frame, _ = scope["_attach_weather"](
            None,
            pd.DataFrame([{"route_bearing_deg": 0}]),
            {
                "origin": {"wind_speed_ms": 4, "wind_direction_deg": 350},
                "destination": {"wind_speed_ms": 4, "wind_direction_deg": 10},
            },
        )
        self.assertAlmostEqual(frame.headwind_ms.iloc[0], 4 * np.cos(np.deg2rad(10)))

    def test_new_forecast_issue_invalidates_context(self):
        tz = timezone(timedelta(hours=9))
        self.assertNotEqual(
            forecast_issue_key(datetime(2026, 10, 20, 2, 10, tzinfo=tz)),
            forecast_issue_key(datetime(2026, 10, 20, 2, 30, tzinfo=tz)),
        )

    def test_past_departure_rejected_before_model_import(self):
        from scheduled_runtime import run_scheduled_optimization

        with self.assertRaisesRegex(ValueError, "이미 지났"):
            run_scheduled_optimization(
                {"latitude": 37.5, "longitude": 127},
                {"latitude": 37.55, "longitude": 127},
                "2000-01-01T10:00:00+09:00",
                use_kma=False,
            )

    def test_automatic_departure_is_resolved_after_model_ready(self):
        from scheduled_runtime import run_scheduled_optimization

        service = object()
        with patch(
            "scheduled_runtime._run_scheduled_optimization", return_value={"ok": True}
        ) as run:
            self.assertEqual(
                run_scheduled_optimization(
                    {"latitude": 37.5, "longitude": 127},
                    {"latitude": 37.55, "longitude": 127},
                    None,
                    eta_service=service,
                    use_kma=False,
                ),
                {"ok": True},
            )
        self.assertIsNone(run.call_args.args[2])
        self.assertIs(run.call_args.kwargs["eta_service"], service)

    def test_automatic_request_warms_and_closes_model_before_inner_run(self):
        from scheduled_runtime import run_scheduled_optimization

        events = []

        class Model:
            def __enter__(self):
                events.append("ready")
                return self

            def __exit__(self, *args):
                events.append("closed")

        def inner(*args, **kwargs):
            self.assertEqual(events, ["ready"])
            self.assertIsNone(args[2])
            return {"ok": True}

        with (
            patch.dict(
                sys.modules,
                {"eta_service": types.SimpleNamespace(BicycleETAService=Model)},
            ),
            patch('shared_assets.prepare_route_cache'),
            patch("scheduled_runtime._run_scheduled_optimization", side_effect=inner),
        ):
            run_scheduled_optimization(
                {"latitude": 37.5, "longitude": 127},
                {"latitude": 37.55, "longitude": 127},
                None,
                use_kma=False,
            )
        self.assertEqual(events, ["ready", "closed"])

    def test_old_timetable_requires_review(self):
        import tempfile, json
        from transit_schedule import build_timetable

        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "prepared.json").write_text(
                json.dumps({"valid_from": "2000-01-01"})
            )
            with self.assertRaisesRegex(ValueError, "재검토"):
                build_timetable(folder, START)

    def test_installer_records_safe_platform_specific_constraints(self):
        import install_project, tempfile

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            fake_dist = types.SimpleNamespace(
                metadata={"Name": "example"}, version="1.2.3"
            )
            with (
                patch.object(install_project, "ROOT", root),
                patch("install_project.subprocess.run") as run,
                patch(
                    "install_project.metadata.distributions", return_value=[fake_dist]
                ),
            ):
                install_project.main()
                lock = next((root / "config").glob("constraints-*.txt"))
                self.assertIn("example==1.2.3", lock.read_text())
                self.assertEqual(run.call_count, 3)
                install_project.main()
                self.assertIn("-c", run.call_args_list[-2].args[0])

    def test_full_scheduled_runtime_with_fake_services(self):
        import tempfile
        import scheduled_runtime as runtime

        when = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
        departure = when.isoformat()
        n = Network(
            {
                "S": {"latitude": 37.5, "longitude": 127.0, "kind": "stop"},
                "T": {"latitude": 37.51, "longitude": 127.0, "kind": "stop"},
            },
            transit=[
                {
                    "from": "S",
                    "to": "T",
                    "departure": (when + timedelta(minutes=10)).isoformat(),
                    "arrival": (when + timedelta(minutes=20)).isoformat(),
                    "mode": "bus",
                    "trip_id": "T",
                }
            ],
            metadata={"valid_departure": departure, "valid_horizon_minutes": 180},
        )

        class Client:
            def __init__(self, *a, **k):
                pass

            def get(self, *a):
                return (
                    {
                        "status": "OK",
                        "route": {
                            "properties": {"totalTime": 60, "totalDistance": 80},
                            "legs": [],
                        },
                    },
                    departure,
                )

            def stats(self):
                return {"new_api_calls": 0, "cache_hits": 0}

            def close(self):
                pass

        class ETA:
            def predict(self, **kwargs):
                return {
                    "eta": {"p50_min": 3, "p80_min": 4, "p90_min": 5},
                    "route": {
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[127, 37.5], [127, 37.51]],
                        }
                    },
                    "model": {"weather_available": False},
                }

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog = root / "stations.csv"
            catalog.write_text(
                "station_id,station_name,latitude,longitude\n1,A,37.5001,127\n2,B,37.5099,127\n",
                encoding="utf-8",
            )
            with (
                patch.object(runtime, "STATION_CATALOG", catalog),
                patch.object(runtime, "RUNTIME_CACHE_DIR", root / "cache"),
                patch.object(runtime, "KakaoClient", Client),
                patch.object(runtime, "_artifact_identity", return_value=[]),
                patch("transit_geometry.attach_transit_geometry"),
            ):
                result = runtime.run_scheduled_optimization(
                    n.nodes["S"],
                    n.nodes["T"],
                    departure,
                    timetable=n,
                    eta_service=ETA(),
                    use_kma=False,
                    use_inventory=False,
                    config=ScheduledConfig(
                        top_k=1,
                        require_known_inventory=False,
                        max_empty_probability=None,
                    ),
                    output_dir=root / "out",
                    progress=None,
                )
            self.assertTrue(result["routes"])
            self.assertTrue((root / "out/result.json").exists())
            self.assertFalse((root / "out/network.json").exists())

    def test_cache_hits_still_work_after_walk_call_limit(self):
        n = Network(
            {
                "origin": {"latitude": 37.5, "longitude": 127.0},
                "near": {"latitude": 37.501, "longitude": 127.0, "kind": "stop"},
                "destination": {"latitude": 37.502, "longitude": 127.0},
            },
            walk_policy={"max_distance_m": 900},
        )

        def walking(a, b, start, end):
            if end == "destination":
                return {"from": start, "to": end, "minutes": 1}
            raise CallLimit("uncached request blocked")

        r = Router(n, None, Config(top_k=1), walking_provider=walking).search(
            "origin", "destination", START
        )
        self.assertTrue(r["routes"])
        self.assertEqual(r["status"], "partial")

    def test_spatial_index_retains_all_points_in_radius(self):
        import random
        from candidate_edges import SpatialIndex
        from multimodal_data import distance

        rng = random.Random(7)
        nodes = {
            str(i): {
                "latitude": 37.4 + rng.random() * 0.3,
                "longitude": 126.8 + rng.random() * 0.4,
            }
            for i in range(200)
        }
        index = SpatialIndex(nodes)
        for point in list(nodes.values())[:10]:
            expected = {k for k, v in nodes.items() if distance(point, v) <= 7000}
            self.assertEqual({k for _, k in index.near(point, 7000)}, expected)

    def test_invalid_eta_is_a_partial_candidate_failure_not_query_crash(self):
        from multimodal import V521BikeProvider

        class ETA:
            def predict(self, **kwargs):
                return {"eta": {"p50_min": 0}, "route": {}, "model": {}}

        provider = V521BikeProvider(ETA())
        n = Network(
            {"O": {}, "Z": {}},
            bikes=[{"from": "O", "to": "Z", "eta_from": "1", "eta_to": "2"}],
            walks=[{"from": "O", "to": "Z", "minutes": 2}],
        )
        r = Router(n, provider, Config(top_k=1)).search("O", "Z", START)
        self.assertTrue(r["routes"])
        self.assertEqual(r["status"], "partial")


if __name__ == "__main__":
    unittest.main()
