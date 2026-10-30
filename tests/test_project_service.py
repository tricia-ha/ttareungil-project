from pathlib import Path
import copy, json, os, sys, tempfile, unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from personal_affine import personalized_minutes
from project_service import ProjectService
from pydantic import ValidationError
from open_meteo_history import HistoricalWeatherUnavailable


class Model:
    def __init__(self):
        self.calls = []

    def predict(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"eta": {"p50_min": 10.0}}


class Weather:
    def for_route(self, *args):
        return {"weather": None, "source": "test_observation"}


class ProjectServiceTests(unittest.TestCase):
    def test_public_service_methods_persist_profile(self):
        profile = self.service.create_profile()["profile_id"]
        self.assertEqual(self.service.get_profile(profile)["factor"], 1.0)
        self.assertEqual(self.service.list_rides(profile)["count"], 0)
        self.service.delete_profile(profile)
        with self.assertRaises(ValueError):
            self.service.get_profile(profile)

    def test_public_transit_method_passes_bus_and_subway_mode_without_models(self):
        with (
            patch.dict(os.environ, {"KAKAO_REST_API_KEY": "test"}),
            patch(
                "scheduled_runtime.run_scheduled_optimization",
                return_value={"routes": []},
            ) as run,
            patch.object(self.service, "model", side_effect=AssertionError("no model")),
        ):
            result = self.service.find_routes(
                {"latitude": 37.5, "longitude": 127},
                {"latitude": 37.6, "longitude": 127},
                mode="transit",
            )
        self.assertEqual(result, {"routes": []})
        self.assertEqual(run.call_args.kwargs["config"].search_mode, "transit")
        self.assertFalse(run.call_args.kwargs["use_inventory"])
        self.assertFalse(run.call_args.kwargs["use_kma"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.image = self.path / "image.png"
        self.image.write_bytes(b"fixture")
        self.model = Model()
        self.service = ProjectService(
            self.path / "history.sqlite", self.model, Weather()
        )
        self.id = self.service._execute({"action": "profile.create"})["profile_id"]
        self.sample = json.loads(
            (ROOT / "tests/fixtures/screenshot_ocr/original_2.json").read_text(
                encoding="utf-8"
            )
        )

    def preview(self, sample=None, profile=None):
        sample = sample or self.sample
        with patch(
            "project_service.recognize", return_value=(sample["tokens"], sample["size"])
        ):
            return self.service._execute(
                {
                    "action": "screenshot.preview",
                    "profile_id": profile or self.id,
                    "image_path": str(self.image),
                }
            )

    def confirm(self, preview, **extra):
        q = dict(
            action="screenshot.confirm",
            profile_id=self.id,
            draft_id=preview["draft_id"],
            card_index=0,
            confirmed=True,
        )
        q.update(extra)
        return self.service._execute(q)

    def test_confirm_personalizes_and_deduplicates_across_previews(self):
        before = self.service._execute(dict(action="profile.get", profile_id=self.id))
        self.assertEqual(before["factor"], 1.0)
        self.confirm(self.preview())
        self.confirm(self.preview())
        records = self.service._execute(dict(action="rides.list", profile_id=self.id))
        after = self.service._execute(dict(action="profile.get", profile_id=self.id))
        self.assertEqual(records["count"], 1)
        self.assertGreater(personalized_minutes(10, after), 10.0)
        self.assertEqual(self.model.calls[0][1]["user_type"], "unknown")

    def test_unknown_owner_and_cross_owner_draft_rejected(self):
        preview = self.preview()
        other = self.service._execute({"action": "profile.create"})["profile_id"]
        with self.assertRaises(ValueError):
            self.confirm(preview, profile_id=other)
        self.assertEqual(
            self.service._execute(dict(action="rides.list", profile_id=other))["count"],
            0,
        )
        with self.assertRaises(ValueError):
            self.service._execute(dict(action="profile.get", profile_id="invalid"))

    def test_explicit_confirmation_and_server_owned_draft_required(self):
        preview = self.preview()
        for update in [
            dict(confirmed=False),
            dict(draft_id="x" * 32),
            dict(card_index=100),
        ]:
            with self.assertRaises(ValueError):
                self.confirm(preview, **update)
        with self.assertRaises(ValidationError):
            self.confirm(preview, confirmed="true")
        with self.assertRaises(ValidationError):
            self.confirm(preview, base_p50_min=1.0)
        self.assertFalse(self.model.calls)

    def test_expired_draft_rejected_before_weather_or_model(self):
        preview = self.preview()
        with self.service.store() as store:
            with store.db:
                store.db.execute("UPDATE screenshot_drafts SET expires=0")
        with self.assertRaises(ValueError):
            self.confirm(preview)
        self.assertFalse(self.model.calls)

    def test_incomplete_ocr_can_be_manually_completed_and_registered(self):
        from screenshot_history import extract_cards, catalogs

        original = extract_cards(self.sample["tokens"], self.sample["size"])["cards"][
            0
        ]["record"]
        sample = copy.deepcopy(self.sample)
        sample["tokens"] = [t for t in sample["tokens"] if t["text"] != "03.34"]
        preview = self.preview(sample)
        self.assertFalse(preview["cards"][0]["reviewable"])
        self.assertTrue(preview["cards"][0]["manual_editable"])
        self.assertIn("draft_id", preview)
        with self.assertRaises(ValueError):
            self.confirm(preview)
        with self.assertRaisesRegex(ValueError, "입력해 주세요"):
            self.confirm(preview, manual=True)
        stations, _ = catalogs()
        edits = {k: v for k, v in original.items() if k != "bike_type"}
        edits.update(
            start_station_name=stations[edits["start_station_id"]]["station_name"],
            end_station_name=stations[edits["end_station_id"]]["station_name"],
        )
        with self.assertRaises(ValueError):
            self.confirm(
                preview, manual=True, edits=dict(edits, start_station_id="999999")
            )
        with self.assertRaises(ValueError):
            self.confirm(preview, manual=True, edits=dict(edits, actual_minutes=999.0))
        self.assertFalse(self.model.calls)
        result = self.confirm(preview, manual=True, edits=edits)
        self.assertTrue(result["used_for_update"])
        self.assertEqual(self.service.list_rides(self.id)["count"], 1)
        self.assertTrue(self.confirm(preview, manual=True, edits=edits)["duplicate"])

    def test_unrelated_image_does_not_enable_manual_registration(self):
        sample = dict(
            tokens=[
                dict(
                    text="일반 사진",
                    confidence=0.99,
                    box=[[0, 0], [100, 0], [100, 20], [0, 20]],
                )
            ],
            size=[200, 200],
        )
        result = self.preview(sample)
        self.assertEqual(result["manual_reviewable_count"], 0)
        self.assertNotIn("draft_id", result)

    def test_station_id_name_mismatch_held_and_explicit_correction_allowed(self):
        p = self.preview()
        for bad in ["1278", "999999"]:
            with self.assertRaises(ValueError):
                self.confirm(p, edits={"start_station_id": bad})
        self.assertFalse(self.model.calls)
        self.confirm(
            p, edits={"start_station_id": "5756", "start_station_name": "극동아파트"}
        )
        self.assertEqual(len(self.model.calls), 1)

    def test_unknown_ocr_number_kept_for_review_without_silent_reassignment(self):
        s = copy.deepcopy(self.sample)
        for t in s["tokens"]:
            if t["text"] == "05756":
                t["text"] = "99999"
        p = self.preview(s)
        self.assertEqual(p["eligible_count"], 0)
        self.assertEqual(p["reviewable_count"], 1)
        with self.assertRaises(ValueError):
            self.confirm(p)
        result = self.confirm(p, edits={"start_station_id": "5756"})
        self.assertEqual(result["record"]["start_station_id"], "5756")

    def test_unrelated_text_has_no_draft(self):
        p = self.preview({"tokens": [], "size": [1200, 2600]})
        self.assertNotIn("draft_id", p)
        self.assertEqual(p["reviewable_count"], 0)

    def test_invalid_crop_and_invalid_images_cannot_register(self):
        from PIL import Image

        Image.new("RGB", (50, 50), "white").save(self.image)
        for crop in [[0, 0, 100, 100], [20, 20, 1, 1]]:
            with self.assertRaises(ValueError):
                self.service._execute(
                    dict(
                        action="screenshot.preview",
                        profile_id=self.id,
                        image_path=str(self.image),
                        crop=crop,
                    )
                )
        self.image.write_bytes(b"invalid image")
        with self.assertRaises(ValueError):
            self.service._execute(
                dict(
                    action="screenshot.preview",
                    profile_id=self.id,
                    image_path=str(self.image),
                )
            )

    def test_missing_weather_blocks_unless_explicitly_allowed(self):
        p = self.preview()
        with patch.object(
            self.service.weather_provider,
            "for_route",
            side_effect=HistoricalWeatherUnavailable("missing"),
        ):
            with self.assertRaises(HistoricalWeatherUnavailable):
                self.confirm(p)
            self.assertFalse(self.model.calls)
            r = self.confirm(p, allow_missing_weather=True)
            self.assertEqual(r["weather_source"], "historical_observation_unavailable")

    def test_delete_observation_resets_factor_and_profile_cascades(self):
        self.confirm(self.preview())
        receipt = self.service._execute(dict(action="rides.list", profile_id=self.id))[
            "records"
        ][0]["receipt"]
        result = self.service._execute(
            dict(action="rides.delete", profile_id=self.id, receipt=receipt)
        )
        self.assertEqual(result["profile"]["factor"], 1.0)
        self.service._execute(dict(action="profile.delete", profile_id=self.id))
        with self.assertRaises(ValueError):
            self.service._execute(dict(action="profile.get", profile_id=self.id))

    def test_route_forwards_same_candidate_search_and_personalization_database(self):
        q = dict(
            action="route",
            origin={"latitude": 37.5568, "longitude": 126.9723},
            destination={"latitude": 37.5284, "longitude": 126.9326},
            departure="auto",
        )
        q["profile_id"] = self.id
        with (
            patch.dict(
                os.environ, {"KAKAO_REST_API_KEY": "test", "KMA_API_KEY": "test"}
            ),
            patch(
                "scheduled_runtime.run_scheduled_optimization",
                return_value={"routes": []},
            ) as run,
        ):
            self.assertEqual(self.service._execute(q), {"routes": []})
        opts = run.call_args.kwargs
        self.assertIsNone(run.call_args.args[2])
        self.assertTrue(opts["config"].complete_candidates)
        self.assertEqual(opts["config"].bike_quantile, "p50")
        self.assertEqual(opts["personalization_id"], self.id)
        self.assertEqual(opts["personalization_path"], self.service.db_path)
        self.assertIs(opts["eta_service"]._model, self.model)

    def test_subway_skips_eta_weather_inventory(self):
        q = dict(
            action="route",
            origin={"latitude": 37.5568, "longitude": 126.9723},
            destination={"latitude": 37.5284, "longitude": 126.9326},
            departure="auto",
        )
        q["search_mode"] = "subway"
        with (
            patch.dict(os.environ, {"KAKAO_REST_API_KEY": "test"}),
            patch(
                "scheduled_runtime.run_scheduled_optimization", return_value={}
            ) as run,
            patch.object(
                self.service, "model", side_effect=AssertionError("unneeded model")
            ),
        ):
            self.service._execute(q)
        self.assertFalse(run.call_args.kwargs["use_kma"])
        self.assertFalse(run.call_args.kwargs["use_inventory"])
        self.assertIsNone(run.call_args.kwargs["eta_service"])

    def test_invalid_and_past_queries_rejected(self):
        for q in [
            [],
            {"action": "unknown"},
            {"action": "profile.create", "unexpected": True},
        ]:
            with self.assertRaises(ValueError):
                self.service._execute(q)
        q = dict(
            action="route",
            origin={"latitude": 37.5568, "longitude": 126.9723},
            destination={"latitude": 37.5284, "longitude": 126.9326},
            departure="auto",
        )
        q["departure"] = "2000-01-01T00:00:00+09:00"
        with self.assertRaises(ValueError):
            self.service._execute(q)


if __name__ == "__main__":
    unittest.main()
