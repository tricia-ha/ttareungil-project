from pathlib import Path
import sys, unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from create_environment import validate_interpreter


class BootstrapTests(unittest.TestCase):
    def test_supported_64bit_versions(self):
        for minor in (11, 12, 13):
            validate_interpreter((3, minor, 0), 64)

    def test_unsupported_version_or_32bit_has_clear_error(self):
        for version, bits in [((3, 10, 9), 64), ((3, 14, 0), 64), ((3, 11, 9), 32)]:
            with self.assertRaises(RuntimeError):
                validate_interpreter(version, bits)


if __name__ == "__main__":
    unittest.main()
