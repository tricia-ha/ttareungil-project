"""Optional walking failures and blocking transport must not lose known routes."""

import sys, time, tempfile, threading, unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from kakao_routes import KakaoClient, KakaoError, CallLimit, WalkingDeadline
from scheduled_runtime import KakaoWalking, ScheduledConfig, build_scheduled_network
from multimodal import Network, Router, Config

START = "2026-10-30T08:30:00+09:00"
A = dict(latitude=37.5, longitude=127.0)
B = dict(latitude=37.505, longitude=127.0)


def network():
    return Network(
        nodes={
            "origin": A,
            "destination": B,
            "S": dict(latitude=37.5001, longitude=127.0, kind="stop"),
            "T": dict(latitude=37.5049, longitude=127.0, kind="stop"),
        },
        walks=[
            {"from": "origin", "to": "S", "minutes": 1},
            {"from": "T", "to": "destination", "minutes": 1},
        ],
        transit=[
            {
                "from": "S",
                "to": "T",
                "mode": "bus",
                "trip_id": "B",
                "departure": "2026-10-30T08:32:00+09:00",
                "arrival": "2026-10-30T08:35:00+09:00",
            }
        ],
        walk_policy={"max_distance_m": 900},
    )


class WalkingReliabilityTests(unittest.TestCase):
    def test_optional_failure_preserves_known_route_and_redacts_error(self):
        def fail(*args):
            raise KakaoError("secret-token https://api.example/?key=secret-token")

        result = Router(
            network(), None, Config(required_mode="transit"), walking_provider=fail
        ).search("origin", "destination", START)
        self.assertEqual(result["routes"][0]["total_minutes"], 6)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["termination_reason"], "walking_api_error")
        self.assertGreater(result["network_metadata"]["walking_api_errors"], 0)
        self.assertNotIn("secret-token", str(result))
        self.assertFalse(result["search_complete"])

    def test_malformed_walking_payload_is_an_optional_failure(self):
        from kakao_routes import walking_edge

        with self.assertRaises(KakaoError):
            walking_edge(
                {"status": "OK", "route": {"properties": {"totalTime": "invalid"}}},
                "now",
                "A",
                "B",
            )

    def test_timetable_strings_are_parsed_once_per_build(self):
        from multimodal import timestamp, _time_parser

        parse = _time_parser()
        with patch("multimodal.timestamp", wraps=timestamp) as original:
            results = [parse(START) for _ in range(1000)]
            self.assertEqual(original.call_count, 1)
            self.assertEqual(results, [timestamp(START)] * 1000)

    def test_seed_failure_keeps_successful_connections(self):
        calls = []

        def walk(a, b, start, end):
            calls.append((start, end))
            if len(calls) == 1:
                raise KakaoError("simulated failure")
            return {"from": start, "to": end, "minutes": 1}

        n = network()
        built = build_scheduled_network(
            n, [], A, B, walk, ScheduledConfig(search_mode="transit")
        )
        self.assertTrue(built.walks)
        self.assertEqual(built.metadata["walking_api_errors"], 1)

    def test_http_wait_returns_at_deadline_and_late_response_cannot_write_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            client = KakaoClient(
                Path(directory) / "cache.sqlite",
                api_key="dummy",
                max_calls=20,
                interval_seconds=0,
            )
            release = threading.Event()
            response = Mock(status_code=200)
            response.json.return_value = {"status": "NO_RESULTS"}

            def blocked(*args, **kwargs):
                release.wait(3)
                return response

            try:
                client.deadline = time.monotonic() + 0.06
                with patch.object(
                    client.session, "get", side_effect=blocked
                ) as request:
                    start = time.monotonic()
                    with self.assertRaises(WalkingDeadline):
                        client.get("walk", A, B)
                    self.assertLess(time.monotonic() - start, 0.5)
                    self.assertEqual(client.calls, 1)
                    self.assertLessEqual(request.call_args.kwargs["timeout"], 0.06)
                    release.set()
                    client._inflight.result(timeout=2)
                    self.assertEqual(
                        client.db.execute(
                            "select count(*) from kakao_cache"
                        ).fetchone()[0],
                        0,
                    )
            finally:
                release.set()
                client.close()

    def test_expired_deadline_uses_no_call_and_can_still_read_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            client = KakaoClient(
                Path(directory) / "cache.sqlite", api_key="dummy", interval_seconds=0
            )
            response = Mock(status_code=200)
            response.json.return_value = {"status": "NO_RESULTS"}
            try:
                with patch.object(
                    client.session, "get", return_value=response
                ) as request:
                    client.get("walk", A, B)
                    client.deadline = time.monotonic() - 1
                    client.get("walk", A, B)
                    with self.assertRaises(WalkingDeadline):
                        client.get("walk", B, A)
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(client.calls, 1)
            finally:
                client.close()

    def test_known_route_survives_transport_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            client = KakaoClient(
                Path(directory) / "cache.sqlite", api_key="dummy", interval_seconds=0
            )
            release = threading.Event()

            def blocked(*args, **kwargs):
                release.wait(3)
                return Mock(status_code=200)

            try:
                with patch.object(client.session, "get", side_effect=blocked):
                    result = Router(
                        network(),
                        None,
                        Config(required_mode="transit", max_seconds=0.1),
                        walking_provider=KakaoWalking(client),
                    ).search("origin", "destination", START)
                    self.assertEqual(result["routes"][0]["total_minutes"], 6)
                    self.assertEqual(result["termination_reason"], "max_seconds")
                    self.assertLess(result["elapsed_seconds"], 0.6)
                    self.assertEqual(client.calls, 1)
            finally:
                release.set()
                if client._inflight:
                    client._inflight.result(timeout=2)
                client.close()

    def test_exhausted_budget_does_not_sleep_for_each_optional_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            client = KakaoClient(
                Path(directory) / "cache.sqlite", api_key="dummy", max_calls=1
            )
            client.calls = 1
            client.last_call = time.monotonic()
            try:
                with patch("kakao_routes.time.sleep") as sleep:
                    for _ in range(10):
                        with self.assertRaises(CallLimit):
                            client.get("walk", A, B)
                    sleep.assert_not_called()
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
