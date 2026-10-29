from pathlib import Path
import sys, tempfile, unittest, json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_project_clean import check_clean


class ProjectCleanTests(unittest.TestCase):
    def setup_root(self, path):
        root = Path(path) / "project"
        (root / "data/inventory").mkdir(parents=True)
        (root / "data/inventory/station_crosswalk.csv").write_text(
            "id\n1\n", encoding="utf-8"
        )
        (root / "notebooks").mkdir()
        return root

    def test_portable_files_accepted_and_private_file_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.setup_root(d)
            self.assertEqual(check_clean(root), 1)
            (root / ".env").write_text("KMA_API_KEY=not-for-sharing", encoding="utf-8")
            with self.assertRaises(ValueError) as e:
                check_clean(root)
            self.assertNotIn("not-for-sharing", str(e.exception))

    def test_saved_notebook_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.setup_root(d)
            (root / "notebooks/test.ipynb").write_text(
                json.dumps(
                    {
                        "cells": [
                            {
                                "cell_type": "code",
                                "outputs": [{"text": "old"}],
                                "execution_count": 1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                check_clean(root)

    def test_secret_content_outside_env_is_detected_without_printing_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.setup_root(d)
            private = root.parent / "project_local"
            private.mkdir()
            (private / ".env").write_text(
                "KMA_API_KEY=private-test-key", encoding="utf-8"
            )
            (root / "example.txt").write_text(
                "accidentally private-test-key", encoding="utf-8"
            )
            with self.assertRaises(ValueError) as e:
                check_clean(root)
            self.assertIn("example.txt", str(e.exception))
            self.assertNotIn("private-test-key", str(e.exception))

    def test_ngrok_secret_in_accidental_copy_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.setup_root(d)
            private = root.parent / "project_local"
            private.mkdir()
            secret = "dummy-ngrok-private-credential"
            (private / "web_ngrok.json").write_text(
                json.dumps({"authtoken": secret}), encoding="utf-8"
            )
            (root / "accidental.txt").write_text(secret, encoding="utf-8")
            with self.assertRaises(ValueError) as e:
                check_clean(root)
            self.assertIn("accidental.txt", str(e.exception))
            self.assertNotIn(secret, str(e.exception))

    def test_private_account_database_cannot_be_packaged(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.setup_root(d)
            (root / "web_personalization.sqlite").write_bytes(b"private")
            with self.assertRaises(ValueError):
                check_clean(root)


if __name__ == "__main__":
    unittest.main()
