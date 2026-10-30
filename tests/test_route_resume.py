"""A reload reconnects only to this browser's existing job, without resubmission."""

from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fastapi.testclient import TestClient
from web_server import create_app
from test_web_server import FakeService, FakeModel, Weather


class RouteResumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.started, self.release = threading.Event(), threading.Event()
        outer = self
        FakeService.calls = []

        class Service(FakeService):
            def find_routes(self, *args, on_routes=None, **kwargs):
                result = super().find_routes(*args, **kwargs)
                if on_routes:
                    on_routes(result)
                outer.started.set()
                if not outer.release.wait(5):
                    raise RuntimeError("test timeout")
                return result

        self.app = create_app(
            lambda: Service(
                Path(self.directory.name) / "db.sqlite", FakeModel(), Weather()
            ),
            testing=True,
        )
        self.client = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self.release.set)
        self.client.get("/api/session")
        self.query = dict(
            origin=dict(name="녹천역", latitude=37.644, longitude=127.052),
            destination=dict(name="중계역", latitude=37.644, longitude=127.064),
            mode="transit",
            use_kma=False,
        )

    def start(self):
        result = self.client.post("/api/routes", json=self.query)
        self.assertEqual(result.status_code, 202)
        self.assertTrue(self.started.wait(2))
        return result.json()["job_id"]

    def finish(self, job_id):
        self.release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = self.client.get("/api/jobs/" + job_id).json()
            if job["status"] == "done":
                return job
            time.sleep(0.01)
        self.fail("job did not finish")

    def test_fresh_page_with_same_cookies_recovers_running_job_and_partial(self):
        job_id = self.start()
        other = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(other.close)
        other.cookies.update(self.client.cookies)
        other.get("/api/session")
        response = other.get("/api/routes/current")
        self.assertEqual(response.headers["cache-control"], "no-store")
        job = response.json()["job"]
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(job["status"], "running")
        self.assertTrue(job["partial_result"]["routes"])
        self.assertEqual(job["route_query"]["origin"]["name"], "녹천역")
        self.assertEqual(job["route_query"]["destination"]["name"], "중계역")
        self.assertNotIn("owner", job)
        self.assertEqual(other.post("/api/routes", json=self.query).status_code, 409)
        self.finish(job_id)
        self.assertEqual(len(FakeService.calls), 1)

    def test_completed_result_is_recoverable_without_a_new_search(self):
        job_id = self.start()
        completed = self.finish(job_id)
        job = self.client.get("/api/routes/current").json()["job"]
        self.assertEqual(job["job_id"], job_id)
        self.assertEqual(job["result"], completed["result"])
        self.assertEqual(len(FakeService.calls), 1)

    def test_another_browser_cannot_discover_the_route(self):
        job_id = self.start()
        other = TestClient(self.app)
        self.addCleanup(other.close)
        self.assertIsNone(other.get("/api/routes/current").json()["job"])
        self.assertEqual(other.get("/api/jobs/" + job_id).status_code, 404)

    def test_completed_job_expires_but_active_job_does_not(self):
        job_id = self.start()
        backend = self.app.state.backend
        with backend.lock:
            backend.jobs[job_id]["created"] -= 901
        self.assertEqual(
            self.client.get("/api/routes/current").json()["job"]["job_id"], job_id
        )
        self.finish(job_id)
        self.assertIsNone(self.client.get("/api/routes/current").json()["job"])
