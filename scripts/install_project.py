"""Install the local CPU environment, validate it, then record exact versions."""

from pathlib import Path
from importlib import metadata
import subprocess
import sys
from create_environment import validate_interpreter

ROOT = Path(__file__).resolve().parents[1]


def main():
    validate_interpreter()
    lock = (
        ROOT
        / "config"
        / f"constraints-{sys.platform}-py{sys.version_info.major}{sys.version_info.minor}.txt"
    )
    lock.parent.mkdir(parents=True, exist_ok=True)
    constraint = ["-c", str(lock)] if lock.exists() else []
    cpu_index = (
        ["--index-url", "https://download.pytorch.org/whl/cpu"]
        if sys.platform in ("win32", "linux")
        else []
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            *constraint,
            "torch==2.9.1",
            "torchvision==0.24.1",
            *cpu_index,
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(ROOT / "requirements.txt"),
            *constraint,
        ],
        check=True,
    )
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_project.py"), "--dependencies"],
        check=True,
    )
    if not lock.exists():
        # Names and versions only: no credentials, local paths or repository URLs.
        versions = {
            d.metadata["Name"]: d.version
            for d in metadata.distributions()
            if d.metadata.get("Name")
        }
        lock.write_text(
            "# Generated after dependency validation; platform/Python specific.\n"
            + "".join(
                f"{name}=={value}\n"
                for name, value in sorted(versions.items(), key=lambda x: x[0].lower())
            ),
            encoding="utf-8",
        )
    print("Environment validated. Version reference: " + lock.name)


if __name__ == "__main__":
    main()
