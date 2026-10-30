"""Public route progress labels; callbacks cannot interrupt route computation."""

STAGES = {
    "queued": (
        "탐색을 시작할 차례를 기다리고 있습니다.",
        "다른 작업이나 PC 자원 상황에 따라 대기할 수 있습니다.",
    ),
    "starting": ("경로 탐색을 준비하고 있습니다.", ""),
    "model": (
        "따릉이 이동시간 모델과 도로망을 준비하고 있습니다.",
        "처음 실행할 때는 준비 시간이 더 걸릴 수 있습니다.",
    ),
    "timetable": ("출발 시각에 맞는 버스·지하철 시간표를 확인하고 있습니다.", ""),
    "inventory": (
        "따릉이 재고 자료를 확인하고 있습니다.",
        "대여소에 도착할 시각의 재고를 판단하기 위한 준비입니다.",
    ),
    "network": (
        "출발·도착지와 환승 지점을 연결하고 있습니다.",
        "주변 교통수단과 도보 연결을 확인합니다.",
    ),
    "searching": (
        "이동시간과 환승을 비교하며 경로를 찾고 있습니다.",
        "탐색 제한시간과 별도로 준비·결과 정리 시간이 걸릴 수 있습니다.",
    ),
    "searching_weather": (
        "날씨를 반영한 이동시간과 환승 경로를 비교하고 있습니다.",
        "필요한 예보를 조회하거나 저장된 예보를 사용합니다.",
    ),
    "results": ("찾은 경로를 지도에 표시할 수 있도록 정리하고 있습니다.", ""),
    "done": ("탐색 결과가 준비되었습니다.", ""),
    "error": ("경로 탐색을 완료하지 못했습니다.", ""),
}


def notify_progress(callback, stage):
    if callback is not None and stage in STAGES:
        try:
            callback(stage)
        except Exception:
            # Status display must not change results or discard valid routes.
            pass
