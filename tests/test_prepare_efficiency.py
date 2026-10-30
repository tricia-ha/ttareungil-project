"""Result equivalence, data-version invalidation and exact stock cutoff."""

import sys, json, tempfile, unittest, hashlib, csv
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import Network, Config, Router, V521BikeProvider
from multimodal_cache import CachedBikeProvider
from inventory_service import InventoryPredictor
import transit_schedule as schedule

START = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)


def at(minutes):
    return (START + timedelta(minutes=minutes)).isoformat()


class PrepareEfficiencyTests(unittest.TestCase):
    def test_confirmed_dead_ends_skip_eta_but_keep_the_fastest_route(self):
        n = Network(
            nodes={k: {} for k in ("O", "A", "B", "Z")},
            walks=[
                {"from": "O", "to": "A", "minutes": 0.1},
                {"from": "O", "to": "Z", "minutes": 10},
            ],
            bikes=[{"from": "A", "to": "B"}],
        )
        counts = []
        for prune in (False, True):
            calls = []

            def eta(e, t):
                calls.append(t)
                return {"minutes": 1.0}

            r = Router(
                n, eta, Config(top_k=1, horizon_minutes=12, prune_known_dead_ends=prune)
            ).search("O", "Z", START)
            self.assertEqual(r["routes"][0]["total_minutes"], 10.0)
            counts.append(len(calls))
        self.assertGreater(counts[0], 0)
        self.assertEqual(counts[1], 0)

    def test_unknown_lazy_exit_is_not_discarded(self):
        n = Network(
            nodes={
                "origin": dict(latitude=37.5, longitude=127.0, kind="endpoint"),
                "A": dict(
                    latitude=37.501, longitude=127.0, kind="bike", station_id="1"
                ),
                "B": dict(latitude=37.51, longitude=127.0, kind="bike", station_id="2"),
                "destination": dict(latitude=37.511, longitude=127.0, kind="endpoint"),
            },
            walks=[{"from": "origin", "to": "A", "minutes": 1.0}],
            bikes=[{"from": "A", "to": "B"}],
            walk_policy={"max_distance_m": 200},
        )

        def walking(a, b, start, end):
            return (
                {"from": start, "to": end, "minutes": 1.0}
                if (start, end) == ("B", "destination")
                else None
            )

        r = Router(
            n,
            lambda *_: {"minutes": 2.0},
            Config(top_k=1, horizon_minutes=10),
            walking_provider=walking,
        ).search("origin", "destination", START)
        self.assertEqual(r["routes"][0]["total_minutes"], 4.0)

    def test_permanently_unroutable_ods_do_not_generate_future_waits(self):
        class NoRoute(RuntimeError):
            permanent_route_failure = True

        def eta(*args):
            raise NoRoute("No connected road")

        n = Network(nodes={"O": {}, "Z": {}}, bikes=[{"from": "O", "to": "Z"}])
        counts = []
        for skip in (False, True):
            r = Router(
                n, eta, Config(top_k=1, horizon_minutes=180, skip_unroutable_waits=skip)
            ).search("O", "Z", START)
            self.assertEqual(r["routes"], [])
            counts.append(r["expanded_states"])
        self.assertLess(counts[1], counts[0])

    def test_stock_cutoff_stops_only_unsupported_waits(self):
        class Inventory:
            def predict(self, **kw):
                return {"p_empty_at_arrival": 0.9}

        def run(cutoff):
            provider = V521BikeProvider(
                None,
                Inventory(),
                {"S": {"snapshot_at": at(0), "current_bikes": 0}},
                at(0),
            )
            n = Network(
                nodes={"O": {}, "Z": {}},
                bikes=[{"from": "O", "to": "Z", "inventory_from": "S"}],
            )
            result = Router(
                n,
                provider,
                Config(
                    top_k=1,
                    horizon_minutes=180,
                    required_mode="bicycle",
                    require_known_inventory=True,
                    max_empty_probability=0.1,
                    latest_bicycle_departure=at(60) if cutoff else None,
                ),
            ).search("O", "Z", START)
            return result, provider

        before, old = run(False)
        after, new = run(True)
        self.assertEqual(before["routes"], after["routes"])
        self.assertEqual(new.inventory_status_counts.get("outside_60min_horizon", 0), 0)
        self.assertEqual(new.inventory_status_counts["uncalibrated"], 61)
        # Minute 180 is the journey horizon, so no new movement is evaluated.
        self.assertEqual(old.inventory_status_counts["outside_60min_horizon"], 119)
        self.assertLess(after["expanded_states"], before["expanded_states"])

    def test_last_valid_rental_can_finish_after_the_stock_deadline(self):
        n = Network(nodes={"O": {}, "Z": {}}, bikes=[{"from": "O", "to": "Z"}])

        class Provider:
            def precheck_inventory(self, e, t):
                return {"p_empty": 0.0 if t == at(60) else 0.9}

            def __call__(self, e, t):
                return {"minutes": 20.0, "p_empty": 0.0}

        r = Router(
            n,
            Provider(),
            Config(
                top_k=1,
                horizon_minutes=120,
                require_known_inventory=True,
                max_empty_probability=0.1,
                latest_bicycle_departure=at(60),
            ),
        ).search("O", "Z", START)
        self.assertEqual(r["routes"][0]["total_minutes"], 80.0)

    def test_transit_after_stock_deadline_is_unchanged(self):
        n = Network(
            nodes={"O": {}, "Z": {}},
            transit=[
                dict(
                    mode="subway",
                    trip_id="late",
                    **{"from": "O", "to": "Z"},
                    departure=at(90),
                    arrival=at(100),
                )
            ],
        )
        r = Router(
            n,
            None,
            Config(
                top_k=1, require_known_inventory=True, latest_bicycle_departure=at(60)
            ),
        ).search("O", "Z", START)
        self.assertEqual(r["routes"][0]["total_minutes"], 100.0)

    def test_batch_checkpoint_is_single_commit_and_keeps_forecast_failure_policy(self):
        class Provider:
            def predict_eta_batch(self, edges, t):
                return [
                    dict(
                        minutes=1.0,
                        weather_error="missing" if e["to"] == "bad" else None,
                    )
                    for e in edges
                ]

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cache.sqlite"
            cache = CachedBikeProvider(Provider(), p, {})
            statements = []
            cache.db.set_trace_callback(statements.append)
            edges = [{"from": "O", "to": str(i)} for i in range(16)] + [
                {"from": "O", "to": "bad"}
            ]
            self.assertEqual(cache.prefetch(edges, at(0)), 17)
            self.assertEqual(sum(x == "COMMIT" for x in statements), 1)
            self.assertEqual(
                cache.db.execute("SELECT count(*) FROM predictions").fetchone()[0], 16
            )
            self.assertFalse(cache.needs_prediction(edges[-1], at(0)))
            cache.close()
            reopened = CachedBikeProvider(Provider(), p, {})
            try:
                self.assertFalse(reopened.needs_prediction(edges[0], at(0)))
                self.assertTrue(reopened.needs_prediction(edges[-1], at(0)))
            finally:
                reopened.close()

    def model_files(self, folder):
        rates = folder / "weekly_rates.csv"
        with rates.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "station_id",
                    "dow",
                    "hour",
                    "rentals_per_hour",
                    "returns_per_hour",
                ],
            )
            writer.writeheader()
            writer.writerows(
                dict(
                    station_id="S",
                    dow=d,
                    hour=h,
                    rentals_per_hour=1,
                    returns_per_hour=1,
                )
                for d in range(7)
                for h in range(24)
            )
        (folder / "model_metadata.json").write_text(
            json.dumps(
                {
                    "weekly_rates_sha256": hashlib.sha256(
                        rates.read_bytes()
                    ).hexdigest(),
                    "production_training_end": "2026-09-01",
                    "scope_stations": 1,
                }
            ),
            encoding="utf-8",
        )
        (folder / "station_activity.csv").write_text(
            "station_id,full_flow_total\nS,1\n", encoding="utf-8"
        )
        return rates

    def test_inventory_table_reused_and_invalidated_on_source_change(self):
        with tempfile.TemporaryDirectory() as d:
            rates = self.model_files(Path(d))
            a = InventoryPredictor(rates)
            b = InventoryPredictor(rates)
            self.assertIs(a.rates, b.rates)
            args = dict(station_id="S", current_bikes=3, eta_minutes=10, as_of=at(0))
            self.assertEqual(a.predict(**args), b.predict(**args))
            with self.assertRaises(TypeError):
                b.rates[("S", 0, 0)] = (100, 100)
            b.metadata["scope_stations"] = 5
            self.assertEqual(InventoryPredictor(rates).metadata["scope_stations"], 1)
            rates.write_text(
                rates.read_text(encoding="utf-8").replace("S,0,0,1,1", "S,0,0,2,1"),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                InventoryPredictor(rates)

    def test_rail_groups_cached_but_dates_windows_and_files_rechecked(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)

            def payload(last="10:05:00"):
                return {
                    "valid_from": "2026-09-01",
                    "valid_until": None,
                    "day_types": ["weekday", "saturday", "holiday"],
                    "subway_stops": {
                        "A": {"latitude": 37.5, "longitude": 127.0},
                        "B": {"latitude": 37.51, "longitude": 127.0},
                    },
                    "subway_rows": [
                        dict(
                            LINE_NUM="1호선",
                            TRAIN_NO="T",
                            WEEK_TAG="1",
                            STATION_CD=s,
                            ARRIVETIME=t,
                            LEFTTIME=t,
                        )
                        for s, t in [("A", "10:00:00"), ("B", last)]
                    ],
                }

            p = folder / "prepared.json"
            p.write_text(json.dumps(payload()), encoding="utf-8")
            departure = "2026-10-27T09:59:00+09:00"
            with patch.object(
                schedule,
                "reconstruct_bus",
                side_effect=AssertionError("no bus preparation"),
            ):
                a = schedule.build_timetable(folder, departure, 30, include_bus=False)
                with patch.object(
                    schedule,
                    "_group_subway_rows",
                    side_effect=AssertionError("must reuse parsed groups"),
                ):
                    b = schedule.build_timetable(
                        folder, departure, 30, include_bus=False
                    )
                    later = schedule.build_timetable(
                        folder, "2026-10-27T10:01:00+09:00", 30, include_bus=False
                    )
                self.assertEqual(a.transit, b.transit)
                self.assertEqual(later.transit, [])
                p.write_text(json.dumps(payload("10:06:00")), encoding="utf-8")
                changed = schedule.build_timetable(
                    folder, departure, 30, include_bus=False
                )
                self.assertNotEqual(a.transit, changed.transit)
                weekend = schedule.build_timetable(
                    folder, "2026-10-31T09:59:00+09:00", 30, include_bus=False
                )
                self.assertEqual(weekend.transit, [])


if __name__ == "__main__":
    unittest.main()
