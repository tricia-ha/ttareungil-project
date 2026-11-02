"""Future rentals may be conditional; missing/stale in-range stock stays blocked."""
import pytest
from multimodal import Network, Router, Config


@pytest.mark.parametrize("status,risk,expected", [
    ("outside_60min_horizon", None, True),
    ("stale_snapshot", None, False),
    ("unsupported_station", None, False),
    ("uncalibrated", 0.9, False),
    ("uncalibrated", 0.01, True),
])
def test_conditional_mixed_route_preserves_stock_status(status, risk, expected):
    class Provider:
        def precheck_inventory(self, edge, departure):
            return {"p_empty": risk, "inventory_status": status}

        def __call__(self, edge, departure):
            return dict(self.precheck_inventory(edge, departure), minutes=5)

    network = Network(
        nodes={"O": {}, "S": {}, "D": {}},
        transit=[{"mode": "subway", "from": "O", "to": "S", "trip_id": "t",
                  "departure": "2026-11-03T23:32:00+09:00",
                  "arrival": "2026-11-03T23:40:00+09:00"}],
        bikes=[{"from": "S", "to": "D"}],
    )
    result = Router(network, Provider(), Config(
        top_k=1, horizon_minutes=25, require_known_inventory=True,
        allow_future_unknown_inventory=True, max_empty_probability=0.1,
    )).search("O", "D", "2026-11-03T23:30:00+09:00")
    assert bool(result["routes"]) == expected
    if expected:
        leg = next(x for x in result["routes"][0]["legs"] if x["mode"] == "bicycle")
        assert leg["details"]["inventory_status"] == status
