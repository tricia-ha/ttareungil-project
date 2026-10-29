"""Sharing a URL or profile identifier must not share an authenticated account."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fastapi.testclient import TestClient
from web_server import create_app
from test_web_server import FakeService, FakeModel, Weather


class AccountIsolationTests(unittest.TestCase):
    def test_https_pc_login_does_not_sign_in_a_separate_phone(self):
        devices = []
        for agent in (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1",
        ):
            device = TestClient(
                self.app,
                base_url="https://testserver",
                headers={"Origin": "https://testserver", "User-Agent": agent},
            )
            self.addCleanup(device.close)
            devices.append(device)
        pc, phone = devices
        login = pc.post("/api/session", json=self.credentials_a)
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["profile_id"], self.profile_a)
        self.assertIn("Secure", login.headers["set-cookie"])
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertEqual(phone.get("/").status_code, 200)
        phone_session = phone.get("/api/session").json()
        self.assertIsNone(phone_session["profile_id"])
        self.assertIsNone(phone_session["username"])
        self.assertEqual(phone.get("/api/rides").status_code, 401)
        self.assertEqual(pc.get("/api/session").json()["profile_id"], self.profile_a)
        self.assertNotEqual(
            pc.cookies.get("tw_browser"), phone.cookies.get("tw_browser")
        )
        phone.post("/api/session", json=self.credentials_b).raise_for_status()
        self.assertEqual(phone.get("/api/session").json()["profile_id"], self.profile_b)
        self.assertEqual(pc.get("/api/session").json()["profile_id"], self.profile_a)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = create_app(
            lambda: FakeService(
                Path(folder.name) / "web.sqlite", FakeModel(), Weather()
            ),
            testing=True,
        )
        self.a = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.a.__enter__()
        self.addCleanup(self.a.__exit__, None, None, None)
        self.a.get("/api/session")
        self.b = self.fresh()
        self.credentials_a = dict(
            username="isolation_user_a", password="fixture-password-A-123"
        )
        self.credentials_b = dict(
            username="isolation_user_b", password="fixture-password-B-456"
        )
        self.profile_a = self.a.post("/api/account", json=self.credentials_a).json()[
            "profile_id"
        ]
        self.profile_b = self.b.post("/api/account", json=self.credentials_b).json()[
            "profile_id"
        ]

    def fresh(self, initialize=True):
        client = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(client.close)
        if initialize:
            client.get("/api/session")
        return client

    def test_two_accounts_keep_separate_passwords_and_personal_records(self):
        self.assertNotEqual(self.profile_a, self.profile_b)
        self.assertNotEqual(
            self.a.cookies.get("tw_browser"), self.b.cookies.get("tw_browser")
        )
        self.assertNotEqual(
            self.a.cookies.get("tw_auth"), self.b.cookies.get("tw_auth")
        )
        with self.app.state.backend.service.store() as store:
            started = datetime.now(timezone.utc) - timedelta(hours=1)
            trip = store.start_trip(
                self.profile_a, "5756", "4859", started, 10.0, "isolation_test"
            )
            store.finish_trip(
                self.profile_a, trip["receipt"], "4859", started + timedelta(minutes=11)
            )
        self.assertEqual(self.a.get("/api/rides").json()["count"], 1)
        self.assertEqual(self.b.get("/api/rides").json()["count"], 0)
        self.assertEqual(
            self.a.get("/api/session").json()["profile_id"], self.profile_a
        )
        self.assertEqual(
            self.b.get("/api/session").json()["profile_id"], self.profile_b
        )
        outsider = self.fresh()
        for username, password in (
            (self.credentials_a["username"], self.credentials_b["password"]),
            (self.credentials_b["username"], self.credentials_a["password"]),
        ):
            self.assertEqual(
                outsider.post(
                    "/api/session", json=dict(username=username, password=password)
                ).status_code,
                400,
            )

    def test_shared_url_and_query_identifiers_do_not_log_in_another_browser(self):
        outsider = self.fresh()
        query = dict(profile_id=self.profile_a, username=self.credentials_a["username"])
        page = outsider.get("/", params=query)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(self.credentials_a["username"], page.text)
        self.assertIsNone(
            outsider.get("/api/session", params=query).json()["profile_id"]
        )
        self.assertEqual(outsider.get("/api/rides", params=query).status_code, 401)
        self.assertEqual(
            self.a.get("/api/session").json()["profile_id"], self.profile_a
        )

    def test_named_profile_cookie_without_login_proof_is_rejected(self):
        outsider = self.fresh(False)
        outsider.cookies.set("tw_profile", self.profile_a)
        self.assertIsNone(outsider.get("/api/session").json()["profile_id"])
        self.assertEqual(outsider.get("/api/rides").status_code, 401)

    def test_unnamed_profile_cookie_without_login_proof_is_also_rejected(self):
        profile = self.app.state.backend.service.create_profile()["profile_id"]
        outsider = self.fresh(False)
        outsider.cookies.set("tw_profile", profile)
        self.assertIsNone(outsider.get("/api/session").json()["profile_id"])
        self.assertEqual(outsider.get("/api/rides").status_code, 401)
