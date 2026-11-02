# OSM 보행 자료

출처: © OpenStreetMap contributors
저작권·라이선스 안내: https://www.openstreetmap.org/copyright
수집: Overpass API, 2026-11-02
범위: 서울 주변 12개 겹침 타일. 정확한 범위와 체크섬은 tiles_manifest.json 참고.

tiles/*.json.gz는 공개 OSM 응답의 압축 체크포인트입니다. 개인 이용이력·인증키·실시간 재고는 포함하지 않습니다.

실행에 필요한 SQLite 도보망은 Git에서 제외합니다. 포함된 원본만 사용해 네트워크 호출 없이 생성합니다.

```powershell
python scripts/build_osm_walking.py --rebuild-cached
```

완료 후 data/walking/seoul_walk.sqlite가 생성됩니다. 이후 서버를 재시작합니다.
