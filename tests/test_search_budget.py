import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from search_budget import workload, select_budget, calibration


REFERENCE = {'profiles': {'all': {'samples': 10, 'low_workload': 100, 'high_workload': 10000}}}


def test_bounds_and_monotonicity():
    plans = [select_budget({'workload': n}, reference=REFERENCE)
             for n in (1, 100, 200, 1000, 5000, 10000, 10**15)]
    assert (plans[0]['max_kakao_calls'], plans[0]['max_seconds']) == (15, 30)
    assert (plans[-1]['max_kakao_calls'], plans[-1]['max_seconds']) == (45, 150)
    assert [p['max_seconds'] for p in plans] == sorted(p['max_seconds'] for p in plans)
    assert [p['max_kakao_calls'] for p in plans] == sorted(p['max_kakao_calls'] for p in plans)
    assert all(15 <= p['max_kakao_calls'] <= 45 and 30 <= p['max_seconds'] <= 150 for p in plans)


def test_more_timetable_events_or_candidates_raise_workload():
    origin = dict(latitude=37.5, longitude=127.0)
    destination = dict(latitude=37.51, longitude=127.01)
    nodes = {'A': origin, 'B': destination}
    stations = [origin, destination]
    small = workload(nodes, stations, origin, destination, 10, 900, 'integrated')
    more_events = workload(nodes, stations, origin, destination, 100, 900, 'integrated')
    more_access = workload(dict(nodes, C=origin, D=destination), stations, origin, destination, 10, 900, 'integrated')
    assert more_events['workload'] > small['workload']
    assert more_access['workload'] > small['workload']
    assert workload(nodes, stations, origin, destination, 10, 900, 'transit')['origin_bicycle_candidates'] == 0


def test_explicit_ceiling_and_shipped_profiles():
    p = select_budget({'workload': 10**15}, call_ceiling=20, time_ceiling=90, reference=REFERENCE)
    assert p['max_kakao_calls'] == 20 and p['max_seconds'] == 90
    p = select_budget({'workload': 10**15}, call_ceiling=100, time_ceiling=None, reference=REFERENCE)
    assert p['max_kakao_calls'] == 45 and p['max_seconds'] == 150
    assert calibration()['profiles']['all']['samples'] > 0
    assert select_budget({'workload': 1}, mode='subway')['max_seconds'] == 30
