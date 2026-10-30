# OSM 대여소 경로

공개 대여소 좌표 사이에서 기존 OSM 도로 그래프로 계산했던 경로입니다. 자전거 경로의 도로 형상과 거리·경사 등의 특징만 포함합니다. 실제 이용자의 GPS 기록, 개인 정보, 조회 시각, 카카오 API 응답은 포함하지 않습니다.

`manifest.json`에 그래프 식별값, 경로 수, 크기와 SHA256을 기록했습니다. `osm_station_routes.sqlite.gz`는 이동시간 모델의 경로 캐시가 없고 그래프 식별값이 일치할 때만 초기 자료로 사용합니다. 기존 캐시가 있으면 덮어쓰지 않으며, 포함되지 않은 경로는 기존 방식으로 계산합니다.

© [OpenStreetMap contributors](https://www.openstreetmap.org/copyright). OSM에서 파생한 도로 경로 데이터는 [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/)으로 제공합니다. 배포 시 출처와 라이선스 표시를 유지해야 합니다. 이 조건은 경로 데이터에 적용됩니다.
