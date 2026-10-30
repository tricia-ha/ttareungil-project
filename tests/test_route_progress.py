import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from route_progress import notify_progress
from web_server import Backend, create_app
from fastapi.testclient import TestClient
from test_web_server import FakeService, FakeModel, Weather


class ProgressTests(unittest.TestCase):
    def test_callback_errors_do_not_interrupt_computation(self):
        def broken(stage):
            raise ValueError("display failed")

        notify_progress(broken, "model")
        seen = []
        notify_progress(seen.append, "unknown")
        notify_progress(seen.append, "searching")
        self.assertEqual(seen, ["searching"])

    def test_snapshot_preserves_partial_and_freezes_finished_clock(self):
        backend = Backend.__new__(Backend)
        backend.lock = threading.RLock()
        job = dict(
            kind="route", status="running", owner=("private", "profile"), created=10
        )
        backend.jobs = {"job": job}
        with patch("web_server.time.monotonic", return_value=20):
            backend._progress(job, "model")
        backend.publish("job", {"routes": [{"total_minutes": 5}]})
        with patch("web_server.time.monotonic", return_value=30):
            backend.publish("job", {"progress_stage": "results"})
        job["finished"] = 35
        with patch("web_server.time.monotonic", return_value=999):
            value = backend.snapshot(job)
        self.assertEqual(value["progress"]["elapsed_seconds"], 25)
        self.assertEqual(value["progress"]["stage_elapsed_seconds"], 5)
        self.assertEqual(value["partial_result"]["routes"][0]["total_minutes"], 5)
        self.assertEqual(value["revision"], 1)
        for key in ("owner", "created", "stage_started", "finished"):
            self.assertNotIn(key, value)
        backend.publish("job", {"progress_stage": "private-path"})
        self.assertEqual(job["progress"]["stage"], "results")

    def test_http_stage_survives_resume_and_completion(self):
        entered = threading.Event()
        release = threading.Event()

        class Service(FakeService):
            def find_routes(self, origin, destination, **options):
                options["on_progress"]("timetable")
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test timed out")
                options["on_progress"]("results")
                return super().find_routes(origin, destination, **options)

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.dict(os.environ, {"KAKAO_REST_API_KEY": "test-key"}),
        ):
            app = create_app(
                lambda: Service(Path(folder) / "db.sqlite", FakeModel(), Weather()),
                testing=True,
            )
            with TestClient(app, headers={"Origin": "http://testserver"}) as client:
                try:
                    response = client.post(
                        "/api/routes",
                        json=dict(
                            origin=dict(latitude=37.5, longitude=127),
                            destination=dict(latitude=37.51, longitude=127),
                            mode="transit",
                            use_kma=False,
                        ),
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    job_id = response.json()["job_id"]
                    self.assertTrue(entered.wait(3))
                    current = client.get("/api/jobs/" + job_id).json()
                    self.assertEqual(current["progress"]["stage"], "timetable")
                    restored = client.get("/api/routes/current").json()["job"]
                    self.assertEqual(restored["job_id"], job_id)
                    self.assertEqual(restored["progress"]["stage"], "timetable")
                    self.assertNotIn("created", restored)
                finally:
                    release.set()
                for _ in range(60):
                    result = client.get("/api/jobs/" + job_id).json()
                    if result["status"] == "done":
                        break
                    time.sleep(0.02)
                self.assertEqual(result["status"], "done")
                self.assertTrue(result["result"]["routes"])


if __name__ == "__main__":
    unittest.main()
