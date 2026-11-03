import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from candidate_edges import LazyConnections
from multimodal import Network, Router, Config
from kakao_routes import CallLimit


def test_candidate_priority_does_not_eagerly_resolve_every_walk():
    nodes = {key: dict(latitude=37.5+i*.0001, longitude=127., kind='stop')
             for i, key in enumerate(['origin', 'A', 'B', 'C', 'destination'])}
    net = Network(nodes, walk_policy={'max_distance_m': 900})

    class Walking:
        peek_count = cached_count = 0
        def peek(self, *args):
            self.peek_count += 1
            return False, None
        def cached(self, a, b, start, end):
            self.cached_count += 1
            return True, {'from': start, 'to': end, 'minutes': 1}
        def __call__(self, *args):
            return self.cached(*args)[1]

    walking = Walking()
    lazy = LazyConnections(net, walking)
    stream = lazy.iter_walks_from('origin')
    assert next(stream) is not None
    assert walking.peek_count == 4
    assert walking.cached_count == 1
    assert len(list(stream)) == 3  # Every other candidate remains reachable.


def test_blocked_api_still_resolves_remaining_offline_walks():
    nodes = {key: dict(latitude=37.5+i*.0001, longitude=127., kind='stop')
             for i, key in enumerate(['origin', 'A', 'B', 'destination'])}
    net = Network(nodes, walk_policy={'max_distance_m': 900})

    class Walking:
        def peek(self, *args): return False, None
        def __call__(self, *args): raise CallLimit('quota')
        def cached(self, a, b, start, end):
            return True, {'from': start, 'to': end, 'minutes': 1}

    lazy = LazyConnections(net, Walking())
    edges = list(lazy.iter_walks_from('origin'))
    assert edges[0] is None
    assert {e['to'] for e in edges if e} == {'A', 'B'}


def test_final_timetable_refresh_can_improve_an_existing_result():
    when = '2026-10-30T10:00:00+09:00'
    nodes = {key: dict(latitude=37.5+i*.001, longitude=127., kind='stop')
             for i, key in enumerate(['O', 'A', 'B', 'D'])}
    def ride(a, leave, arrive, trip):
        return {'from': a, 'to': 'D', 'mode': 'bus', 'trip_id': trip,
                'departure': f'2026-10-30T10:{leave:02d}:00+09:00',
                'arrival': f'2026-10-30T10:{arrive:02d}:00+09:00'}
    net = Network(nodes, walks=[{'from': 'O', 'to': 'A', 'minutes': 1}],
                  transit=[ride('A', 20, 30, 'slow'), ride('B', 5, 10, 'fast')],
                  walk_policy={'max_distance_m': 900})
    original = LazyConnections.__init__
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.walk_cache['A'] = [{'from': 'A', 'to': 'B', 'minutes': 1}]
    def blocked(*args): raise CallLimit('test')
    with patch.object(LazyConnections, '__init__', init):
        result = Router(net, None, Config(top_k=1, required_mode='transit', max_expansions=2),
                        walking_provider=blocked).search('O', 'D', when)
    assert result['routes'][0]['total_minutes'] == 10
    assert len(result['timetable_seed_scans']) == 2
