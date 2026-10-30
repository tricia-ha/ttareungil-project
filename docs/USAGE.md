# Python 사용법

프로젝트 폴더에서 설치한 Python 환경으로 실행합니다. 노트북에서는 `01_기능실행.ipynb`를 사용합니다.

```python
from pathlib import Path
import sys
ROOT = Path.cwd()  # bicycle 폴더
sys.path.insert(0, str(ROOT / "src"))
from project_service import ProjectService

origin = {"name": "서울역", "latitude": 37.5568, "longitude": 126.9723}
destination = {"name": "여의도 한강공원", "latitude": 37.5284, "longitude": 126.9326}
with ProjectService() as service:
    result = service.find_routes(origin, destination, departure="auto", mode="transit")
    for route in result["routes"]:
        print(route["total_minutes"], route["legs"])
```

`mode`는 `integrated`, `transit`, `bicycle`입니다. `transit`은 버스와 지하철 모두 허용하고 따릉이를 제외합니다. 출발시각은 `auto` 또는 시간대가 포함된 미래 시각입니다. `auto`는 모델 준비 후 현재로부터 5분 뒤로 설정합니다.

선택 인자는 `profile_id`, `top_k`(기본 3), `max_seconds`(탐색 단계 기본 30초, 자료 준비시간 별도), `max_kakao_calls`(기본 80), `use_kma`, `use_inventory`, `refresh_inventory`입니다. 대중교통 전용에서는 날씨·재고 옵션을 사용하지 않습니다. 같은 `ProjectService` 안에서 반복 실행하면 따릉이 모델을 재사용합니다.

## 도보 조회와 호출 한도

웹의 탐색당 신규 도보 호출 한도는 기본 20회, 로컬 일일 한도는 1,000회입니다. Python 함수의 기본 한도는 80회입니다.

- 세 모드 모두 좌표·방향·유효기간이 일치하는 캐시를 먼저 재사용합니다. 유효한 경로 없음 응답도 재조회하지 않습니다.
- 출발·도착과 따릉이·지하철·버스 연결을 번갈아 준비하고, 나머지 호출은 탐색 중 필요한 연결에 사용합니다.
- 도착지까지의 마지막 도보와 이미 접근한 대여소의 따릉이 예측을 우선 평가합니다. 거리 범위 내의 다른 후보도 유지합니다.
- 따릉이 전용에서 허용 거리 내 대여·반납 조합이 하나도 없으면 도보 조회 없이 종료합니다. 현재 기본 따릉이 구간 한도는 직선거리 7km입니다.
- 재고 예측 범위를 벗어나면 자전거 대여 연결을 비활성화합니다. 대여소가 유효한 도보 경유지인 경우는 유지합니다.
- 설정한 이동시간 범위가 끝난 상태와, 가능한 후속 이동이 전혀 없는 지점에 대한 새 조회는 생략합니다. 후속 도보·대중교통·자전거 연결이 불확실하면 후보를 유지합니다. 서로끼리만 연결된 작은 후보 묶음도 확실히 출구가 없을 때만 제외합니다.
- 호출 한도에 도달하면 실패가 확정된 새 조회는 반복하지 않고 캐시를 확인합니다. 남은 캐시·시간표 작업이 끝나면 30초 이전에도 종료합니다.
- 추가 도보 조회가 실패해도 이미 확보한 경로는 `partial` 결과로 반환합니다. 오류 내용은 인증정보를 제외한 진단으로 남습니다.
- 탐색 중 도보 API 대기는 남은 탐색시간으로 제한합니다. 자료 준비시간은 탐색시간과 별도입니다.

호출·시간이 제한되면 후보를 모두 평가하지 못할 수 있습니다. 저장된 연결은 보존하지만, 새 출발·도착 조합에서 동일한 결과나 전체 최단 경로를 보장하지는 않습니다.

Python 함수의 `on_routes`에 콜백을 지정하면 완성된 중간 경로를 받을 수 있습니다. 예: `service.find_routes(origin, destination, on_routes=lambda value: print(value["routes"]))`. 콜백은 Python 인자이며 JSON 입력에는 포함하지 않습니다. 최종 결과는 기존처럼 함수 반환값으로 받습니다.

## 반환값

- `routes`: 추천 경로. `total_minutes`는 전체 시간, `legs`는 수단·구간·시각·경로 좌표입니다.
- `mixed_routes`: 탐색 중 확보한 따릉이·대중교통 혼합 경로
- `status`, `search_complete`, `termination_reason`: 탐색 완료 여부와 종료 이유
- `data_gaps`: 확보하지 못한 시간표·좌표 자료 범위
- `personalization`, `model_versions`, `api_usage`: 개인 배율·고정 시간 보정·사용 모델·호출 현황

`partial`은 한도나 시간제한 등으로 탐색을 마치지 못한 상태입니다. 결과가 있어도 전체 후보의 최단 경로를 보장하지 않습니다.

## 개인화

```python
with ProjectService() as service:
    profile_id = service.create_profile()["profile_id"]
    print(service.get_profile(profile_id))
    print(service.list_rides(profile_id))
    result = service.find_routes(origin, destination, mode="integrated", profile_id=profile_id)
```

처음 발급한 ID는 배율 1·고정 보정 0분으로 시작합니다. 사진 등록 후 같은 ID로 탐색하면 보정됩니다. `delete_ride(profile_id, receipt)`는 이력 한 건을, `delete_profile(profile_id)`은 해당 ID와 이력을 삭제합니다. ID는 본인의 이력을 사용하는 키이므로 보관해야 합니다.

테스트 시 `ProjectService(db_path="임시_DB_경로.sqlite")`로 실제 이력과 분리할 수 있습니다. 사진 경로와 DB의 상대 경로 기준이 혼동되지 않도록 절대 경로 사용을 권장합니다.

## 환경 확인

- `프로젝트_점검.cmd`: 필수 자료와 패키지 버전·실제 import 검사
- `프로젝트_점검.cmd --clean`: 압축 전 개인 자료·캐시·실행 출력 검사
- 폴더 위치·이름 변경 후에는 `.venv`를 재생성합니다. 설치 폴더에 쓰기 권한이 있어야 합니다.
- Python 3.10 이하, 3.14 이상, 32비트 환경은 자동 설치에서 안내 후 중단합니다.
- 인증키는 새 위치의 `.env`에 입력하며 키별 서비스 사용 권한이 필요합니다.
- API 한도·자료 미확보는 설치 오류와 별개입니다. `partial`, `termination_reason`, `data_gaps`를 확인합니다.
