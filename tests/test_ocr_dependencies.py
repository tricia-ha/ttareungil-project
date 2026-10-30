from pathlib import Path
from unittest.mock import patch
import sys, tempfile, unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import ocr_dependencies


class OCRDependencyTests(unittest.TestCase):
    def test_active_environment_is_preferred(self):
        expected = object()
        with (
            patch.object(
                ocr_dependencies.importlib, "import_module", return_value=expected
            ),
            patch.object(
                ocr_dependencies, "compatible_development_wheels"
            ) as compatibility,
        ):
            self.assertIs(ocr_dependencies.load_easyocr(), expected)
            compatibility.assert_not_called()

    def test_incompatible_wheel_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "binary.dist-info"
            p.mkdir()
            (p / "WHEEL").write_text("Tag: cp999-cp999-win_amd64\n", encoding="utf-8")
            self.assertFalse(ocr_dependencies.compatible_development_wheels(d))
            (p / "WHEEL").write_text("Tag: py3-none-any\n", encoding="utf-8")
            self.assertTrue(ocr_dependencies.compatible_development_wheels(d))

    def test_incomplete_installed_dependency_does_not_override_with_development_folder(
        self,
    ):
        with (
            patch.object(
                ocr_dependencies.importlib,
                "import_module",
                side_effect=ModuleNotFoundError(name="pyclipper"),
            ),
            self.assertRaises(RuntimeError),
        ):
            ocr_dependencies.load_easyocr()


if __name__ == "__main__":
    unittest.main()
