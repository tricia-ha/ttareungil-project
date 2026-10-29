from datetime import datetime, timedelta

"""Completed route snapshots, ownership and shortest-first live updates."""

import sys, unittest, tempfile, threading, time, copy, os
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).parent),
]
from multimodal import Network, Router, Config
from project_service import ProjectService
from web_server import create_app
from fastapi.testclient import TestClient
from test_web_server import FakeService, FakeModel, Weather

START = "2026-10-30T08:30:00+09:00"


def graph():
    return Network(
        nodes={
            "origin": dict(latitude=37.5, longitude=127),
            "B": dict(latitude=37.51, longitude=127),
            "destination": dict(latitude=37.52, longitude=127),
        },
        walks=[
            {"from": "origin", "to": "destination", "minutes": 10},
            {"from": "B", "to": "destination", "minutes": 1},
        ],
        bikes=[{"from": "origin", "to": "B"}],
        walk_policy={"max_distance_m": 1},
    )


class LiveRouterTests(unittest.TestCase):
    def test_known_route_is_published_before_better_route_and_final_is_unchanged(self):
        snapshots = []

        def callback(value):
            snapshots.append(copy.deepcopy(value))

        result = Router(
            graph(),
            lambda *a: {"minutes": 1, "p_empty": 0},
            Config(top_k=1),
            walking_provider=lambda *a: None,
            on_routes=callback,
        ).search("origin", "destination", START)
        self.assertEqual([v["routes"][0]["total_minutes"] for v in snapshots], [10, 2])
        self.assertTrue(
            all(
                v["status"] == "searching" and not v["search_complete"]
                for v in snapshots
            )
        )
        baseline = Router(
            graph(),
            lambda *a: {"minutes": 1, "p_empty": 0},
            Config(top_k=1),
            walking_provider=lambda *a: None,
        ).search("origin", "destination", START)
        self.assertEqual(result["routes"], baseline["routes"])

    def test_display_callback_cannot_mutate_search_results(self):
        def callback(value):
            value["routes"][0]["total_minutes"] = 999

        r = Router(
            graph(),
            lambda *a: {"minutes": 1, "p_empty": 0},
            Config(top_k=1),
            walking_provider=lambda *a: None,
            on_routes=callback,
        ).search("origin", "destination", START)
        self.assertEqual(r["routes"][0]["total_minutes"], 2)

    def test_callback_failure_does_not_abort_search(self):
        def callback(value):
            raise RuntimeError("display failure")

        r = Router(
            graph(),
            lambda *a: {"minutes": 1, "p_empty": 0},
            Config(top_k=1),
            walking_provider=lambda *a: None,
            on_routes=callback,
        ).search("origin", "destination", START)
        self.assertEqual(r["routes"][0]["total_minutes"], 2)
        self.assertGreater(r["network_metadata"]["route_update_errors"], 0)

    def test_display_work_does_not_consume_route_search_budget(self):
        clock = [0.0]

        def callback(value):
            clock[0] += 5

        with patch("multimodal.time.monotonic", side_effect=lambda: clock[0]):
            result = Router(
                graph(),
                lambda *a: {"minutes": 1, "p_empty": 0},
                Config(top_k=1, max_seconds=1),
                walking_provider=lambda *a: None,
                on_routes=callback,
            ).search("origin", "destination", START)
        self.assertEqual(result["routes"][0]["total_minutes"], 2)
        self.assertGreaterEqual(result["route_update_seconds"], 10)

    def test_python_callback_is_not_part_of_validated_json_input(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ProjectService(db_path=Path(directory) / "db.sqlite")
            callback = lambda result: None
            try:
                with patch.dict(os.environ, {"KAKAO_REST_API_KEY": "unit-test"}), patch(
                    "scheduled_runtime.run_scheduled_optimization",
                    return_value={"routes": []},
                ) as run:
                    service.find_routes(
                        {"latitude": 37.5, "longitude": 127},
                        {"latitude": 37.51, "longitude": 127},
                        mode="transit",
                        on_routes=callback,
                    )
                    self.assertIs(run.call_args.kwargs["on_routes"], callback)
            finally:
                service.close()


def payload(values):
    routes = []
    for minutes in values:
        arrival = (
            datetime.fromisoformat(START) + timedelta(minutes=minutes)
        ).isoformat()
        routes.append(
            dict(
                total_minutes=minutes,
                arrival=arrival,
                route_kind="transit",
                legs=[
                    dict(
                        mode="bus",
                        **{"from": "origin", "to": "destination"},
                        trip_id=str(minutes),
                        minutes=minutes,
                        start=START,
                        arrival=arrival,
                    )
                ],
            )
        )
    return dict(
        routes=routes,
        mixed_routes=[],
        status="searching",
        search_complete=False,
        departure=START,
        search_mode="transit",
        nodes={
            "origin": {"name": "A", "latitude": 37.5, "longitude": 127},
            "destination": {"name": "B", "latitude": 37.51, "longitude": 127},
        },
    )


class LiveWebTests(unittest.TestCase):
    def run_scenario(self, fail=False):
        first, second, release, finish = [threading.Event() for _ in range(4)]

        class Streaming(FakeService):
            def find_routes(self, *args, on_routes=None, **kwargs):
                value = payload([15])
                on_routes(value)
                value["routes"][0]["total_minutes"] = 999
                first.set()
                release.wait(5)
                if fail:
                    raise RuntimeError("late failure")
                on_routes(payload([15, 10]))
                second.set()
                finish.wait(5)
                value = payload([10, 15])
                value.update(status="ok", search_complete=True)
                return value

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                lambda: Streaming(
                    db_path=Path(directory) / "db.sqlite",
                    eta_service=FakeModel(),
                    weather_provider=Weather(),
                ),
                testing=True,
            )
            with TestClient(app, headers={"Origin": "http://testserver"}) as client:
                try:
                    client.get("/api/session")
                    q = dict(
                        origin={"latitude": 37.5, "longitude": 127},
                        destination={"latitude": 37.51, "longitude": 127},
                        mode="transit",
                    )
                    response = client.post("/api/routes", json=q)
                    self.assertEqual(response.status_code, 202)
                    url = "/api/jobs/" + response.json()["job_id"]
                    self.assertTrue(first.wait(2))
                    initial = client.get(url).json()
                    self.assertEqual(initial["status"], "running")
                    self.assertEqual(
                        initial["partial_result"]["routes"][0]["total_minutes"], 15
                    )
                    other = TestClient(app, headers={"Origin": "http://testserver"})
                    self.assertEqual(other.get(url).status_code, 404)
                    other.close()
                    release.set()
                    if not fail:
                        self.assertTrue(second.wait(2))
                        live = client.get(url).json()
                        self.assertEqual(
                            [
                                r["total_minutes"]
                                for r in live["partial_result"]["routes"]
                            ],
                            [10, 15],
                        )
                        self.assertGreater(live["revision"], initial["revision"])
                    finish.set()
                    for _ in range(100):
                        result = client.get(url).json()
                        if result["status"] == "done":
                            break
                        time.sleep(0.01)
                    self.assertEqual(result["status"], "done")
                    self.assertNotIn("partial_result", result)
                    self.assertEqual(
                        result["result"]["routes"][0]["total_minutes"],
                        15 if fail else 10,
                    )
                    if fail:
                        self.assertEqual(result["result"]["status"], "partial")
                finally:
                    release.set()
                    finish.set()

    def test_live_results_before_completion_are_sorted_and_private(self):
        self.run_scenario()

    def test_error_after_live_result_keeps_completed_route(self):
        self.run_scenario(fail=True)


if __name__ == "__main__":
    unittest.main()
