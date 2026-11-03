"""Completed bicycle journeys must not wait behind transit-only detours."""
from multimodal import Network, Router, Config
from test_transit_roundtrips import walk, ride, START


def screenshot_case():
    return Network(
        nodes={k: {} for k in ('O', 'R', 'X', 'A', 'B', 'Z')},
        walks=[walk('O', 'R', 2), walk('X', 'A', 2.7), walk('A', 'Z', .1)],
        bikes=[{'from': 'R', 'to': 'X'}],
        transit=[ride('A', 'B', 42.6, 43.9, 'out'),
                 ride('B', 'A', 48.6, 49.9, 'back')],
    )


def test_screenshot_shorter_bicycle_route_is_published_first_under_small_budget():
    updates = []
    result = Router(screenshot_case(), lambda *_: {'minutes': 31.9, 'p_empty': 0},
        Config(top_k=3, horizon_minutes=65, max_seconds=3, max_expansions=8),
        on_routes=lambda r: updates.append(r)).search('O', 'Z', START)
    assert result['routes'][0]['total_minutes'] == 36.7
    assert updates[0]['routes'][0]['total_minutes'] == 36.7
    assert not result['mixed_routes']
    assert result['pruned_transit_roundtrips'] > 0
    for group in ('routes', 'mixed_routes'):
        assert all(not any(l.get('trip_id') in ('out', 'back') for l in r['legs'])
                   for r in result[group])


def test_useful_onward_mixed_journey_survives_detour_removal():
    network = screenshot_case()
    network.nodes['C'] = {}
    network.transit.append(ride('A', 'C', 40, 42, 'onward'))
    network.walks.append(walk('C', 'Z', .5))
    result = Router(network, lambda *_: {'minutes': 31.9, 'p_empty': 0},
        Config(top_k=3, horizon_minutes=65, max_seconds=3, max_expansions=1000)
    ).search('O', 'Z', START)
    assert result['routes'][0]['total_minutes'] == 36.7
    assert result['mixed_routes'][0]['total_minutes'] == 42.5
    assert any(l.get('trip_id') == 'onward' for l in result['mixed_routes'][0]['legs'])
    assert not any(l.get('trip_id') == 'back' for l in result['mixed_routes'][0]['legs'])


def test_platform_transfer_advantage_is_not_mistaken_for_an_equivalent_state():
    network = Network(
        nodes={k: {} for k in ('O', 'A', 'B', 'T', 'Z')},
        walks=[walk('O', 'A', .1),
               dict(walk('A', 'T', .1), platform_transfer=True, entry_fallback_seconds=120),
               walk('T', 'Z', .1)],
        transit=[ride('A', 'B', .2, .3, 'out'), ride('B', 'A', .4, .5, 'back')])
    result = Router(network, None, Config(top_k=1, horizon_minutes=5, max_seconds=3)
                    ).search('O', 'Z', START)
    assert abs(result['routes'][0]['total_minutes'] - .7) < 1e-6
