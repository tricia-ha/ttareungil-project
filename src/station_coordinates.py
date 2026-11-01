"""Attach query-local positions only when a live station moved from the model catalog."""


def different_coordinates(a, b):
    # CSV/JSON floats can differ at the last binary digit (observed ~1e-9 m).
    # Twelve decimal degrees remove that serialization noise only.
    return any(
        round(float(a[k]), 12) != round(float(b[k]), 12)
        for k in ("latitude", "longitude")
    )


def bicycle_coordinates(edge, source, target):
    for side, node in (("from", source), ("to", target)):
        if "eta_catalog_latitude" not in node or "eta_catalog_longitude" not in node:
            continue
        current = (float(node["latitude"]), float(node["longitude"]))
        original = (
            float(node["eta_catalog_latitude"]),
            float(node["eta_catalog_longitude"]),
        )
        if any(round(a, 12) != round(b, 12) for a, b in zip(current, original)):
            edge[f"eta_{side}_latitude"], edge[f"eta_{side}_longitude"] = current
    return edge
