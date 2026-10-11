"""Minimal direct-use example. Change station IDs and departure as needed."""

from datetime import datetime, timedelta
from pprint import pprint

from eta_service import BicycleETAService


with BicycleETAService() as service:
    result = service.predict_with_kma(
        start_station_id="2106",
        end_station_id="2144",
        departure=(datetime.now() + timedelta(hours=2)).isoformat(timespec="minutes"),
    )
    pprint(result)
