# 파일 기반 대중교통 자료

기본 실행은 `notebooks/02_시간표최적화.ipynb`입니다. 시간표·좌표 준비에는 API 키가 필요 없습니다.

| 로컬 원본 | 출처·기준 |
|---|---|
| files/subway_timetable.csv | 공공데이터포털 15098251, 2026-09-01 기준 시간표, 423,107행 |
| files/bus_coordinates.xlsx | 서울시 OA-15067, 2026-09-02 게시본, 11,236행 |
| files/bus_coordinates_supplement.csv | 공식 TOPIS 노드정보 중 사용 중인 정류장 50,918개, 정확한 ID로만 보완 |
| files/subway_coordinates.csv | 서울교통공사 15099316, 1~8호선 좌표, 276행 |
| files/subway_line9_coordinates.csv | 국가철도공단 15041335, 9호선 좌표, 38행 |
| files/national_rail_stations.xlsx | 철도 데이터 포털 id=32, 2026-06-30 게시본, 1,099행 |
| bus_raw.zip | 기존에 받은 버스 첫차·막차·요일별 배차·운행시간. 실제 형식은 UTF-8 CSV |
| bus_route_nodes.dat | 기존에 받은 노선별 정류장 순서·거리 |

`files/sources.json`에 출처 URL, 다운로드 주소, 파일 크기, SHA-256을 기록했습니다. 다운로드 도구는 `scripts/download_transit_files.py`이며 모델·인증키를 사용하지 않습니다. 이미 있는 파일은 다시 받지 않습니다. 새 게시본으로 갱신할 경우 기존 원본을 별도 보관한 후 새 파일을 받으십시오.

`transit_files.prepare_transit_files()`가 원본의 ID·호선·역명을 연결하여 `prepared.json`을 만듭니다. 4호선의 옛 이름 신길온천은 현재 이름 능길로 연결하며 좌표를 임의 추정하지 않습니다. 파일의 1~9호선 시간표가 서울 전역의 모든 철도 노선을 포함한다는 뜻은 아닙니다. 추가 7개 노선의 원본·방식은 `extended_rail`에 있습니다.

평일·토요일·공휴일을 구분합니다. 원본에 공식 종료일이 없더라도 기준일로부터 120일 뒤에는 최신 자료 재검토를 요구합니다. 이는 프로젝트 안전정책이지 운영사가 보장한 만료일이 아닙니다. 같은 파일의 재다운로드만으로 기준일을 갱신하지 않습니다.

버스는 요일별 첫차·막차·배차로 운행편을 생성하고 전체 운행시간을 구간 거리 비율로 배분합니다. `링크거리누계`는 단조 증가하지 않아 `segment`로 해석합니다. 0 운행시간·좌표 누락·잘못된 순서의 노선은 제외합니다. 2026-10-20 12:00부터 180분 구성에서 409개 노선에 구간이 생성됩니다. 전체 버스를 모두 포함하는 것은 아닙니다.

버스 복원 시각은 공식 정류장별 확정 시각이 아닙니다. 이 설명은 개발 문서에 유지하며 경로 화면에 별도 표식은 추가하지 않습니다.

`preparation_report.json`에는 좌표 연결 범위, 결과 `network_metadata.bus_report`에는 누락 ID·제외 노선 예시가 남습니다. 큰 `network.json`은 `save_network=True`일 때만 저장합니다. 기존 API 수집기는 호환용이며 기본 노트북에서는 호출하지 않습니다. 서울시 인증키는 현재 따릉이 재고 조회에만 필요합니다.

보완 좌표의 출처 해시는 `bus_coordinates_supplement.provenance.json`에 있습니다. 새 공식 노드 CSV에서 다시 추출할 때는 `python scripts/prepare_bus_supplement.py 원본CSV경로`를 사용합니다. 팀원 실행에는 이 원본 CSV나 연구 백업 폴더가 필요하지 않습니다.
