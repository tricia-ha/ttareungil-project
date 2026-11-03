import gc
import sys
import unittest
import weakref
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multimodal import Config, Network, Router


class Marker:
    pass


class SearchLifetimeTests(unittest.TestCase):
    def test_finished_and_truncated_search_release_records_without_cyclic_gc(self):
        def query(limit):
            marker = Marker()
            reference = weakref.ref(marker)
            network = Network(
                {'O': {}, 'D': {}},
                walks=[{'from': 'O', 'to': 'D', 'minutes': 1, 'marker': marker}],
            )
            result = Router(network, None, Config(
                horizon_minutes=3, top_k=1, max_seconds=None, max_expansions=limit,
            )).search('O', 'D', '2026-11-02T21:06:00+09:00')
            return reference, result['routes'][0]['total_minutes'] if result['routes'] else None

        enabled = gc.isenabled()
        gc.collect()
        gc.disable()
        try:
            for limit in (1, 100):
                with self.subTest(max_expansions=limit):
                    reference, minutes = query(limit)
                    self.assertIsNone(reference())
                    if limit == 100:
                        self.assertEqual(minutes, 1)
        finally:
            if enabled:
                gc.enable()
            gc.collect()


if __name__ == '__main__':
    unittest.main()
