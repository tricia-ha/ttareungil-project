"""Download OCR weights once; later recognition is local."""

from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
from ocr_dependencies import load_easyocr
from project_paths import CACHE_ROOT

easyocr = load_easyocr()
easyocr.Reader(
    ["ko", "en"],
    gpu=False,
    model_storage_directory=str(CACHE_ROOT / "ocr_models"),
    verbose=False,
)
print("한국어 OCR 준비 완료")
