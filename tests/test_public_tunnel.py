"""Fixed URL/QR persistence, secret isolation, and tunnel startup guards."""

from pathlib import Path
from unittest.mock import patch, Mock
import contextlib, io, json, os, sys, tempfile, unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]
import public_tunnel as tunnel
import web_tunnel_config as settings

URL = "https://example-demo.ngrok-free.app"
TOKEN = "local_test_token_123456789"


class TunnelTests(unittest.TestCase):
    def test_normalizes_only_ngrok_https_hostnames(self):
        self.assertEqual(settings.normalize_url("example-demo.ngrok-free.app/"), URL)
        for value in [
            "http://example-demo.ngrok-free.app",
            "https://evil.example",
            URL + "/path",
            URL + "?token=foo",
            URL + ":123",
            "https://user@" + URL[8:],
            "https://bad.ngrok-free.app.evil.test",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.normalize_url(value)

    def test_credentials_round_trip_and_environment_override(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(
                settings, "settings_path", return_value=Path(folder) / "settings.json"
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            path = settings.save_settings(TOKEN, URL)
            self.assertEqual(settings.load_settings(), (TOKEN, URL))
            self.assertFalse(path.with_suffix(".tmp").exists())
            with patch.dict(os.environ, {"NGROK_DOMAIN": "other.ngrok-free.dev"}):
                self.assertEqual(settings.configured_host(), "other.ngrok-free.dev")

    def test_unchanged_address_keeps_qr_file_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            qr = tunnel.publish_address(URL, output)
            before = (qr.read_bytes(), qr.stat().st_mtime_ns)
            with patch("qrcode.make", side_effect=AssertionError("must reuse")):
                tunnel.publish_address(URL, output)
            self.assertEqual(before, (qr.read_bytes(), qr.stat().st_mtime_ns))
            self.assertEqual((output / "접속주소.txt").read_text().strip(), URL)

    def test_agent_gets_only_its_own_token(self):
        with patch.dict(
            os.environ,
            {
                "KMA_API_KEY": "weather_secret",
                "SEOUL_API_KEY": "seoul_secret",
                "OTHER_TOKEN": "private",
            },
            clear=True,
        ):
            env = tunnel.child_environment(TOKEN)
        self.assertEqual(env, {"NGROK_AUTHTOKEN": TOKEN})

    def test_missing_account_does_not_launch_or_call_apis(self):
        with (
            patch.object(tunnel, "load_settings", return_value=("", "")),
            patch.object(tunnel.requests, "get") as get,
            patch.object(tunnel, "get_binary") as binary,
        ):
            with self.assertRaisesRegex(RuntimeError, "ngrok_설정"):
                tunnel.main([])
            get.assert_not_called()
            binary.assert_not_called()

    def test_tunnel_start_requires_exact_host_acceptance(self):
        health = Mock(status_code=200)
        health.json.return_value = {"model_version": "V5.2.1"}
        with (
            patch.object(tunnel, "load_settings", return_value=(TOKEN, URL)),
            patch.object(tunnel, "tunnel_already_running", return_value=False),
            patch.object(
                tunnel.requests, "get", side_effect=[health, Mock(status_code=400)]
            ),
            patch.object(tunnel, "get_binary") as binary,
        ):
            with self.assertRaisesRegex(RuntimeError, "웹_종료"):
                tunnel.main([])
            binary.assert_not_called()

    def test_started_tunnel_saves_only_the_expected_domain_and_cleans_pid(self):
        with tempfile.TemporaryDirectory() as folder:
            child = Mock(
                pid=12345,
                stdout=[
                    json.dumps(
                        {"msg": "started tunnel", "url": "https://other.ngrok-free.app"}
                    )
                    + "\n",
                    json.dumps({"msg": "started tunnel", "url": URL}) + "\n",
                ],
            )
            child.wait.return_value = 0
            child.poll.return_value = 0
            health = Mock(status_code=200)
            health.json.return_value = {"model_version": "V5.2.1"}
            cache = Path(folder) / "cache"
            cache.mkdir()
            with (
                patch.object(tunnel, "load_settings", return_value=(TOKEN, URL)),
                patch.object(tunnel, "tunnel_already_running", return_value=False),
                patch.object(tunnel.requests, "get", return_value=health),
                patch.object(
                    tunnel, "get_binary", return_value=Path(folder) / "ngrok.exe"
                ),
                patch.object(tunnel, "CACHE_ROOT", cache),
                patch.object(tunnel, "RUNTIME_CACHE_DIR", cache),
                patch.object(tunnel, "RUNTIME_OUTPUT_DIR", Path(folder) / "outputs"),
                patch.object(tunnel.subprocess, "Popen", return_value=child) as launch,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(tunnel.main([]), 0)
            self.assertTrue((Path(folder) / "outputs/web/접속QR.png").is_file())
            self.assertFalse((cache / "web_tunnel.json").exists())
            self.assertNotIn(TOKEN, str(launch.call_args.args))
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertIn("--inspect=false", launch.call_args.args[0])

    def test_only_configured_ngrok_host_is_allowed(self):
        from web_server import allowed_hosts

        with patch.object(settings, "load_settings", return_value=(TOKEN, URL)):
            hosts = allowed_hosts()
        self.assertIn(URL[8:], hosts)
        self.assertNotIn("*.ngrok-free.app", hosts)


if __name__ == "__main__":
    unittest.main()
