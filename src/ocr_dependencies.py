"""Prefer the active environment; never load incompatible development wheels."""

import importlib
import sys
from pathlib import Path
from project_paths import PROJECT_ROOT, private_root


def compatible_development_wheels(folder):
    from packaging.tags import sys_tags, parse_tag

    supported = set(sys_tags())
    wheels = list(Path(folder).glob("*.dist-info/WHEEL"))
    if not wheels:
        return False
    for wheel in wheels:
        tags = set()
        for line in wheel.read_text(encoding="utf-8").splitlines():
            if line.startswith("Tag: "):
                tags.update(parse_tag(line[5:].strip()))
        if not tags.intersection(supported):
            return False
    return True


def load_easyocr():
    try:
        return importlib.import_module("easyocr")
    except ImportError as exc:
        if getattr(exc, "name", None) != "easyocr":
            raise RuntimeError(
                "OCR 의존성이 불완전합니다. 현재 Python 환경에서 OCR_설치.cmd를 실행해 주세요."
            ) from None
    local = PROJECT_ROOT / ".ocr_deps"
    if not local.is_dir():
        local = private_root() / ".ocr_deps"
    if not local.is_dir() or not compatible_development_wheels(local):
        raise RuntimeError(
            "이 Python 환경에 맞는 OCR 설치가 필요합니다. OCR_설치.cmd를 실행해 주세요."
        )
    sys.path.insert(0, str(local))
    try:
        return importlib.import_module("easyocr")
    except ImportError:
        raise RuntimeError(
            "OCR 설치가 필요합니다. OCR_설치.cmd를 실행해 주세요."
        ) from None
