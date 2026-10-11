# 따릉길 통합 프로젝트

기존의 따릉길 프런트엔드 아이디어를 실제 대여소 목록과 Bicycle ETA V5.2.1 예측 API에 연결한 로컬 실행용 프로젝트입니다. HTML/CSS 디자인을 기반으로 하되, 대여소 선택·경로 예측·지도 표시를 연결했습니다.

## 구현된 기능
- `index.html`: 기존 초록색 UI 콘셉트를 유지한 웹 화면
- 실제 대여소 목록: `GET /v1/stations` (패키지 데이터 카탈로그, 기준 2026-06)
- 실제 도로 경로 및 이동시간: `POST /v1/eta` (Bicycle ETA V5.2.1, OSM/SRTM 오프라인 도로망)
- P50/P80/P90 예상 시간, 거리, 상승·하강, 신호·교차로 표시
- Leaflet/OpenStreetMap을 이용한 지도와 예측 경로 표시 (브라우저 인터넷 연결 필요)
- 별도 Inventory 고갈 위험 API: `POST /v1/inventory-risk`

## Windows에서 실행

1. ZIP 압축을 푼 뒤 `따릉길_통합프로젝트` 폴더를 유지합니다. `package`와 `model` 폴더의 상대 위치를 바꾸지 마세요.
2. Python 3.11 64비트를 권장합니다.
3. PowerShell에서 프로젝트 폴더로 이동한 뒤 아래 명령을 실행합니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m uvicorn api:app --app-dir package --host 127.0.0.1 --port 8000
```

4. 브라우저에서 `http://127.0.0.1:8000` 을 엽니다. 모델과 오프라인 도로망을 처음 불러오는 데 시간이 걸릴 수 있습니다.
5. API 확인은 `http://127.0.0.1:8000/docs`, 상태 확인은 `http://127.0.0.1:8000/health` 입니다.

## 기상청 예보
기본값은 외부 기상청 API를 호출하지 않고 ETA 모델의 기본 입력으로 예측합니다. 기상청 예보를 사용하려면 기상청 API Hub 인증키를 환경 변수로 설정하세요.

```powershell
$env:KMA_API_KEY="본인의_인증키"
```

그 다음 화면의 날씨 선택을 `기상청 예보`로 바꾸세요. 키가 없거나 조회가 실패하면 현재 화면 설정은 대체 예측으로 이어집니다.

## Inventory 고갈 위험 모델 주의사항
`02_AI예측.zip`의 모델/시간별 수요 데이터도 포함하고 `/v1/inventory-risk`로 노출했습니다. 하지만 이 모델은 ETA 모델과 대여소 ID 체계가 다릅니다. 제공된 파일에는 `station_crosswalk.csv`가 없으며, 실시간 잔여 자전거 API도 포함되어 있지 않습니다. 따라서 웹 화면에서는 ETA 대여소 ID를 고갈 위험 모델에 자동 연결하지 않았습니다.

Inventory API 요청 예시:

```json
{
  "station_id": "ST-ID-모델에_있는_ID",
  "current_bikes": 3,
  "eta_minutes": 15,
  "as_of": "2026-10-11T18:00:00+09:00",
  "epsilon": 0.1
}
```

실제 `station_id`는 `inventory_model/03_모델/station_activity.csv` 및 `weekly_rates.csv`에 있는 ID여야 합니다. `current_bikes`는 실제 실시간 재고가 아니라 사용자가 제공한 관측값/시나리오 값이어야 하며, 위험 확률은 모델 가정에 기반한 미보정 추정치입니다.

## 한계
- 이 프로젝트는 로컬 개발·시연용입니다. 인터넷에 공개 배포하기 전에는 인증, 요청 제한, 운영 로그, 비밀키 관리, 오류 처리, 개인정보 정책 등을 보완해야 합니다.
- 대여소 카탈로그는 패키지에 포함된 2026년 6월 기준 자료입니다. 실시간 대여 가능 자전거 수는 제공되지 않습니다.
- 지도 타일과 Leaflet은 CDN을 사용하므로 지도 표시에는 인터넷 연결이 필요합니다.
- 이동시간은 예측치이지 실제 시간 보장이 아닙니다.
