from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multimodal import V521BikeProvider, Network, Router, Config
from multimodal_cache import CachedBikeProvider
from scheduled_runtime import ScheduledConfig
from kakao_optimizer import KakaoConfig, map_html


class FakeETA:
    def predict(self, **kwargs):
        return dict(
            eta={"p50_min": 10.0, "p80_min": 15.0, "p90_min": 20.0}, route={}, model={}
        )

    def predict_batch(self, requests):
        return [self.predict() for r in requests]


class BikeQuantileTests(unittest.TestCase):
    edge = {"from": "O", "to": "Z", "eta_from": "1", "eta_to": "2"}
    departure = "2026-10-17T08:00:00+09:00"

    def test_default_and_batch_use_p50(self):
        provider = V521BikeProvider(FakeETA())
        self.assertEqual(
            provider.predict_eta(self.edge, self.departure)["minutes"], 10.0
        )
        self.assertEqual(
            provider.predict_eta_batch([self.edge], self.departure)[0]["minutes"], 10.0
        )
        self.assertEqual(ScheduledConfig().bike_quantile, "p50")
        self.assertEqual(KakaoConfig().bike_quantile, "p50")

    def test_quantiles_do_not_share_cached_minutes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bike.sqlite"
            for quantile, minutes in (("p80", 15.0), ("p50", 10.0)):
                provider = V521BikeProvider(FakeETA(), eta_quantile=quantile)
                cached = CachedBikeProvider(provider, path, {"same": "context"})
                try:
                    self.assertEqual(
                        cached(self.edge, self.departure)["minutes"], minutes
                    )
                    self.assertEqual(cached.misses, 1)
                finally:
                    cached.close()

    def test_p50_can_change_winner_without_forcing_bicycle(self):
        network = Network(
            nodes={"O": {}, "Z": {}},
            bikes=[self.edge],
            transit=[
                {
                    "from": "O",
                    "to": "Z",
                    "mode": "bus",
                    "trip_id": "B",
                    "departure": self.departure,
                    "arrival": "2026-10-17T08:12:00+09:00",
                }
            ],
        )
        for quantile, expected in (("p80", "bus"), ("p50", "bicycle")):
            result = Router(
                network,
                V521BikeProvider(FakeETA(), eta_quantile=quantile),
                Config(top_k=1, require_known_inventory=False),
            ).search("O", "Z", self.departure)
            modes = {leg["mode"] for leg in result["routes"][0]["legs"]}
            self.assertIn(expected, modes)

    def test_invalid_quantile_is_rejected(self):
        with self.assertRaises(ValueError):
            V521BikeProvider(FakeETA(), eta_quantile="mean")
        with self.assertRaises(ValueError):
            ScheduledConfig(bike_quantile="mean").validate()
        with self.assertRaises(ValueError):
            KakaoConfig(bike_quantile="mean").validate()
