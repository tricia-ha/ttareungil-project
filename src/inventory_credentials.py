"""환경변수 또는 이 Windows 사용자에 묶인 로컬 DPAPI 인증키. 키를 로그에 쓰지 않는다."""

import os, ctypes
from pathlib import Path


def get_api_key():
    value = os.environ.get("SEOUL_API_KEY", "").strip()
    if value:
        return value
    local = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not local:
        return ""
    path = Path(local) / "DdareungiAI/seoul_api_key.dpapi"
    if not path.exists():
        return ""

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.c_void_p)]

    try:
        encrypted = bytes.fromhex(path.read_text(encoding="utf-8-sig").strip())
        buffer = ctypes.create_string_buffer(encrypted)
        source = Blob(len(encrypted), ctypes.cast(buffer, ctypes.c_void_p))
        target = Blob()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
        ):
            raise ValueError("DPAPI decrypt failed")
        try:
            return (
                ctypes.string_at(target.data, target.size)
                .decode("utf-16-le")
                .rstrip("\x00")
                .strip()
            )
        finally:
            ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            ctypes.windll.kernel32.LocalFree(target.data)
    except Exception:
        raise ValueError(
            "로컬 인증키를 읽지 못했습니다. 같은 Windows 사용자로 실행하거나 SEOUL_API_KEY를 설정하세요."
        ) from None
