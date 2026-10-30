# 사진 등록

`OCR_설치.cmd`를 한 번 실행합니다. 최초 사전학습 가중치 다운로드에는 인터넷이 필요하고 이후 글자 인식은 로컬 CPU에서 실행됩니다.

```python
with ProjectService() as service:
    profile_id = service.create_profile()["profile_id"]
    draft = service.preview_screenshot(profile_id, "C:/photos/ride.png")
    print(draft)

# 원본과 추출 결과를 대조한 뒤 등록합니다.
with ProjectService() as service:
    saved = service.confirm_screenshot(profile_id, draft["draft_id"], 0, confirmed=True)
```

`preview_screenshot()`의 `crop=[왼쪽, 위, 오른쪽, 아래]`로 픽셀 범위를 지정할 수 있습니다. PNG/JPEG, 최대 12MB·2천만 화소를 지원합니다. 자전거 번호부터 이용거리까지 한 이용내역이 완전히 보여야 합니다.

글자의 위치와 카드 경계로 다른 내역의 항목이 섞이지 않도록 합니다. `cards`의 출발·도착 번호와 이름, 날짜·시각, 이용시간·거리, 자전거 번호를 확인합니다. `station_mapping`은 매핑된 대여소 이름과 불일치·없는 번호·수정 후보를 보여줍니다.

초안은 같은 ID만 사용할 수 있고 30분 뒤 만료됩니다. 번호·이름 불일치, 없는 대여소, 필수 항목 누락, 미래 반납, 시간 불일치, 같은 대여소 왕복은 등록하지 않습니다. 수정은 `confirm_screenshot(..., edits={"start_station_id":"5756", "start_station_name":"극동아파트"})`처럼 지정하며 다시 검증합니다. 없는 번호를 자동으로 다른 대여소에 대입하지 않습니다.

등록할 때 대여 시각의 Open-Meteo 과거 날씨와 V5.2.1 기본 P50을 계산합니다. 과거 날씨 조회에 실패하면 기본적으로 등록을 중단합니다. `allow_missing_weather=True`를 명시한 경우에만 날씨 결측 상태로 진행하고 출처를 남깁니다.

동일 ID·출발·도착·대여시각의 중복 기록은 다시 반영하지 않습니다. 원본 사진은 저장하지 않고 추출값·확인값·사진 해시·검증 결과·P50·날씨 출처를 DB에 보관합니다. 사진의 위조 여부까지 검증하는 기능은 아닙니다.

일부 항목을 읽지 못해도 `manual_reviewable_count`가 양수이고 `draft_id`가 있으면 직접 수정할 수 있습니다. 원본에서 빠진 필드를 모두 확인해 `confirm_screenshot(..., manual=True, edits={...}, confirmed=True)`로 전달합니다. 웹에서는 `직접 입력·수정`을 사용합니다. 직접 수정 후에도 같은 대여소·시각·거리 검증이 적용됩니다.
