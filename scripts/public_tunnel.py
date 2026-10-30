"""Run the assigned ngrok HTTPS endpoint and reuse its QR code."""

from pathlib import Path
import argparse, getpass, json, os, re, shutil, subprocess, sys, zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from project_paths import (
    CACHE_ROOT,
    RUNTIME_OUTPUT_DIR,
    RUNTIME_CACHE_DIR,
    load_project_env,
)
from web_tunnel_config import load_settings, normalize_url, save_settings
from web_runtime import prepare

prepare()
import requests

DOWNLOAD = "https://bin.ngrok.com/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"


def verify_signature(path):
    if os.name != "nt":
        raise RuntimeError("이 자동 설치는 Windows x64용입니다.")
    # PowerShell 7 module paths cannot be loaded by Windows PowerShell 5.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    env["NGROK_VERIFY_PATH"] = str(path)
    command = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:NGROK_VERIFY_PATH; "
        "@{status=[string]$s.Status;subject=[string]$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    value = json.loads(result.stdout) if result.returncode == 0 else {}
    if (
        value.get("status") != "Valid"
        or "NGROK" not in value.get("subject", "").upper()
    ):
        raise RuntimeError(
            "ngrok 배포 서명을 확인하지 못했습니다. 공식 Windows 설치 안내를 확인해 주세요."
        )


def get_binary():
    existing = shutil.which("ngrok")
    if existing:
        return Path(existing).resolve()
    target = CACHE_ROOT / "tools/ngrok.exe"
    if target.is_file():
        return target
    print("ngrok 공식 배포 파일을 준비합니다.", flush=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    archive = target.with_suffix(".zip.partial")
    part = target.with_name("ngrok-download.exe")
    try:
        with requests.get(DOWNLOAD, stream=True, timeout=(10, 90)) as response:
            response.raise_for_status()
            size = 0
            with archive.open("wb") as f:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise RuntimeError("ngrok 배포 파일 크기를 확인해 주세요.")
                    f.write(chunk)
        with zipfile.ZipFile(archive) as zipped:
            info = zipped.getinfo("ngrok.exe")
            if info.file_size > 150 * 1024 * 1024:
                raise RuntimeError("ngrok 실행 파일 크기를 확인해 주세요.")
            with zipped.open(info) as source, part.open("wb") as dest:
                shutil.copyfileobj(source, dest)
        verify_signature(part)
        os.replace(part, target)
    finally:
        archive.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    return target


def publish_address(address, output):
    address = normalize_url(address)
    output.mkdir(parents=True, exist_ok=True)
    textfile = output / "접속주소.txt"
    qrfile = output / "접속QR.png"
    same = (
        textfile.is_file() and textfile.read_text(encoding="utf-8").strip() == address
    )
    if not same or not qrfile.is_file():
        import qrcode

        temp = output / "접속QR.tmp.png"
        try:
            qrcode.make(address).save(temp)
            os.replace(temp, qrfile)
        finally:
            temp.unlink(missing_ok=True)
    if not same:
        textfile.write_text(address + "\n", encoding="utf-8")
    return qrfile


def child_environment(token):
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET"))
    }
    env["NGROK_AUTHTOKEN"] = token
    return env


def configure():
    print("무료 가입: https://dashboard.ngrok.com/signup")
    print("Authtoken: https://dashboard.ngrok.com/authtokens")
    print("고정 도메인: https://dashboard.ngrok.com/domains")
    token = getpass.getpass("ngrok Authtoken (화면에 표시되지 않음): ")
    print("Domains에 배정된 주소를 입력하세요. 임의의 .com 주소는 사용할 수 없습니다.")
    address = normalize_url(input("ngrok 고정 도메인 (예: example.ngrok-free.app): "))
    path = save_settings(token, address)
    qr = publish_address(address, RUNTIME_OUTPUT_DIR / "web")
    print("설정 저장:", path)
    print("고정 접속 주소:", address)
    print("QR:", qr)
    print("웹_종료.cmd로 종료한 뒤 웹_실행.cmd → 외부접속_실행.cmd 순서로 실행하세요.")


def tunnel_already_running(pidfile):
    if not pidfile.is_file():
        return False
    pid = int(json.loads(pidfile.read_text(encoding="utf-8"))["pid"])
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return bool(result.stdout.strip())
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--configure", action="store_true")
    p.add_argument("--download-only", action="store_true")
    args = p.parse_args(argv)
    load_project_env()
    if args.configure:
        configure()
        return 0
    if args.download_only:
        print("ngrok ready:", get_binary())
        return 0
    token, address = load_settings()
    if not token or not address:
        raise RuntimeError("ngrok_설정.cmd를 먼저 실행해 주세요.")
    pidfile = RUNTIME_CACHE_DIR / "web_tunnel.json"
    if tunnel_already_running(pidfile):
        raise RuntimeError(
            "외부 접속이 이미 실행 중입니다. 웹_종료.cmd로 종료한 뒤 다시 실행해 주세요."
        )
    base = f"http://127.0.0.1:{args.port}"
    response = requests.get(base + "/api/health", timeout=5)
    response.raise_for_status()
    if response.json().get("model_version") != "V5.2.1":
        raise RuntimeError("웹_실행.cmd를 먼저 실행해 주세요.")
    accepted = requests.get(
        base + "/api/health",
        headers={"Host": address.removeprefix("https://")},
        timeout=5,
    )
    if accepted.status_code != 200:
        raise RuntimeError(
            "고정 도메인 적용을 위해 웹_종료.cmd → 웹_실행.cmd를 실행해 주세요."
        )
    binary = get_binary()
    config = CACHE_ROOT / "tools/ngrok-agent.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {"version": "3", "agent": {"web_addr": False, "update_check": False}}
        ),
        encoding="utf-8",
    )
    print("ngrok 고정 주소로 연결합니다. 종료: Ctrl+C", flush=True)
    try:
        child = subprocess.Popen(
            [
                str(binary),
                "http",
                base,
                "--url",
                address,
                "--inspect=false",
                "--config",
                str(config),
                "--log=stdout",
                "--log-format=json",
                "--log-level=info",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment(token),
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) in (4556, 577, 1260):
            raise RuntimeError(
                "Windows에서 ngrok 실행을 차단했습니다. Microsoft Store에서 ngrok을 설치한 뒤 실행 창을 다시 여세요. https://ngrok.com/download/windows"
            ) from None
        raise
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(
        json.dumps(
            dict(pid=child.pid, root=str(ROOT), binary=str(binary), provider="ngrok")
        ),
        encoding="utf-8",
    )
    ready = False
    try:
        for line in child.stdout:
            try:
                record = json.loads(line)
            except ValueError:
                record = {}
            if (
                record.get("url") == address
                and record.get("msg") == "started tunnel"
                and not ready
            ):
                ready = True
                qr = publish_address(address, RUNTIME_OUTPUT_DIR / "web")
                print("휴대폰·노트북 접속:", address, flush=True)
                print("QR:", qr, flush=True)
            codes = set(re.findall(r"ERR_NGROK_\d+", line))
            if codes:
                print(
                    "ngrok 오류:",
                    ", ".join(sorted(codes)),
                    "인증·도메인·무료 사용량을 확인해 주세요.",
                    flush=True,
                )
        result = child.wait()
        if not ready:
            raise RuntimeError(
                "ngrok 연결을 확인하지 못했습니다. Authtoken·배정된 도메인·네트워크를 확인해 주세요."
            )
        return result
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if (
            pidfile.is_file()
            and json.loads(pidfile.read_text(encoding="utf-8")).get("pid") == child.pid
        ):
            pidfile.unlink()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except (
        requests.RequestException,
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as exc:
        print(
            str(exc)
            if isinstance(exc, (RuntimeError, ValueError))
            else "ngrok 설정·네트워크·공식 배포 파일을 확인해 주세요.",
            file=sys.stderr,
        )
        raise SystemExit(1)
