"""Prune transit-only detours without banning valid transfers or exact-time waits."""

import sys, unittest, random
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import Network, Router, Config

START = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)


def at(minutes):
    return (START + timedelta(minutes=minutes)).isoformat()


def ride(a, b, dep, arr, trip, **kw):
    return dict(
        {
            "from": a,
            "to": b,
            "departure": at(dep),
            "arrival": at(arr),
            "mode": "subway",
            "trip_id": trip,
        },
        **kw,
    )


def walk(a, b, minutes):
    return {"from": a, "to": b, "minutes": minutes}


def run(network, prune=True, provider=None, origin="O", top_k=5):
    return Router(
        network,
        provider,
        Config(
            top_k=top_k,
            horizon_minutes=12,
            max_seconds=5,
            prune_transit_roundtrips=prune,
        ),
    ).search(origin, "Z", START)


class TransitRoundtripTests(unittest.TestCase):
    def test_out_and_back_is_blocked_before_further_expansion(self):
        n = Network(
            nodes={k: {} for k in ("O", "A", "B", "Z")},
            walks=[walk("A", "Z", 1)],
            transit=[
                ride("O", "A", 0, 2, "in"),
                ride("A", "B", 3, 4, "out"),
                ride("B", "A", 5, 6, "back"),
            ],
        )
        before, after = run(n, False), run(n)
        self.assertEqual(
            before["routes"][0]["total_minutes"], after["routes"][0]["total_minutes"]
        )
        self.assertTrue(
            any(
                "back" in [l.get("trip_id") for l in r["legs"]]
                for r in before["routes"]
            )
        )
        self.assertFalse(
            any(
                "back" in [l.get("trip_id") for l in r["legs"]] for r in after["routes"]
            )
        )
        self.assertGreater(after["pruned_transit_roundtrips"], 0)
        self.assertLess(after["expanded_states"], before["expanded_states"])

    def test_same_station_name_different_platform_transfer_is_kept(self):
        nodes = {
            k: {"name": "공덕" if k.startswith("A") else k}
            for k in ("O", "A1", "A2", "Z")
        }
        n = Network(
            nodes=nodes,
            walks=[walk("A1", "A2", 1)],
            transit=[ride("O", "A1", 0, 2, "airport"), ride("A2", "Z", 4, 5, "line5")],
        )
        a, b = run(n, False), run(n)
        self.assertEqual(a["routes"], b["routes"])
        self.assertEqual(b["routes"][0]["total_minutes"], 5.0)

    def test_first_pass_does_not_allow_alighting(self):
        n = Network(
            nodes={k: {} for k in ("O", "A", "B", "Z")},
            walks=[walk("A", "Z", 1)],
            transit=[
                ride("O", "A", 0, 1, "T", dropoff_allowed=False),
                ride("A", "B", 1, 2, "T", dropoff_allowed=False),
                ride("B", "A", 2, 3, "T"),
            ],
        )
        a, b = run(n, False, top_k=1), run(n, top_k=1)
        self.assertEqual(a["routes"], b["routes"])
        self.assertEqual(b["routes"][0]["total_minutes"], 4.0)

    def test_pickup_restricted_continuation_requires_staying_aboard(self):
        n = Network(
            nodes={k: {} for k in ("O", "A", "B", "Z")},
            walks=[walk("O", "A", 0.1)],
            transit=[
                ride("A", "B", 1, 2, "T"),
                ride("B", "A", 2, 3, "T"),
                ride("A", "Z", 3, 4, "T", pickup_allowed=False),
            ],
        )
        a, b = run(n, False, top_k=1), run(n, top_k=1)
        self.assertEqual(
            a["routes"][0]["total_minutes"], b["routes"][0]["total_minutes"]
        )
        self.assertEqual(b["routes"][0]["total_minutes"], 4.0)
        self.assertEqual(b["routes"][0]["transit_boardings"], 1)

    def test_boarding_buffer_can_make_staying_aboard_essential(self):
        n = Network(
            nodes={k: {} for k in ("A", "B", "Z")},
            transit=[
                ride("A", "B", 0.25, 0.5, "T"),
                ride("B", "A", 0.5, 1, "T"),
                ride("A", "Z", 1.1, 2, "T", boarding_seconds=120),
            ],
        )
        a, b = run(n, False, origin="A", top_k=1), run(n, origin="A", top_k=1)
        self.assertEqual(
            a["routes"][0]["total_minutes"], b["routes"][0]["total_minutes"]
        )
        self.assertEqual(b["routes"][0]["total_minutes"], 2.0)

    def test_wait_replacement_preserves_non_grid_inventory_time(self):
        n = Network(
            nodes={k: {} for k in ("O", "A", "B", "Z")},
            bikes=[{"from": "A", "to": "Z"}],
            transit=[
                ride("O", "A", 0, 0.1, "in"),
                ride("A", "B", 0.25, 0.5, "out"),
                ride("B", "A", 0.5, 0.75, "back"),
            ],
        )

        def provider(edge, departure):
            return {"minutes": 1 if departure == at(0.75) else 100, "p_empty": 0.0}

        a, b = run(n, False, provider, top_k=1), run(n, provider=provider, top_k=1)
        self.assertAlmostEqual(
            a["routes"][0]["total_minutes"], b["routes"][0]["total_minutes"]
        )
        self.assertAlmostEqual(b["routes"][0]["total_minutes"], 1.75)
        self.assertNotIn("back", [l.get("trip_id") for l in b["routes"][0]["legs"]])
        self.assertTrue(
            any(
                l.get("source") == "transit_roundtrip_replaced_by_wait"
                for l in b["routes"][0]["legs"]
            )
        )

    def test_cycle_that_changes_bicycle_action_state_is_not_pruned(self):
        n = Network(
            nodes={k: {} for k in ("O", "A", "B", "Z")},
            bikes=[{"from": "O", "to": "A"}, {"from": "A", "to": "Z"}],
            transit=[ride("A", "B", 2, 3, "out"), ride("B", "A", 4, 5, "back")],
        )
        provider = lambda *_: {"minutes": 1.0, "p_empty": 0.0}
        a, b = run(n, False, provider, top_k=1), run(n, provider=provider, top_k=1)
        self.assertEqual(
            a["routes"][0]["total_minutes"], b["routes"][0]["total_minutes"]
        )
        self.assertEqual(b["routes"][0]["bike_rentals"], 2)

    def test_random_exact_time_bicycle_forecasts_keep_the_best_eta(self):
        rng = random.Random(1028)
        for _ in range(30):
            favorable = rng.choice([0.15, 1.0, 1.6, 2.0, 3.0])
            slow = rng.randint(4, 10)
            n = Network(
                nodes={k: {} for k in ("O", "A", "B", "Z")},
                bikes=[{"from": "A", "to": "Z"}],
                transit=[
                    ride("O", "A", 0, 0.15, "in"),
                    ride("A", "B", 0.5, 1.0, "out"),
                    ride("B", "A", 1.25, 1.6, "back"),
                ],
            )

            def provider(edge, departure):
                return {
                    "minutes": 0.5 if departure == at(favorable) else slow,
                    "p_empty": 0.0,
                }

            a, b = run(n, False, provider, top_k=1), run(n, provider=provider, top_k=1)
            self.assertAlmostEqual(
                a["routes"][0]["total_minutes"], b["routes"][0]["total_minutes"]
            )


if __name__ == "__main__":
    unittest.main()
