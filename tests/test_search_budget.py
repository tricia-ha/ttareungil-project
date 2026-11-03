from search_budget import select_budget
import pytest


def test_fixed_budget_depends_on_mode_but_not_density():
    for mode, calls, seconds in [('integrated', 30, 100), ('transit', 20, 50),
                                 ('subway', 20, 50), ('bicycle', 10, 30)]:
        for workload in (1, 10**6, 10**15):
            plan = select_budget({'workload': workload}, mode)
            assert plan['policy'] == 'fixed_limits_v1'
            assert plan['max_kakao_calls'] == calls
            assert plan['max_seconds'] == seconds
            assert plan['daily_limit_unchanged'] is True


def test_old_settings_cannot_restore_larger_or_unlimited_budgets():
    for seconds in (None, 150, 600):
        plan = select_budget(call_ceiling=45, time_ceiling=seconds)
        assert (plan['max_kakao_calls'], plan['max_seconds']) == (30, 100)


def test_smaller_explicit_limits_are_respected():
    plan = select_budget(call_ceiling=5, time_ceiling=10)
    assert (plan['max_kakao_calls'], plan['max_seconds']) == (5, 10)


@pytest.mark.parametrize('kwargs', [{'call_ceiling': 0}, {'time_ceiling': 0}, {'time_ceiling': float('nan')}])
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        select_budget(**kwargs)


@pytest.mark.parametrize('mode,calls,seconds', [('integrated', 30, 100),
    ('transit', 20, 50), ('subway', 20, 50), ('bicycle', 10, 30)])
def test_legacy_ceilings_cannot_exceed_mode_limits(mode, calls, seconds):
    plan = select_budget(mode=mode, call_ceiling=45, time_ceiling=None)
    assert (plan['max_kakao_calls'], plan['max_seconds']) == (calls, seconds)
    smaller = select_budget(mode=mode, call_ceiling=5, time_ceiling=10)
    assert (smaller['max_kakao_calls'], smaller['max_seconds']) == (5, 10)
