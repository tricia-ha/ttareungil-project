"""Read-only project cleanliness check: required assets, no local state, no saved notebook output."""

from pathlib import Path
import argparse, json, sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    ".env",
    ".venv",
    ".ocr_deps",
    "cache",
    "outputs",
    "__pycache__",
    ".ipynb_checkpoints",
    "web_ngrok.json",
    "personalization.sqlite",
    "web_personalization.sqlite",
}
SECRET_KEYS = {
    "KAKAO_REST_API_KEY",
    "KMA_API_KEY",
    "KMA_AUTH_KEY",
    "SEOUL_API_KEY",
    "KMA_HISTORY_API_KEY",
    "BICYCLE_PERSONALIZATION_ID",
    "NGROK_AUTHTOKEN",
}


def check_clean(root):
    root = Path(root).resolve()
    problems = []
    files = []
    for p in root.rglob("*"):
        if p.name in FORBIDDEN or (
            p.name.startswith(".env.") and p.name != ".env.example"
        ):
            problems.append("Local/private item: " + str(p.relative_to(root)))
        if p.is_symlink():
            problems.append("External link: " + str(p.relative_to(root)))
        if p.is_file():
            files.append(p)
    for p in (root / "notebooks").glob("*.ipynb"):
        nb = json.loads(p.read_text(encoding="utf-8-sig"))
        if any(
            c.get("outputs") or c.get("execution_count") is not None
            for c in nb["cells"]
            if c["cell_type"] == "code"
        ):
            problems.append("Saved notebook output: " + p.name)
    for p in (root / "data/inventory").iterdir():
        if p.is_file() and p.name != "station_crosswalk.csv":
            problems.append("Local inventory snapshot: " + p.name)
    # Compare against known local credential values without ever printing them.
    secrets = set()
    for settings in [
        root / "web_ngrok.json",
        root.parent / (root.name + "_local") / "web_ngrok.json",
    ]:
        if settings.is_file():
            value = json.loads(settings.read_text(encoding="utf-8")).get(
                "authtoken", ""
            )
            if isinstance(value, str) and len(value) >= 8:
                secrets.add(value.encode())
    for env in [
        root / ".env",
        root.parent / (root.name + "_local") / ".env",
        root / ".env.example",
    ]:
        if env.exists():
            for line in env.read_text(encoding="utf-8-sig").splitlines():
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip("\"'")
                if k.strip() in SECRET_KEYS and len(v) >= 8:
                    secrets.add(v.encode())
    for p in files:
        if secrets:
            with p.open("rb") as f:
                tail = b""
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    data = tail + chunk
                    if any(secret in data for secret in secrets):
                        problems.append(
                            "Credential content: " + str(p.relative_to(root))
                        )
                        break
                    tail = data[-max(map(len, secrets)) :]
    if problems:
        raise ValueError("\n".join(problems))
    print(
        "Project check OK:",
        len(files),
        "files; no local state or known credential content.",
    )
    return len(files)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=ROOT)
    args = p.parse_args()
    import check_project

    check_project.ROOT = args.root.resolve()
    try:
        check_project.check()
        check_clean(args.root)
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        raise SystemExit(1)
