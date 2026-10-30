import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
import shared_assets
from export_public_routes import NUMBERS, OPTIONAL_NUMBERS, export, public_payload


class SharedAssetsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.reference = self.root / "reference"
        self.reference.mkdir()
        self.target = self.root / "cache/routes.sqlite"

    def seed(self):
        raw = b"SQLite format 3\x00" + b"route-data" * 20
        packed = gzip.compress(raw, mtime=0)
        (self.reference / "osm_station_routes.sqlite.gz").write_bytes(packed)
        self.metadata = dict(
            graph_signature="graph",
            sha256=hashlib.sha256(packed).hexdigest(),
            uncompressed_bytes=len(raw),
        )
        self.write_manifest()
        return raw

    def write_manifest(self):
        (self.reference / "manifest.json").write_text(
            json.dumps(self.metadata), encoding="utf-8"
        )

    def test_initializes_missing_cache_and_never_replaces_existing(self):
        raw = self.seed()
        self.assertTrue(
            shared_assets.seed_route_cache(self.target, "graph", self.reference)
        )
        self.assertEqual(self.target.read_bytes(), raw)
        self.target.write_bytes(b"existing-local-cache")
        self.assertFalse(
            shared_assets.seed_route_cache(self.target, "graph", self.reference)
        )
        self.assertEqual(self.target.read_bytes(), b"existing-local-cache")
        self.assertEqual(list(self.target.parent.glob("routes-seed-*")), [])

    def test_missing_or_different_graph_is_skipped(self):
        self.assertFalse(
            shared_assets.seed_route_cache(self.target, "graph", self.reference)
        )
        self.seed()
        self.assertFalse(
            shared_assets.seed_route_cache(self.target, "other", self.reference)
        )
        self.assertFalse(self.target.exists())

    def test_corrupted_archive_or_size_is_rejected(self):
        self.seed()
        self.metadata["sha256"] = "incorrect"
        self.write_manifest()
        with self.assertRaises(ValueError):
            shared_assets.seed_route_cache(self.target, "graph", self.reference)
        self.seed()
        self.metadata["uncompressed_bytes"] += 1
        self.write_manifest()
        with self.assertRaises(ValueError):
            shared_assets.seed_route_cache(self.target, "graph", self.reference)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob("routes-seed-*")), [])

    def test_ocr_prefers_bundled_weights_with_legacy_fallback(self):
        with (
            patch.object(shared_assets, "PROJECT_ROOT", self.root),
            patch.object(shared_assets, "CACHE_ROOT", self.root / "cache"),
        ):
            self.assertEqual(
                shared_assets.ocr_model_options(),
                {"model_storage_directory": str(self.root / "cache/ocr_models")},
            )
            folder = self.root / "models/ocr"
            folder.mkdir(parents=True)
            for name in ("craft_mlt_25k.pth", "korean_g2.pth"):
                (folder / name).write_bytes(b"test-weight")
            self.assertEqual(
                shared_assets.ocr_model_options(),
                dict(model_storage_directory=str(folder), download_enabled=False),
            )

    def test_bootstrap_uses_configured_runtime_cache(self):
        model = self.root / "model"
        model.mkdir()
        (model / "manifest.json").write_text(
            json.dumps({"graph_signature": "graph"}), encoding="utf-8"
        )
        with (
            patch.object(shared_assets, "ETA_MODEL_DIR", model),
            patch.dict("os.environ", {"BICYCLE_CACHE_DIR": str(self.root / "custom")}),
            patch.object(shared_assets, "seed_route_cache") as seed,
        ):
            shared_assets.prepare_route_cache()
            seed.assert_called_once_with(self.root / "custom/routes_v2.sqlite", "graph")

    @staticmethod
    def route():
        value = {key: 1 for key in NUMBERS - OPTIONAL_NUMBERS}
        value.update(
            route_id="a" * 24,
            route_source="osm_srtm_v2",
            geometry={
                "type": "LineString",
                "coordinates": [[127.0, 37.0], [127.1, 37.1]],
            },
            user_id="PRIVATE-USER",
            api_key="PRIVATE-KEY",
        )
        return value

    def test_export_drops_unknown_fields_and_accepts_older_route_shape(self):
        clean = public_payload(json.dumps([self.route()]))
        self.assertNotIn("PRIVATE", clean)
        self.assertNotIn("access_start_m", json.loads(clean)[0])
        bad = self.route()
        bad["route_source"] = "external-api"
        self.assertIsNone(public_payload(json.dumps([bad])))

    def test_export_excludes_non_station_queries_and_seed_opens_as_database(self):
        model = self.root / "model"
        (model / "data").mkdir(parents=True)
        (model / "manifest.json").write_text(
            json.dumps({"graph_signature": "graph"}), encoding="utf-8"
        )
        (model / "data/stations_current.csv").write_text(
            "latitude,longitude\n37.0,127.0\n37.1,127.1\n", encoding="utf-8"
        )
        key = hashlib.sha256(
            json.dumps(
                ["graph", 37.0, 127.0, 37.1, 127.1], sort_keys=True, default=str
            ).encode()
        ).hexdigest()[:24]
        source = self.root / "source.sqlite"
        connection = sqlite3.connect(source)
        try:
            connection.execute(
                "CREATE TABLE routes(cache_key TEXT PRIMARY KEY,payload TEXT)"
            )
            connection.executemany(
                "INSERT INTO routes VALUES(?,?)",
                [
                    (key, json.dumps([self.route()])),
                    ("unknown-coordinate-key", json.dumps([self.route()])),
                ],
            )
            connection.commit()
        finally:
            connection.close()
        metadata = export(source, self.reference, model)
        self.assertEqual(metadata["rows"], 1)
        self.assertTrue(
            shared_assets.seed_route_cache(self.target, "graph", self.reference)
        )
        connection = sqlite3.connect(self.target)
        try:
            rows = connection.execute("SELECT cache_key,payload FROM routes").fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], key)
        self.assertNotIn("PRIVATE", rows[0][1])


if __name__ == "__main__":
    unittest.main()
