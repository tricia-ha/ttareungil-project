"""Account-bound URL; credentials stay outside the project directory."""

from urllib.parse import urlsplit
import json, os, re
from project_paths import private_root


def settings_path():
    return private_root() / "web_ngrok.json"


def normalize_url(value):
    value = value.strip()
    p = urlsplit(value if "://" in value else "https://" + value)
    host = (p.hostname or "").lower()
    if (
        p.scheme != "https"
        or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:ngrok-free\.(?:app|dev)|ngrok\.(?:app|dev))",
            host,
        )
        or p.netloc.lower() != host
        or p.path not in ("", "/")
        or p.query
        or p.fragment
    ):
        raise ValueError("ngrok Domains에 표시된 HTTPS 도메인만 입력해 주세요.")
    return "https://" + host


def load_settings():
    path = settings_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key, ""), str) for key in ("authtoken", "url")
    ):
        raise ValueError("ngrok_설정.cmd로 설정을 다시 저장해 주세요.")
    token = os.getenv("NGROK_AUTHTOKEN", "").strip() or data.get("authtoken", "")
    value = os.getenv("NGROK_DOMAIN", "").strip() or data.get("url", "")
    return token, normalize_url(value) if value else ""


def save_settings(token, url):
    token = token.strip()
    url = normalize_url(url)
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", token):
        raise ValueError("Your Authtoken의 인증 문자열만 입력해 주세요.")
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            os.chmod(temp, 0o600)
            json.dump(dict(authtoken=token, url=url), f, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def configured_host():
    try:
        _, url = load_settings()
        return urlsplit(url).hostname if url else None
    except (ValueError, OSError, TypeError):
        return None
