"""Create a project-local environment using the selected interpreter."""

from pathlib import Path
import struct, sys, venv, subprocess


def validate_interpreter(version=None, bits=None):
    version = version or sys.version_info
    bits = bits or struct.calcsize("P") * 8
    if not (3, 11) <= tuple(version[:2]) < (3, 14):
        raise RuntimeError(
            "Python 3.11~3.13을 사용하세요. Python 3.11 x64를 권장합니다."
        )
    if bits != 64:
        raise RuntimeError("64비트 Python이 필요합니다.")


if __name__ == "__main__":
    try:
        validate_interpreter()
        target = Path(__file__).resolve().parents[1] / ".venv"
        venv.EnvBuilder(with_pip=True).create(target)
        print("Environment created:", target)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print("Environment setup failed: " + str(exc), file=sys.stderr)
        print(
            "Check the Python version, folder permissions and TEMP directory.",
            file=sys.stderr,
        )
        raise SystemExit(1)
