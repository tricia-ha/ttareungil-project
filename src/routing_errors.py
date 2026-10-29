"""Structural routing failures, distinct from prediction or API failures."""


class NoBicycleRoute(ValueError):
    permanent_route_failure = True
