from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from contextlib import closing
import copy, json, os, sys, tempfile, time, unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from fastapi.testclient import TestClient
from personal_affine import personalized_minutes
from web_server import create_app
from project_service import ProjectService


class FakeModel:
    def predict(self, *args, **kwargs):
        return {"eta": {"p50_min": 10.0}}


class Weather:
    def for_route(self, *args):
        return {"weather": None, "source": "test"}


class FakeService(ProjectService):
    calls = []

    def find_routes(self, origin, destination, **options):
        if origin.get("name") == "FAIL":
            raise ValueError(
                "secret-test-key https://api.example/?authKey=secret-test-key"
            )
        if origin.get("name") == "SLOW":
            time.sleep(0.25)
        self.calls.append(options)
        departure = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        arrival = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        mode = "bus" if options["mode"] == "transit" else "bicycle"
        personal = (
            self.get_profile(options["profile_id"])
            if options.get("profile_id")
            else {"enabled": False, "factor": 1}
        )
        return dict(
            status="ok",
            routes=[
                dict(
                    total_minutes=10,
                    arrival=arrival,
                    route_kind="transit",
                    legs=[
                        dict(
                            mode=mode,
                            **{"from": "origin", "to": "destination"},
                            minutes=10,
                            start=departure,
                            arrival=arrival,
                            geometry={
                                "type": "LineString",
                                "coordinates": [
                                    [126.9723, 37.5568],
                                    [126.9326, 37.5284],
                                ],
                            },
                        )
                    ],
                )
            ],
            mixed_routes=[],
            nodes={"origin": origin, "destination": destination, "unused": {}},
            departure=departure,
            search_mode=options["mode"],
            personalization=personal,
            api_usage={"new_api_calls": 0},
            search_complete=True,
        )


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = Path(self.temp.name) / "test.sqlite"
        FakeService.calls = []
        self.app = create_app(
            lambda: FakeService(db, FakeModel(), Weather()), testing=True
        )
        self.client = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.get("/api/session")

    def wait(self, client, response):
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(200):
            result = client.get("/api/jobs/" + response.json()["job_id"])
            self.assertEqual(result.status_code, 200, result.text)
            if result.json()["status"] in ("done", "error"):
                return result.json()
            time.sleep(0.01)
        self.fail("job did not complete")

    def query(self, name="origin", mode="integrated"):
        return dict(
            origin={"name": name, "latitude": 37.5568, "longitude": 126.9723},
            destination={
                "name": "destination",
                "latitude": 37.5284,
                "longitude": 126.9326,
            },
            mode=mode,
        )

    def test_ui_static_files_and_no_private_file_serving(self):
        for path in ["/", "/app.js", "/app.css", "/base.css", "/assets/logo.png"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Content-Security-Policy", response.headers)
        for path in [
            "/src/project_service.py",
            "/.env",
            "/models/eta_v5_2_1/best.pt",
            "/docs",
            "/openapi.json",
        ]:
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_cross_origin_and_bad_host_rejected(self):
        self.assertEqual(
            self.client.post(
                "/api/account", headers={"Origin": "https://attacker.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/api/places?q=서울", headers={"Sec-Fetch-Site": "cross-site"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/", headers={"Host": "attacker.example"}).status_code, 400
        )

    def test_anonymous_route_and_job_ownership(self):
        response = self.client.post("/api/routes", json=self.query(mode="transit"))
        done = self.wait(self.client, response)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["result"]["routes"][0]["legs"][0]["mode"], "bus")
        self.assertNotIn("unused", done["result"]["nodes"])
        other = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(other.close)
        self.assertEqual(
            other.get("/api/jobs/" + response.json()["job_id"]).status_code, 404
        )

    def test_id_works_across_browsers_but_not_without_restore(self):
        profile = self.client.post(
            "/api/account",
            json={"username": "테스트아이디", "password": "test-password-123"},
        ).json()["profile_id"]
        self.assertEqual(self.client.get("/api/rides").json()["count"], 0)
        other = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(other.close)
        self.assertEqual(other.get("/api/rides").status_code, 401)
        self.assertEqual(
            other.post(
                "/api/session",
                json={"username": "테스트아이디", "password": "test-password-123"},
            ).status_code,
            200,
        )
        self.assertEqual(other.get("/api/session").json()["profile_id"], profile)
        self.wait(other, other.post("/api/routes", json=self.query()))
        self.assertEqual(FakeService.calls[-1]["profile_id"], profile)
        self.client.delete("/api/account")
        self.assertIsNone(other.get("/api/session").json()["profile_id"])

    def test_pending_job_blocks_duplicate_work(self):
        response = self.client.post("/api/routes", json=self.query("SLOW"))
        self.assertEqual(
            self.client.post("/api/routes", json=self.query()).status_code, 409
        )
        self.assertEqual(
            self.client.post(
                "/api/account",
                json={"username": "테스트아이디", "password": "test-password-123"},
            ).status_code,
            409,
        )
        self.wait(self.client, response)

    def test_errors_do_not_expose_credentials(self):
        with patch.dict(os.environ, {"KMA_API_KEY": "secret-test-key"}):
            done = self.wait(
                self.client, self.client.post("/api/routes", json=self.query("FAIL"))
            )
        self.assertEqual(done["status"], "error")
        self.assertNotIn("secret-test-key", done["error"])
        self.assertNotIn("https://", done["error"])

    def test_invalid_and_expensive_inputs_rejected_before_queue(self):
        for change in [
            dict(mode="invalid"),
            dict(max_seconds=10000),
            dict(profile_id="forged"),
            dict(use_inventory="yes"),
        ]:
            value = self.query()
            value.update(change)
            self.assertIn(
                self.client.post("/api/routes", json=value).status_code, (400, 422)
            )
        self.assertEqual(
            self.client.post("/api/screenshots", content=b"x").status_code, 401
        )

    def test_place_search_results_can_be_used_as_route_points(self):
        from unittest.mock import Mock

        response = Mock(status_code=200)
        response.json.return_value = {
            "documents": [
                dict(
                    place_name="롯데월드 어드벤처",
                    x="127.098",
                    y="37.511",
                    road_address_name="서울 송파구 올림픽로 240",
                )
            ]
        }
        with (
            patch.dict(os.environ, {"KAKAO_REST_API_KEY": "test"}),
            patch("web_server.requests.get", return_value=response),
        ):
            places = self.client.get("/api/places?q=롯데월드 어드벤처").json()
        self.assertIn("address", places[0])
        query = self.query(mode="transit")
        query["origin"] = places[0]
        query["destination"]["address"] = "서울 마포구 망원시장"
        result = self.wait(self.client, self.client.post("/api/routes", json=query))
        self.assertEqual(result["status"], "done")
        origin = result["result"]["nodes"]["origin"]
        self.assertNotIn("address", origin)
        self.assertEqual(origin["name"], "롯데월드 어드벤처")
        from project_service import Point

        Point.model_validate(origin)

    def test_validation_message_identifies_coordinate_field(self):
        query = self.query()
        query["origin"]["latitude"] = "invalid"
        response = self.client.post("/api/routes", json=query)
        self.assertEqual(response.status_code, 422)
        self.assertIn("출발지 위도", response.json()["detail"])
        self.assertEqual(self.client.post("/api/routes", json=[]).status_code, 422)

    def test_photo_memory_preview_confirm_dedupe_and_personalization(self):
        profile = self.client.post(
            "/api/account",
            json={"username": "테스트아이디", "password": "test-password-123"},
        ).json()["profile_id"]
        fixture = json.loads(
            (ROOT / "tests/fixtures/screenshot_ocr/original_2.json").read_text(
                encoding="utf-8"
            )
        )
        with patch(
            "project_service.recognize",
            return_value=(fixture["tokens"], fixture["size"]),
        ):
            done = self.wait(
                self.client,
                self.client.post(
                    "/api/screenshots",
                    content=b"fixture",
                    headers={"Content-Type": "image/png"},
                ),
            )
        self.assertEqual(done["status"], "done")
        draft = done["result"]
        self.assertEqual(draft["eligible_count"], 1)
        q = dict(draft_id=draft["draft_id"], card_index=0, confirmed=True, edits={})
        bad = copy.deepcopy(q)
        bad["edits"] = {"start_station_id": "1278"}
        self.assertEqual(
            self.wait(
                self.client, self.client.post("/api/screenshots/confirm", json=bad)
            )["status"],
            "error",
        )
        self.assertEqual(
            self.wait(
                self.client, self.client.post("/api/screenshots/confirm", json=q)
            )["status"],
            "done",
        )
        self.assertTrue(
            self.wait(
                self.client, self.client.post("/api/screenshots/confirm", json=q)
            )["result"]["duplicate"]
        )
        self.assertEqual(self.client.get("/api/rides").json()["count"], 1)
        self.assertGreater(
            personalized_minutes(10, self.client.get("/api/session").json()["profile"]),
            10.0,
        )
        receipt = self.client.get("/api/rides").json()["records"][0]["receipt"]
        self.assertEqual(self.client.delete("/api/rides/" + receipt).status_code, 200)
        self.assertEqual(
            self.client.get("/api/session").json()["profile"]["factor"], 1.0
        )

    def test_manual_completion_is_accepted_through_web(self):
        from screenshot_history import extract_cards, catalogs

        self.client.post(
            "/api/account",
            json={"username": "직접수정", "password": "test-password-123"},
        )
        fixture = json.loads(
            (ROOT / "tests/fixtures/screenshot_ocr/original_2.json").read_text(
                encoding="utf-8"
            )
        )
        record = extract_cards(fixture["tokens"], fixture["size"])["cards"][0]["record"]
        fixture["tokens"] = [t for t in fixture["tokens"] if t["text"] != "03.34"]
        with patch(
            "project_service.recognize",
            return_value=(fixture["tokens"], fixture["size"]),
        ):
            preview = self.wait(
                self.client,
                self.client.post(
                    "/api/screenshots",
                    content=b"fixture",
                    headers={"Content-Type": "image/png"},
                ),
            )["result"]
        self.assertTrue(preview["cards"][0]["manual_editable"])
        stations, _ = catalogs()
        edits = {k: v for k, v in record.items() if k != "bike_type"}
        edits.update(
            start_station_name=stations[record["start_station_id"]]["station_name"],
            end_station_name=stations[record["end_station_id"]]["station_name"],
        )
        result = self.wait(
            self.client,
            self.client.post(
                "/api/screenshots/confirm",
                json=dict(
                    draft_id=preview["draft_id"],
                    card_index=0,
                    confirmed=True,
                    manual=True,
                    edits=edits,
                ),
            ),
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.client.get("/api/rides").json()["count"], 1)

    def test_local_station_search_makes_no_external_calls(self):
        with patch("requests.get", side_effect=AssertionError("no API")):
            response = self.client.get("/api/places?q=5756")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json())

    def test_https_session_cookie_is_secure_and_httponly(self):
        https = TestClient(
            self.app,
            base_url="https://testserver",
            headers={"Origin": "https://testserver"},
        )
        self.addCleanup(https.close)
        response = https.get("/api/session")
        cookie = response.headers.get("set-cookie", "").lower()
        self.assertIn("secure", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        response = https.post(
            "/api/account",
            json={"username": "테스트아이디", "password": "test-password-123"},
        )
        cookie = response.headers.get("set-cookie", "").lower()
        self.assertIn("tw_profile=", cookie)
        self.assertIn("secure", cookie)

    def test_wrong_image_is_rejected_without_saving_a_ride(self):
        self.client.post(
            "/api/account",
            json={"username": "테스트아이디", "password": "test-password-123"},
        )
        response = self.client.post(
            "/api/screenshots",
            content=b"not an image",
            headers={"Content-Type": "image/png"},
        )
        self.assertEqual(self.wait(self.client, response)["status"], "error")
        self.assertEqual(self.client.get("/api/rides").json()["count"], 0)
        self.assertEqual(
            self.client.post(
                "/api/screenshots",
                content=b"pdf",
                headers={"Content-Type": "application/pdf"},
            ).status_code,
            415,
        )
        self.assertEqual(
            self.client.post(
                "/api/screenshots",
                content=b"x",
                headers={
                    "Content-Type": "image/png",
                    "Content-Length": str(13 * 1024 * 1024),
                },
            ).status_code,
            413,
        )

    def test_default_web_database_does_not_restore_legacy_profiles(self):
        import sqlite3
        from contextlib import closing
        from personalization import PersonalizationStore

        folder = Path(self.temp.name) / "separate"
        folder.mkdir()
        with PersonalizationStore(folder / "personalization.sqlite") as store:
            legacy = store.create_profile()
        with patch("web_server.RUNTIME_CACHE_DIR", folder):
            app = create_app(testing=True)
            with TestClient(app, headers={"Origin": "http://testserver"}) as web:
                self.assertEqual(
                    web.post("/api/session", json={"profile_id": legacy}).status_code,
                    422,
                )
                self.assertEqual(
                    web.post(
                        "/api/account",
                        json={"username": "새계정", "password": "test-password-123"},
                    ).status_code,
                    200,
                )
        for name in ("personalization.sqlite", "web_personalization.sqlite"):
            with closing(sqlite3.connect(folder / name)) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM personal_profiles").fetchone()[0],
                    1,
                )

    def test_chosen_username_duplicate_and_wrong_password(self):
        credentials = {"username": "BicycleUser", "password": "test-password-123"}
        response = self.client.post("/api/account", json=credentials)
        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile_id"]
        self.assertEqual(
            self.client.get("/api/session").json()["username"], "BicycleUser"
        )
        other = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(other.close)
        duplicate = other.post(
            "/api/account", json=dict(credentials, username="bicycleuser")
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(
            other.post(
                "/api/session", json=dict(credentials, password="wrong-password")
            ).status_code,
            400,
        )
        self.assertEqual(other.get("/api/rides").status_code, 401)
        self.assertEqual(
            other.post(
                "/api/session", json=dict(credentials, username="BICYCLEUSER")
            ).json()["profile_id"],
            profile,
        )
        self.client.delete("/api/account")
        self.assertEqual(other.get("/api/session").json()["profile_id"], None)
        fresh = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.post("/api/session", json=credentials).status_code, 400)

    def test_profile_cookie_cannot_claim_existing_unnamed_history(self):
        profile = self.app.state.backend.service.create_profile()["profile_id"]
        other = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(other.close)
        other.cookies.set("tw_profile", profile)
        before = other.get("/api/session").json()
        self.assertIsNone(before["profile_id"])
        self.assertIsNone(before["username"])
        result = other.post(
            "/api/account",
            json={"username": "기존이력사용자", "password": "test-password-123"},
        )
        self.assertEqual(result.status_code, 200)
        self.assertNotEqual(result.json()["profile_id"], profile)
        self.assertEqual(other.get("/api/session").json()["username"], "기존이력사용자")

    def test_password_is_salted_and_not_stored_as_plaintext(self):
        import sqlite3

        credentials = {"username": "사용자하나", "password": "test-password-123"}
        self.assertEqual(
            self.client.post("/api/account", json=credentials).status_code, 200
        )
        self.client.delete("/api/session")
        self.assertEqual(
            self.client.post(
                "/api/account", json=dict(credentials, username="사용자둘")
            ).status_code,
            200,
        )
        with closing(sqlite3.connect(self.app.state.backend.service.db_path)) as db:
            rows = db.execute("SELECT salt,password_hash FROM web_accounts").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0], rows[1])
        self.assertNotIn(credentials["password"], str(rows))

    def test_password_change_preserves_profile_and_revokes_other_devices(self):
        old = {"username": "암호변경테스트", "password": "old-password-123"}
        profile = self.client.post("/api/account", json=old).json()["profile_id"]
        with self.app.state.backend.service.store() as store:
            started = datetime.now(timezone.utc) - timedelta(hours=1)
            ride = store.start_trip(
                profile, "5756", "4859", started, 10.0, "password_change_test"
            )
            store.finish_trip(
                profile, ride["receipt"], "4859", started + timedelta(minutes=11)
            )
        before = self.client.get("/api/rides").json()
        self.assertEqual(before["count"], 1)
        other = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(other.close)
        other.post("/api/session", json=old)
        stale = dict(other.cookies)
        wrong = self.client.post(
            "/api/account/password",
            json={
                "current_password": "wrong",
                "new_password": "brand-new-password-123",
            },
        )
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(other.get("/api/session").json()["profile_id"], profile)
        changed = self.client.post(
            "/api/account/password",
            json={
                "current_password": old["password"],
                "new_password": "brand-new-password-123",
            },
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get("/api/session").json()["profile_id"], profile)
        self.assertEqual(self.client.get("/api/rides").status_code, 200)
        self.assertEqual(self.client.get("/api/rides").json(), before)
        self.assertIsNone(other.get("/api/session").json()["profile_id"])
        replay = TestClient(self.app, headers={"Origin": "http://testserver"})
        self.addCleanup(replay.close)
        replay.cookies.update(stale)
        self.assertEqual(replay.get("/api/rides").status_code, 401)
        self.assertEqual(other.post("/api/session", json=old).status_code, 400)
        self.assertEqual(
            other.post(
                "/api/session", json=dict(old, password="brand-new-password-123")
            ).json()["profile_id"],
            profile,
        )


if __name__ == "__main__":
    unittest.main()
