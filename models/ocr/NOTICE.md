# OCR 가중치

EasyOCR의 공식 배포 가중치입니다. `manifest.json`에 원본 다운로드 주소와 MD5·SHA256을 기록했습니다. 원본 스크린샷이나 개인 이용내역은 포함하지 않습니다.

- `craft_mlt_25k.pth`: 글자 영역 검출. [CRAFT](https://github.com/clovaai/CRAFT-pytorch), MIT License.
- `korean_g2.pth`: 한국어·영어 글자 인식. [EasyOCR](https://github.com/JaidedAI/EasyOCR), Apache License 2.0.

라이선스 원문은 이 폴더의 `LICENSE-CRAFT.txt`, `LICENSE-EasyOCR.txt`에 있습니다. 실행에는 `OCR_설치.cmd`로 설치하는 EasyOCR·PyTorch가 필요합니다. 가중치는 이 폴더에서 읽으므로 첫 실행 때 다시 다운로드하지 않습니다.
