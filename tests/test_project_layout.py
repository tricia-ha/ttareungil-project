from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import project_paths
from project_files import required_paths


class ProjectLayoutTests(unittest.TestCase):
    def test_optional_private_state_does_not_change_portable_asset_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            private = Path(directory) / "project_local"
            (private / "cache").mkdir(parents=True)
            (private / ".env").write_text(
                "KMA_API_KEY=private-test\n", encoding="utf-8"
            )
            with (
                patch.object(project_paths, "PROJECT_ROOT", root),
                patch.dict(os.environ, {}, clear=True),
            ):
                self.assertEqual(project_paths.mutable_root("cache"), private / "cache")
                self.assertEqual(
                    project_paths.resolve_project_path("cache/team"),
                    private / "cache/team",
                )
                self.assertEqual(
                    project_paths.resolve_project_path("data/team"), root / "data/team"
                )
                project_paths.load_project_env()
                self.assertEqual(os.environ["KMA_API_KEY"], "private-test")
                (root / ".env").write_text(
                    "KMA_API_KEY=project-test\n", encoding="utf-8"
                )
                os.environ.pop("KMA_API_KEY")
                project_paths.load_project_env()
                self.assertEqual(os.environ["KMA_API_KEY"], "project-test")

    def test_paths_do_not_depend_on_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(project_paths, "PROJECT_ROOT", Path(directory)):
                with patch.dict(
                    os.environ, {"BICYCLE_CACHE_DIR": "cache/team"}, clear=True
                ):
                    project_paths.load_project_env()
                    self.assertEqual(
                        Path(os.environ["BICYCLE_CACHE_DIR"]),
                        Path(directory).resolve() / "cache/team",
                    )

    def test_env_paths_and_existing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                'KMA_API_KEY=file-value\nBICYCLE_KMA_CACHE="cache/날씨"\nUNKNOWN=bad',
                encoding="utf-8",
            )
            with patch.object(project_paths, "PROJECT_ROOT", root):
                with patch.dict(os.environ, {"KMA_API_KEY": "existing"}, clear=True):
                    project_paths.load_project_env()
                    self.assertEqual(os.environ["KMA_API_KEY"], "existing")
                    self.assertEqual(
                        Path(os.environ["BICYCLE_KMA_CACHE"]),
                        root.resolve() / "cache/날씨",
                    )
                    self.assertNotIn("UNKNOWN", os.environ)

    def test_active_notebook_structure(self):
        path = ROOT / "notebooks/02_시간표최적화.ipynb"
        before = path.read_bytes()
        clean = json.loads(before.decode("utf-8-sig"))
        self.assertEqual(clean["nbformat"], 4)
        for cell in clean["cells"]:
            if cell["cell_type"] == "code":
                self.assertIsInstance(cell["outputs"], list)
                self.assertTrue(
                    cell["execution_count"] is None
                    or isinstance(cell["execution_count"], int)
                )
        self.assertEqual(path.read_bytes(), before)

    def test_required_runtime_files_exist(self):
        for name in required_paths():
            self.assertTrue((ROOT / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
