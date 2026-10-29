"""Concurrent requests must respect capacity, model sharing and API budgets."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fastapi import HTTPException
from kakao_routes import KakaoClient, CallLimit, WalkingDeadline
from project_service import ProjectService
from web_capacity import Capacity, GIB, LockedModel
from web_server import Backend, Session

A = dict(latitude=37.5, longitude=127)
B = dict(latitude=37.51, longitude=127)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        cores = patch("web_capacity.os.cpu_count", return_value=16)
        cores.start()
        self.addCleanup(cores.stop)

    def test_cold_model_blocks_additional_routes_until_ready(self):
        ready = [False]
        capacity = Capacity(
            probe=lambda: dict(available=32 * GIB, total=32 * GIB, cpu=0),
            model_ready=lambda: ready[0],
        )
        began = threading.Event()

        def additional():
            with capacity.slot("transit"):
                began.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with capacity.slot("route"):
                future = pool.submit(additional)
                self.assertFalse(began.wait(0.04))
                ready[0] = True
                with capacity.condition:
                    capacity.condition.notify_all()
                self.assertTrue(began.wait(1))
                future.result(timeout=1)
        with capacity.slot("transit"):
            self.assertTrue(capacity._allowed("transit"))

    def test_two_running_four_waiting_and_queue_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            service = SimpleNamespace(
                db_path=Path(folder) / "web.sqlite",
                eta_service=object(),
                close=lambda: None,
            )
            backend = Backend(lambda: service, testing=True)
            releases = [threading.Event() for _ in range(6)]
            started = [threading.Event() for _ in range(6)]

            def work(index):
                started[index].set()
                if not releases[index].wait(5):
                    raise RuntimeError("test release timeout")
                return {"request": index}

            try:
                ids = [
                    backend.submit(Session(str(i)), "route", lambda i=i: work(i))[
                        "job_id"
                    ]
                    for i in range(6)
                ]
                self.assertTrue(started[0].wait(2))
                self.assertTrue(started[1].wait(2))
                self.assertFalse(started[2].is_set())
                self.assertEqual(backend.capacity.routes, 2)
                self.assertEqual(
                    sum(j["status"] == "queued" for j in backend.jobs.values()), 4
                )
                with self.assertRaises(HTTPException) as rejected:
                    backend.submit(Session("overflow"), "route", lambda: {})
                self.assertEqual(rejected.exception.status_code, 429)
                releases[0].set()
                self.assertTrue(started[2].wait(2))
                for event in releases:
                    event.set()
                deadline = time.monotonic() + 3
                while (
                    any(j["status"] != "done" for j in backend.jobs.values())
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                for i, job_id in enumerate(ids):
                    self.assertEqual(backend.jobs[job_id]["result"], {"request": i})
                    self.assertEqual(backend.jobs[job_id]["owner"][0], str(i))
            finally:
                for event in releases:
                    event.set()
                backend.close()

    def test_memory_and_cpu_defer_only_the_additional_route(self):
        state = dict(available=4 * GIB, total=32 * GIB, cpu=0)
        capacity = Capacity(probe=lambda: state)
        began = threading.Event()

        def additional():
            with capacity.slot("route"):
                began.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with capacity.slot("route"):
                waiting = pool.submit(additional)
                self.assertFalse(began.wait(0.04))
                state.update(available=10 * GIB, cpu=90)
                with capacity.condition:
                    capacity.condition.notify_all()
                self.assertFalse(began.wait(0.04))
                state["cpu"] = 10
                with capacity.condition:
                    capacity.condition.notify_all()
                self.assertTrue(began.wait(1))
                waiting.result(timeout=1)

    def test_ocr_is_exclusive_and_shutdown_unblocks_waiters(self):
        capacity = Capacity(
            probe=lambda: dict(available=32 * GIB, total=32 * GIB, cpu=0)
        )
        began = threading.Event()

        def ocr():
            with capacity.slot("screenshot"):
                began.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with capacity.slot("route"):
                future = pool.submit(ocr)
                self.assertFalse(began.wait(0.04))
                capacity.close()
                with self.assertRaisesRegex(ValueError, "종료"):
                    future.result(timeout=1)
        self.assertFalse(began.is_set())

    def test_low_memory_wait_has_a_finite_timeout(self):
        capacity = Capacity(
            probe=lambda: dict(available=GIB, total=32 * GIB, cpu=0), wait_seconds=0.01
        )
        with self.assertRaisesRegex(ValueError, "여유 자원"):
            with capacity.slot("route"):
                self.fail("must remain queued")
        self.assertEqual(capacity.routes, 0)

    def test_model_is_loaded_once_and_shared_inference_is_serialized(self):
        count = 0
        active = 0
        peak = 0

        class Model:
            model_version = "unchanged"

            def predict(self, value):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                time.sleep(0.01)
                active -= 1
                return value * 2

            def close(self):
                pass

        def construct():
            nonlocal count
            count += 1
            time.sleep(0.02)
            return Model()

        with patch.dict(
            sys.modules, {"eta_service": SimpleNamespace(BicycleETAService=construct)}
        ):
            with ProjectService() as service, ThreadPoolExecutor(max_workers=2) as pool:
                models = list(pool.map(lambda _: service.shared_model(), range(2)))
                self.assertIs(models[0], models[1])
                self.assertEqual(count, 1)
                self.assertEqual(models[0].model_version, "unchanged")
                self.assertEqual(
                    list(pool.map(models[0].predict, [3, 5, 7, 9])), [6, 10, 14, 18]
                )
                self.assertEqual(peak, 1)
                self.assertIsInstance(models[0], LockedModel)

    def test_simultaneous_identical_walking_query_uses_one_http_call(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "api.sqlite"
            entered, release, second = [threading.Event() for _ in range(3)]
            response = Mock(status_code=200)
            response.json.return_value = {"status": "OK"}
            hits = []

            def http(*args, **kwargs):
                hits.append(1)
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")
                return response

            def query(index):
                with closing(
                    KakaoClient(path, "test-key", interval_seconds=0, progress=None)
                ) as client:
                    client.session.get = http
                    if index:
                        second.set()
                    payload = client.get("walk", A, B)
                    return payload, client.stats()

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(query, 0)
                try:
                    self.assertTrue(entered.wait(1))
                    other = pool.submit(query, 1)
                    self.assertTrue(second.wait(1))
                    time.sleep(0.03)
                    self.assertEqual(len(hits), 1)
                finally:
                    release.set()
                a, b = first.result(timeout=2), other.result(timeout=2)
            self.assertEqual(a[0], b[0])
            self.assertEqual(a[1]["new_api_calls"] + b[1]["new_api_calls"], 1)
            self.assertEqual(len(hits), 1)

    def test_daily_limit_is_shared_across_concurrent_searches(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "api.sqlite"
            response = Mock(status_code=200)
            response.json.return_value = {"status": "OK"}
            barrier = threading.Barrier(4)

            def query(index):
                with closing(
                    KakaoClient(
                        path,
                        "test-key",
                        daily_limit=2,
                        interval_seconds=0,
                        progress=None,
                    )
                ) as client:
                    client.session.get = Mock(return_value=response)
                    barrier.wait(timeout=3)
                    try:
                        client.get(
                            "walk",
                            A,
                            dict(latitude=37.51 + index / 1000, longitude=127),
                        )
                    except CallLimit:
                        pass
                    return client.calls

            with ThreadPoolExecutor(max_workers=4) as pool:
                self.assertEqual(sum(pool.map(query, range(4))), 2)

    def test_duplicate_query_wait_respects_search_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "api.sqlite"
            entered, release = threading.Event(), threading.Event()
            response = Mock(status_code=200)
            response.json.return_value = {"status": "OK"}

            def query(index):
                with closing(
                    KakaoClient(path, "test-key", interval_seconds=0, progress=None)
                ) as client:

                    def http(*args, **kwargs):
                        entered.set()
                        release.wait(2)
                        return response

                    client.session.get = http
                    if index:
                        client.deadline = time.monotonic() + 0.03
                        with self.assertRaises(WalkingDeadline):
                            client.get("walk", A, B)
                        self.assertEqual(client.calls, 0)
                    else:
                        client.get("walk", A, B)

            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(query, 0)
                try:
                    self.assertTrue(entered.wait(1))
                    pool.submit(query, 1).result(timeout=1)
                finally:
                    release.set()
                first.result(timeout=2)
