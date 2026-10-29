"""Run the PC server with bounded concurrent routing and one shared model."""

from pathlib import Path
import argparse, json, os, socket, sys, webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true")
    p.add_argument("--db", type=Path, help="Optional isolated test database")
    args = p.parse_args()
    from web_capacity import configure_process

    configure_process()
    try:
        from web_runtime import prepare

        prepare()
        import uvicorn
        from web_server import create_app
        from project_paths import RUNTIME_CACHE_DIR
    except ImportError:
        print("웹_설치.cmd를 먼저 실행해 주세요.", file=sys.stderr)
        return 1
    pidfile = RUNTIME_CACHE_DIR / "web_server.json"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    # A listening socket check prevents replacing a running server's PID record.
    with socket.socket() as test:
        try:
            test.bind((args.host, args.port))
        except OSError:
            print("포트가 이미 사용 중입니다. 기존 서버 또는 포트를 확인해 주세요.")
            return 1
    pidfile.write_text(
        json.dumps(dict(pid=os.getpid(), root=str(ROOT), port=args.port)),
        encoding="utf-8",
    )
    print(f"PC: http://127.0.0.1:{args.port}", flush=True)
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            print(f"같은 네트워크: http://{address}:{args.port}", flush=True)
    except OSError:
        pass
    print("외부 접속은 외부접속_실행.cmd를 실행하세요. 종료: Ctrl+C", flush=True)
    if args.open:
        import threading

        threading.Timer(
            2, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")
        ).start()
    try:
        from project_service import ProjectService

        db_path = args.db or RUNTIME_CACHE_DIR / "web_personalization.sqlite"
        uvicorn.run(
            create_app(lambda: ProjectService(db_path=db_path)),
            host=args.host,
            port=args.port,
            workers=1,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1,::1",
            limit_concurrency=30,
            timeout_keep_alive=5,
            access_log=False,
        )
    finally:
        if pidfile.exists():
            data = json.loads(pidfile.read_text(encoding="utf-8"))
            if data.get("pid") == os.getpid():
                pidfile.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
