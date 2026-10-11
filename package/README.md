# 따릉이 이동시간 예측 V5.2.1

출발 대여소, 도착 대여소, 출발 시각을 입력하면 OSM 추천 경로와 P50·P80·P90 이동시간을 반환합니다. OSM이 경로를 만들고 신경망이 해당 경로의 이동시간을 예측합니다.

## 구성

```text
bicycle_eta_v5_2_1/
├─ package/   실행 코드, 설명서, 현재 대여소 목록
└─ model/     모델, 전처리 통계, OSM 그래프
```

압축을 푼 뒤 두 폴더의 상대 위치를 바꾸지 마세요. 약 9GB의 전체 경로 캐시는 제외했습니다. 요청된 OD만 `package/cache`에 저장하므로 처음 조회한 OD보다 다음 요청이 빠릅니다.

## 설치

Windows PowerShell에서 `package` 폴더로 이동하여 실행합니다.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python verify_bundle.py
```

Python 3.10~3.12 64비트를 권장하며 GPU는 필요하지 않습니다.

## 기상청 인증키

기상청 API허브의 `기상청_단기예보 조회서비스(기상청API허브 연계)` 인증키를 사용합니다.

```powershell
$env:KMA_API_KEY="본인의_인증키"
```

인증키는 코드나 ZIP에 넣지 마세요. 같은 예보는 `package/cache/kma_forecast`에서 재사용합니다.

## Python에서 실행

기상청 예보를 적용합니다. 출발 시각은 단기예보 범위 안이어야 합니다.

```python
from eta_service import BicycleETAService

with BicycleETAService() as service:
    result = service.predict_with_kma(
        "2106", "2144", "2026-10-07T18:00:00"
    )
    print(result["eta"])
    print(result["route"])
```

기상청 호출 없이 실행하려면 `service.predict("2106", "2144", 출발시각)`을 사용합니다.

## 웹 API로 실행

```powershell
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

`http://127.0.0.1:8000/docs`에서 요청을 시험할 수 있습니다.

`POST /v1/eta` 예시:

```json
{
  "start_station_id": "2106",
  "end_station_id": "2144",
  "departure": "2026-10-07T18:00:00",
  "use_kma": true,
  "allow_weather_fallback": true
}
```

- `GET /health`: 모델 상태
- `GET /v1/stations`: 현재 대여소 검색
- `POST /v1/eta`: 단일 경로 예측
- `POST /v1/eta/batch`: 최대 100개 경로 예측

필수 입력은 출발 대여소, 도착 대여소, 출발 시각입니다. 기상청 조회가 실패해도 `allow_weather_fallback=true`이면 날씨 결측 상태로 예측하고 원인을 `model.weather_error`에 표시합니다.

## 주요 출력

- `eta.p50_min`: 대표 이동시간
- `eta.p80_min`, `eta.p90_min`: 여유를 둔 이동시간
- `route.geometry`: 지도에 표시할 경로 좌표
- `route.distance_m`: 추천 경로 거리
- `model.station_fallback_used`: 학습 당시 없던 대여소 포함 여부
- `model.od_history_count`: 동일 OD의 학습 이력 수

경로 최적화에서는 후보 OD를 `/v1/eta/batch`로 보내고 `eta.p50_min`, `eta.p80_min`, `route.geometry`를 사용하면 됩니다.

## 모델 범위와 평가

- 현재 운영 대여소: 2,789개
- 모델 사전 포함 대여소: 2,739개
- 경로 기반 보완 예측 대여소: 50개
- 레저성·과도한 우회 의심 기록 제거 후 Test: 4,853,190건
- 전체 P50 MAE: 5.915분
- 중앙 절대오차: 1.706분
- P80 포함률: 79.78%
- P90 포함률: 89.88%
- 처음 본 대여소 포함 Test P50 MAE: 7.174분

`station_fallback_used=true`이면 P50과 P80을 함께 사용하는 것을 권장합니다. 먼 미래는 단기예보 범위 밖이므로 실제 출발 시점에 가까워졌을 때 최신 예보로 다시 계산해야 합니다.

## 파일

- `api.py`: 웹 API
- `eta_service.py`: 모델·경로 통합 추론
- `kma_weather_forecast_provider.py`: 기상청 예보
- `example_usage.py`: 실행 예제
- `verify_bundle.py`: 설치 확인
- `data/stations_current.csv`: 현재 대여소
- `../model`: 모델과 OSM 그래프
