# 따릉이·대중교통 경로 탐색

따릉이·버스·지하철을 연결해 경로와 이동시간을 안내합니다. PC·휴대폰 웹, Python 함수, 실행 노트북을 제공합니다.

- 이동시간: V5.2.1 P50과 OSM 자전거 경로
- 재고: 대여소 도착시각의 예측 재고
- 개인화: 스크린샷 이용내역을 등록하고 배율·고정 시간을 보정
- 탐색 모드: 통합 / 대중교통(버스·지하철) / 따릉이

## 실행

Windows x64·Python 3.11~3.13을 사용합니다.

1. 최초 한 번 `설치.cmd` → `웹_설치.cmd` → `OCR_설치.cmd`를 실행합니다.
2. `.env.example`을 `.env`로 복사하고 카카오·기상청·서울시 인증키를 입력합니다.
3. 외부 접속을 사용할 경우 ngrok 무료 계정의 Authtoken과 배정 도메인을 `ngrok_설정.cmd`에 입력합니다.
4. `웹_실행.cmd`를 실행합니다. PC에서는 http://127.0.0.1:8765 로 접속합니다. HTML 파일을 직접 열지 않습니다.
5. `외부접속_실행.cmd`를 실행하면 휴대폰에서 사용할 고정 HTTPS 주소와 QR을 확인할 수 있습니다.

종료는 `웹_종료.cmd`입니다. 외부 접속 중에는 PC·인터넷·두 실행 프로그램을 유지합니다. 같은 ngrok 도메인을 사용하면 주소와 QR이 유지됩니다.

Python 연결은 `examples/python_usage.py`, 노트북은 `노트북_실행.cmd`에서 시작합니다.

## 폴더

| 폴더 | 내용 |
|---|---|
| `src/` | 경로 탐색·모델 연결·사진 인식·개인화·웹 서버 |
| `web/` | PC·휴대폰 화면과 이미지 |
| `models/` | 이동시간·재고 예측 모델과 통계 |
| `data/` | 시간표·도로 형상·대여소 매핑·과거 날씨 |
| `notebooks/`, `examples/` | 실행 노트북·Python 예제 |
| `scripts/`, `tests/` | 설치·자료 준비·점검·자동 검사 |
| `docs/`, `config/` | 사용법·검증 범위·프로젝트 정보 |

개인 DB·캐시·로그·QR은 `cache/`, `outputs/`에 생성됩니다. 형제 폴더 `<프로젝트 폴더명>_local`에 저장 위치가 있으면 그곳을 사용합니다. 예를 들어 `ttareungil-project`의 외부 저장 폴더는 `ttareungil-project_local`입니다. ngrok 인증 정보도 이 외부 폴더에 보관합니다.

## 점검과 이동

`프로젝트_점검.cmd`로 실행 환경을 검사합니다. 압축 전에는 `프로젝트_점검.cmd --clean`으로 인증키·개인 DB·캐시·노트북 출력 포함 여부를 검사합니다. 설치 후의 `.venv`, `.env`, 개인 자료는 압축에서 제외합니다. 다른 PC에서는 `.venv`를 복사하지 않고 다시 설치합니다.

[웹·휴대폰 사용법](docs/WEB.md) · [Python 사용법](docs/USAGE.md) · [사진 등록](docs/SCREENSHOT_IMPORT.md) · [개인화](docs/PERSONALIZATION.md) · [검증·제한](docs/VALIDATION.md)

화면과 브랜드 이미지는 [ttareungil-project](https://github.com/tricia-ha/ttareungil-project)를 바탕으로 구성했습니다.
