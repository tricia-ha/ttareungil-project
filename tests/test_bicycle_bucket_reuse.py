"""Dedicated cycling keeps time-dependent stock and every OD while reusing ETA windows."""

import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import Config, Network, Router
from multimodal_cache import CachedBikeProvider

START = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)


class Provider:
    def __init__(self, durations, available):
        self.durations, self.available = durations, available

    def predict_eta_batch(self, edges, departure):
        return [self.predict_eta(e, departure) for e in edges]

    def predict_eta(self, edge, departure):
        minute = (datetime.fromisoformat(departure) - START).total_seconds() / 60
        return {"minutes": self.durations[edge["to"]][min(3, int(minute // 5))]}

    def precheck_inventory(self, edge, departure):
        minute = (datetime.fromisoformat(departure) - START).total_seconds() / 60
        return {"p_empty": 0.0 if minute >= self.available[edge["to"]] else 1.0}

    def attach_inventory(self, value, edge, departure):
        return dict(value, **self.precheck_inventory(edge, departure))


class BicycleBucketTests(unittest.TestCase):
    def run_case(
        self,
        durations,
        available,
        optimized,
        required="bicycle",
        batch=2,
        risk_penalty=0,
    ):
        nodes = {
            "O": {},
            "Z": {},
            "A": {"kind": "bike"},
            **{k: {"kind": "bike"} for k in durations},
        }
        walks = [{"from": "O", "to": "A", "minutes": 0.5}] + [
            {"from": k, "to": "Z", "minutes": 1.0} for k in durations
        ]
        edges = [
            {"from": "A", "to": k, "eta_from": "A", "eta_to": k} for k in durations
        ]
        network = Network(nodes=nodes, walks=walks, bikes=edges)
        with tempfile.TemporaryDirectory() as d:
            cache = CachedBikeProvider(
                Provider(durations, available), Path(d) / "cache.sqlite", {}
            )
            try:
                return Router(
                    network,
                    cache,
                    Config(
                        required_mode=required,
                        reuse_bicycle_buckets=optimized,
                        bike_expansion_batch_size=batch,
                        risk_penalty_minutes=risk_penalty,
                        require_known_inventory=True,
                        max_empty_probability=0.1,
                        horizon_minutes=20,
                        top_k=len(durations),
                        max_seconds=10,
                    ),
                ).search("O", "Z", START)
            finally:
                cache.close()

    def signature(self, result):
        return sorted(
            (
                r["total_minutes"],
                [
                    (l["from"], l["to"], l["start"])
                    for l in r["legs"]
                    if l["mode"] == "bicycle"
                ],
            )
            for r in result["routes"]
        )

    def test_next_eta_bucket_can_be_faster_and_is_retained(self):
        args = ({"B": [20, 1, 20, 20]}, {"B": 0})
        before = self.run_case(*args, False)
        after = self.run_case(*args, True, batch=16)
        self.assertEqual(self.signature(before), self.signature(after))
        self.assertEqual(after["routes"][0]["total_minutes"], 7.0)
        self.assertGreater(after["skipped_bicycle_wait_states"], 0)
        self.assertLess(after["expanded_states"], before["expanded_states"])

    def test_stock_becomes_available_inside_bucket(self):
        args = ({"B": [1] * 4}, {"B": 2})
        before = self.run_case(*args, False)
        after = self.run_case(*args, True)
        self.assertEqual(self.signature(before), self.signature(after))
        self.assertEqual(after["routes"][0]["total_minutes"], 4.0)

    def test_one_unavailable_od_prevents_unsafe_wait_jump(self):
        args = ({"B": [10] * 4, "C": [1] * 4}, {"B": 0, "C": 3})
        before = self.run_case(*args, False)
        after = self.run_case(*args, True)
        self.assertEqual(self.signature(before), self.signature(after))
        self.assertEqual(after["routes"][0]["total_minutes"], 5.0)
        self.assertGreater(after["reused_bicycle_transitions"], 0)

    def test_integrated_mode_retains_original_search(self):
        r = self.run_case({"B": [2] * 4}, {"B": 0}, True, required=None)
        self.assertFalse(r["bicycle_bucket_reuse"])
        self.assertEqual(r["skipped_bicycle_wait_states"], 0)

    def test_risk_penalty_disables_time_dominance(self):
        r = self.run_case({"B": [2] * 4}, {"B": 0}, True, risk_penalty=1.0)
        self.assertFalse(r["bicycle_bucket_reuse"])

    def test_equal_arrivals_preserve_distinct_od_alternatives(self):
        r = self.run_case(
            {"B": [2] * 4, "C": [2] * 4}, {"B": 0, "C": 0}, True, batch=16
        )
        self.assertEqual(len(r["routes"]), 2)
        self.assertEqual(
            {
                l["to"]
                for rt in r["routes"]
                for l in rt["legs"]
                if l["mode"] == "bicycle"
            },
            {"B", "C"},
        )

    def test_many_time_dependent_cases_keep_all_ranked_od_routes(self):
        randomizer = random.Random(2710)
        for _ in range(35):
            durations = {
                k: [randomizer.randint(1, 15) for _ in range(4)]
                for k in ("B", "C", "D")
            }
            available = {k: randomizer.randint(0, 8) for k in durations}
            with self.subTest(durations=durations, available=available):
                before = self.run_case(durations, available, False)
                after = self.run_case(durations, available, True, batch=16)
                self.assertEqual(self.signature(before), self.signature(after))
                self.assertTrue(after["search_complete"])


if __name__ == "__main__":
    unittest.main()
