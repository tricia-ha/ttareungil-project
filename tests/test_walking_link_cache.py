import sys, tempfile, unittest, time, json
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kakao_routes import KakaoClient
from walking_link_cache import WalkingLinkCache
from multimodal import Network


class WalkingLinkTests(unittest.TestCase):
    def test_fresh_cached_link_restores_without_http_and_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            a = dict(latitude=37.5, longitude=127.0, kind="endpoint")
            b = dict(latitude=37.501, longitude=127.0, kind="stop")
            payload = {
                "status": "OK",
                "route": {
                    "properties": {"totalTime": 120, "totalDistance": 150},
                    "legs": [],
                },
            }
            client = KakaoClient(
                Path(directory) / "api.sqlite",
                api_key="not-a-real-key",
                interval_seconds=0,
            )
            response = Mock(status_code=200)
            response.json.return_value = payload
            with patch.object(client.session, "get", return_value=response):
                client.get("walk", a, b)
            cache = WalkingLinkCache(Path(directory) / "links.sqlite")
            cache.remember("origin", "S", a, b)
            try:

                def network():
                    return Network(
                        nodes={"origin": a, "S": b}, walk_policy={"max_distance_m": 900}
                    )

                with patch.object(
                    client.session, "get", side_effect=AssertionError("no HTTP")
                ):
                    n = network()
                    self.assertEqual(cache.restore(n, client), 1)
                    self.assertEqual(n.walks[0]["minutes"], 2.0)
                    self.assertEqual(cache.restore(n, client), 0)
                    self.assertEqual(client.calls, 1)
                    with client.db:
                        client.db.execute(
                            "UPDATE kakao_cache SET at=?", (time.time() - 8 * 86400,)
                        )
                    self.assertEqual(cache.restore(network(), client), 0)
            finally:
                cache.close()
                client.close()

    def test_old_results_only_supply_endpoint_pairs_not_old_times(self):
        with tempfile.TemporaryDirectory() as directory:
            a = dict(latitude=37.5, longitude=127.0, kind="endpoint")
            b = dict(latitude=37.501, longitude=127.0, kind="stop")
            p = Path(directory) / "old.json"
            p.write_text(
                json.dumps(
                    {
                        "nodes": {"origin": a, "S": b},
                        "routes": [
                            {
                                "legs": [
                                    {
                                        "mode": "walk",
                                        "from": "origin",
                                        "to": "S",
                                        "minutes": 999,
                                    }
                                ]
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cache = WalkingLinkCache(Path(directory) / "links.sqlite")
            cache.remember_result(p)
            client = Mock()
            client.get_cached.return_value = (
                {
                    "status": "OK",
                    "route": {
                        "properties": {"totalTime": 180, "totalDistance": 150},
                        "legs": [],
                    },
                },
                "2026-10-27T01:00:00+00:00",
            )
            try:
                n = Network(
                    nodes={"origin": a, "S": b}, walk_policy={"max_distance_m": 900}
                )
                self.assertEqual(cache.restore(n, client), 1)
                self.assertEqual(n.walks[0]["minutes"], 3.0)
                moved = dict(b, latitude=37.502)
                self.assertEqual(
                    cache.restore(
                        Network(
                            nodes={"origin": a, "S": moved},
                            walk_policy={"max_distance_m": 900},
                        ),
                        client,
                    ),
                    0,
                )
                self.assertEqual(client.get_cached.call_count, 1)
            finally:
                cache.close()


if __name__ == "__main__":
    unittest.main()
