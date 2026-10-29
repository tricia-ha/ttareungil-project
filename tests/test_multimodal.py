"""Synthetic algorithm tests; these are not Seoul performance results."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import unittest
from multimodal import Network, Router, Config, V521BikeProvider

T = "2026-10-14T08:"


def at(m):
    return T + f"{m:02d}:00+09:00"


def walk(a, b, m):
    return {"from": a, "to": b, "minutes": m}


def bike(a, b, m):
    return {"from": a, "to": b, "test_minutes": m}


def transit(a, b, d, r, trip="train"):
    return dict(
        walk(a, b, r - d), departure=at(d), arrival=at(r), mode="subway", trip_id=trip
    )


def provider(edge, t):
    return dict(minutes=edge["test_minutes"], p_empty=None)


def demo_network():
    return Network(
        nodes={k: {} for k in ["O", "A", "B", "S", "X", "C", "D", "Z"]},
        walks=[
            walk("O", "Z", 50),
            walk("O", "A", 1),
            walk("B", "S", 1),
            walk("X", "C", 1),
            walk("D", "Z", 1),
        ],
        bikes=[bike("A", "B", 3), bike("C", "D", 3)],
        transit=[transit("S", "X", 6, 12)],
        metadata={"synthetic": True},
    )


class Tests(unittest.TestCase):
    def search(self, n, p=provider, **kw):
        return Router(n, p, Config(horizon_minutes=60, top_k=1, **kw)).search(
            "O", "Z", at(0)
        )

    def test_two_rentals(self):
        r = self.search(demo_network())["routes"][0]
        self.assertEqual(r["bike_rentals"], 2)
        self.assertEqual(r["total_minutes"], 17)

    def test_walking_wins(self):
        n = demo_network()
        n.walks[0]["minutes"] = 2
        r = self.search(n)["routes"][0]
        self.assertEqual(r["bike_rentals"], 0)

    def test_missed_train(self):
        n = demo_network()
        n.bikes[0]["test_minutes"] = 5
        n.transit.append(transit("S", "X", 10, 16, "later"))
        r = self.search(n)["routes"][0]
        self.assertEqual(r["total_minutes"], 21)
        self.assertIn("later", [l.get("trip_id") for l in r["legs"]])

    def test_wait_for_non_fifo_bike(self):
        n = Network({"O": {}, "Z": {}}, bikes=[bike("O", "Z", 20)])

        def p(e, t):
            return {"minutes": 1 if t == at(2) else 20}

        self.assertEqual(self.search(n, p)["routes"][0]["total_minutes"], 3)

    def test_unknown_risk_policy(self):
        r = self.search(demo_network(), require_known_inventory=True)["routes"][0]
        self.assertEqual(r["bike_rentals"], 0)

    def test_budget_not_optimal(self):
        r = self.search(demo_network(), max_expansions=1)
        self.assertFalse(r["search_complete"])
        self.assertEqual(r["status"], "partial")

    def test_no_extra_rental_minute(self):
        n = Network({"O": {}, "Z": {}}, bikes=[bike("O", "Z", 3)])
        self.assertEqual(self.search(n)["routes"][0]["total_minutes"], 3)

    def test_no_immediate_rerental_splitting(self):
        n = Network(
            {"O": {}, "B": {}, "Z": {}},
            walks=[walk("O", "Z", 10)],
            bikes=[bike("O", "B", 1), bike("B", "Z", 1)],
        )
        r = self.search(n)["routes"][0]
        self.assertEqual(r["bike_rentals"], 0)
        self.assertEqual(r["total_minutes"], 10)

    def test_fraction_preserves_exact_arrival(self):
        n = Network({"O": {}, "Z": {}}, walks=[walk("O", "Z", 1.1)])
        self.assertAlmostEqual(self.search(n)["routes"][0]["total_minutes"], 1.1)

    def test_adapter_uses_p50_and_metadata(self):
        class ETA:
            def predict(self, **kwargs):
                return dict(
                    eta={"p50_min": 10, "p80_min": 15, "p90_min": 20},
                    route={"geometry": []},
                    model={"weather_available": False},
                )

        p = V521BikeProvider(ETA(), user_factor=1)
        result = p({"eta_from": "1", "eta_to": "2"}, at(0))
        self.assertEqual(result["minutes"], 10)
        self.assertEqual(result["eta_quantile"], "p50")
        self.assertFalse(result["model"]["weather_available"])
        self.assertIsNone(result["p_empty"])

    def test_stay_aboard_without_dropoff(self):
        n = Network(
            {"O": {}, "X": {}, "Z": {}},
            transit=[transit("O", "X", 0, 3), transit("X", "Z", 4, 7)],
        )
        n.transit[0]["dropoff_allowed"] = False
        n.transit[1]["pickup_allowed"] = False
        r = self.search(n)["routes"][0]
        self.assertEqual(r["transit_boardings"], 1)
        self.assertEqual(r["total_minutes"], 7)


if __name__ == "__main__":
    unittest.main()
