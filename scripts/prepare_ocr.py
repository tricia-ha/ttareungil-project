"""Prepare OCR with bundled weights, downloading only for legacy installations."""

from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from ocr_dependencies import load_easyocr
from shared_assets import ocr_model_options

easyocr = load_easyocr()
easyocr.Reader(
    ["ko", "en"],
    gpu=False,
    **ocr_model_options(),
    verbose=False,
)
print("한국어 OCR 준비 완료")
