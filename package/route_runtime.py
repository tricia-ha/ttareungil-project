"""Offline Seoul Bike route and ETA predictor.

Load the bundle once, then call ``predict`` or ``predict_batch``.  No network
connection or server process is required.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
import torch
from sklearn.neighbors import BallTree
from torch import nn


NUMERIC = [
    "route_distance_m", "network_distance_m", "access_distance_m", "base_duration_min",
    "ascent_m", "descent_m", "max_grade", "max_slope_deg", "avg_uphill_slope_deg",
    "longest_climb_m", "uphill_2_4_m", "uphill_4_7_m", "uphill_7_10_m",
    "uphill_10plus_m", "downhill_m", "signal_count", "crossing_count",
    "intersection_count", "turn_count", "signal_density_km", "intersection_density_km",
    "major_road_ratio", "bridge_m", "tunnel_m", "route_bearing_deg", "hour_sin",
    "hour_cos", "weekday_sin", "weekday_cos", "month_sin", "month_cos",
    "origin_temperature_c", "origin_rainfall_mm", "origin_humidity_pct",
    "origin_wind_speed_ms", "origin_snowfall_cm", "destination_temperature_c",
    "destination_rainfall_mm", "destination_humidity_pct", "destination_wind_speed_ms",
    "destination_snowfall_cm", "seoul_temperature_c", "seoul_rainfall_mm",
    "seoul_humidity_pct", "seoul_wind_speed_ms", "seoul_snowfall_cm", "headwind_ms",
    "temperature_delta_c", "rainfall_delta_mm",
]
CAT_COLUMNS = [
    "start_station_id", "end_station_id", "start_district", "end_district",
    "bike_type", "user_type",
]
WEATHER_VARS = [
    "temperature_c", "rainfall_mm", "humidity_pct", "wind_speed_ms",
    "wind_direction_deg", "snowfall_cm",
]


def fingerprint(value) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def canonical_id(value) -> str:
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text or "unknown"


def normalize_departure(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("Asia/Seoul").tz_localize(None)
    return timestamp


def add_time_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    departure = pd.to_datetime(result["started_at"])
    for name, value, period in [
        ("hour", departure.dt.hour + departure.dt.minute / 60, 24),
        ("weekday", departure.dt.dayofweek, 7),
        ("month", departure.dt.month - 1, 12),
    ]:
        result[name + "_sin"] = np.sin(2 * np.pi * value / period)
        result[name + "_cos"] = np.cos(2 * np.pi * value / period)
    return result


class RouteWeatherETA(nn.Module):
    def __init__(self, vocab_sizes, dropout: float = 0.15):
        super().__init__()
        self.station = nn.Embedding(max(vocab_sizes[0], vocab_sizes[1]) + 1, 24)
        self.district = nn.Embedding(max(vocab_sizes[2], vocab_sizes[3]) + 1, 6)
        self.bike = nn.Embedding(vocab_sizes[4] + 1, 3)
        self.user = nn.Embedding(vocab_sizes[5] + 1, 4)
        width = len(NUMERIC) * 2 + 24 * 4 + 6 * 4 + 3 + 4
        self.net = nn.Sequential(
            nn.Linear(width, 256), nn.LayerNorm(256), nn.GELU(), nn.Dropout(dropout),
            nn.Linear(256, 128), nn.LayerNorm(128), nn.GELU(), nn.Dropout(dropout),
            nn.Linear(128, 64), nn.GELU(),
        )
        self.head = nn.Linear(64, 3)

    def forward(self, numeric, categorical):
        start_station = self.station(categorical[:, 0])
        end_station = self.station(categorical[:, 1])
        start_district = self.district(categorical[:, 2])
        end_district = self.district(categorical[:, 3])
        combined = torch.cat(
            [
                numeric,
                start_station, end_station, torch.abs(start_station - end_station), start_station * end_station,
                start_district, end_district, torch.abs(start_district - end_district), start_district * end_district,
                self.bike(categorical[:, 4]), self.user(categorical[:, 5]),
            ],
            dim=1,
        )
        raw = self.head(self.net(combined))
        q50 = raw[:, 0]
        q80 = q50 + nn.functional.softplus(raw[:, 1])
        q90 = q80 + nn.functional.softplus(raw[:, 2])
        return torch.stack([q50, q80, q90], dim=1)


class NeuralPredictor:
    def __init__(self, checkpoint: Path, device: str = "cpu"):
        state = torch.load(checkpoint, map_location="cpu", weights_only=True)
        self.preprocessor = state["preprocessor"]
        self.device = device
        self.model = RouteWeatherETA(state["vocab_sizes"], state["config"]["dropout"]).to(device)
        self.model.load_state_dict(state["model"])
        self.model.eval()

    def _tensors(self, frame: pd.DataFrame):
        prepared = add_time_features(frame)
        raw = prepared.reindex(columns=NUMERIC).apply(pd.to_numeric, errors="coerce").to_numpy(np.float32)
        missing = ~np.isfinite(raw)
        mean = np.asarray(self.preprocessor["mean"], np.float32)
        scale = np.asarray(self.preprocessor["scale"], np.float32)
        numeric = np.concatenate(
            [np.clip((np.where(missing, mean, raw) - mean) / scale, -20, 20), missing.astype(np.float32)],
            axis=1,
        )
        categorical = np.column_stack(
            [
                prepared[column].fillna("unknown").astype(str)
                .map(self.preprocessor["vocabs"][column]).fillna(0).to_numpy(np.int64)
                for column in CAT_COLUMNS
            ]
        )
        return (
            torch.tensor(numeric, device=self.device),
            torch.tensor(categorical, dtype=torch.long, device=self.device),
        )

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        with torch.inference_mode():
            numeric, categorical = self._tensors(frame)
            log_quantiles = self.model(numeric, categorical).cpu().numpy()
        return np.maximum(0, np.expm1(log_quantiles))


class SpatialWeather:
    def __init__(self, path: Path):
        weather = pd.read_csv(path)
        required = {"valid_at", "available_at", "latitude", "longitude", *WEATHER_VARS}
        if not required.issubset(weather.columns):
            raise ValueError(f"Weather columns missing: {sorted(required - set(weather.columns))}")
        weather["valid_at"] = pd.to_datetime(weather["valid_at"])
        weather["available_at"] = pd.to_datetime(weather["available_at"])
        for column in ["latitude", "longitude", *WEATHER_VARS]:
            weather[column] = pd.to_numeric(weather[column], errors="coerce")
        self.data = weather.sort_values("valid_at")
        self.minimum_time = self.data["valid_at"].min()
        self.maximum_time = self.data["valid_at"].max()

    def add(self, frame: pd.DataFrame) -> tuple[pd.DataFrame, list[bool]]:
        result = frame.copy()
        for prefix in ["origin", "destination", "seoul"]:
            for variable in WEATHER_VARS:
                result[f"{prefix}_{variable}"] = np.nan
        matched = pd.Series(False, index=result.index)
        hours = pd.to_datetime(result["started_at"]).dt.floor("h")
        for hour, indexes in hours.groupby(hours).groups.items():
            candidates = self.data[
                (self.data.valid_at <= hour)
                & (self.data.available_at <= hour)
                & (self.data.valid_at >= hour - pd.Timedelta("2h"))
            ]
            if candidates.empty:
                continue
            candidates = candidates.sort_values("valid_at").drop_duplicates(["latitude", "longitude"], keep="last")
            coordinates = candidates[["latitude", "longitude"]].to_numpy(float)
            values = candidates[WEATHER_VARS].to_numpy(float)
            rows = result.loc[indexes]
            for prefix, latitude, longitude in [
                ("origin", "start_lat", "start_lon"),
                ("destination", "end_lat", "end_lon"),
            ]:
                query = rows[[latitude, longitude]].to_numpy(float)
                distances = ((query[:, None, :] - coordinates[None, :, :]) ** 2).sum(axis=2)
                nearest = distances.argmin(axis=1)
                result.loc[indexes, [f"{prefix}_{variable}" for variable in WEATHER_VARS]] = values[nearest]
            result.loc[indexes, [f"seoul_{variable}" for variable in WEATHER_VARS]] = np.nanmean(values, axis=0)
            matched.loc[indexes] = True
        wind = (result.origin_wind_speed_ms + result.destination_wind_speed_ms) / 2
        direction = (result.origin_wind_direction_deg + result.destination_wind_direction_deg) / 2
        result["headwind_ms"] = wind * np.cos(np.deg2rad(direction - result.route_bearing_deg))
        result["temperature_delta_c"] = result.destination_temperature_c - result.origin_temperature_c
        result["rainfall_delta_mm"] = result.destination_rainfall_mm - result.origin_rainfall_mm
        return result, matched.tolist()


def _tags(value):
    if isinstance(value, (list, tuple, set)):
        return {str(item).lower() for item in value}
    return {str(value).lower()}


def _route_bearing(coordinates):
    if len(coordinates) < 2:
        return 0.0
    lon1, lat1 = map(math.radians, coordinates[0])
    lon2, lat2 = map(math.radians, coordinates[-1])
    return float(
        (math.degrees(math.atan2(
            math.sin(lon2 - lon1) * math.cos(lat2),
            math.cos(lat1) * math.sin(lat2)
            - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1),
        )) + 360) % 360
    )


class RouteEngine:
    def __init__(self, graph, cache_path: Path, signature: str, speed_kmh: float = 14.5, max_snap_m: float = 120):
        self.graph = graph
        self.signature = signature
        self.nodes = list(graph.nodes)
        coordinates = np.array([[graph.nodes[node]["y"], graph.nodes[node]["x"]] for node in self.nodes])
        self.tree = BallTree(np.radians(coordinates), metric="haversine")
        self.max_snap_m = max_snap_m
        if not all(data.get("elevation") is not None and np.isfinite(data.get("elevation")) for _, data in graph.nodes(data=True)):
            raise ValueError("Road graph is missing node elevation values")
        self.directed = nx.DiGraph()
        for start, end, key, data in graph.edges(keys=True, data=True):
            length = float(data.get("length", 0))
            rise = float(graph.nodes[end]["elevation"] - graph.nodes[start]["elevation"])
            grade = rise / max(length, 1)
            signal = float("traffic_signals" in _tags(graph.nodes[end].get("highway")))
            data["grade"] = grade
            data["signal"] = signal
            data["cost"] = length / (speed_kmh * 1000 / 60) + max(0, rise) * 0.035 + signal * 0.25
            if length > 0 and (
                not self.directed.has_edge(start, end)
                or data["cost"] < self.directed[start][end]["cost"]
            ):
                self.directed.add_edge(start, end, cost=data["cost"], key=key)
        self.database = sqlite3.connect(cache_path, check_same_thread=False)
        self.database.execute("CREATE TABLE IF NOT EXISTS routes(cache_key TEXT PRIMARY KEY,payload TEXT)")
        self.database.commit()

    def close(self):
        self.database.close()

    def _snap(self, latitude, longitude):
        distance, index = self.tree.query(np.radians([[latitude, longitude]]), k=1)
        meters = float(distance[0, 0] * 6371000)
        if meters > self.max_snap_m:
            raise ValueError(f"Station is {meters:.0f}m from the routable road graph")
        return self.nodes[index[0, 0]], meters

    def candidates(self, start_lat, start_lon, end_lat, end_lon):
        key = fingerprint([self.signature, start_lat, start_lon, end_lat, end_lon])
        cached = self.database.execute("SELECT payload FROM routes WHERE cache_key=?", (key,)).fetchone()
        if cached:
            return json.loads(cached[0])
        source, access_start = self._snap(start_lat, start_lon)
        target, access_end = self._snap(end_lat, end_lon)
        if source == target:
            return []
        try:
            nodes = nx.shortest_path(self.directed, source, target, weight="cost")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []
        edge_ids = [(start, end, self.directed[start][end]["key"]) for start, end in zip(nodes[:-1], nodes[1:])]
        edges = [self.graph[start][end][key] for start, end, key in edge_ids]
        total = sum(data["length"] for data in edges)
        coordinates = [[self.graph.nodes[node]["x"], self.graph.nodes[node]["y"]] for node in nodes]

        def length_if(test):
            return float(sum(data["length"] for data in edges if test(data)))

        result = {
            "route_id": fingerprint(edge_ids),
            "profiles": ["fast"],
            "route_distance_m": total + access_start + access_end,
            "network_distance_m": total,
            "access_distance_m": access_start + access_end,
            "base_duration_min": sum(data["cost"] for data in edges)
            + (access_start + access_end) / (4 * 1000 / 60),
            "descent_m": sum(max(0, -data["grade"] * data["length"]) for data in edges),
            "max_grade": max(abs(data["grade"]) for data in edges),
            "uphill_2_4_m": length_if(lambda data: 0.02 <= data["grade"] < 0.04),
            "uphill_4_7_m": length_if(lambda data: 0.04 <= data["grade"] < 0.07),
            "uphill_7_10_m": length_if(lambda data: 0.07 <= data["grade"] < 0.10),
            "uphill_10plus_m": length_if(lambda data: data["grade"] >= 0.10),
            "downhill_m": length_if(lambda data: data["grade"] <= -0.02),
            "signal_count": sum(data["signal"] for data in edges),
            "crossing_count": sum("crossing" in _tags(self.graph.nodes[node].get("highway")) for node in nodes),
            "intersection_count": sum(
                len(set(self.graph.predecessors(node)) | set(self.graph.successors(node))) >= 3
                for node in nodes[1:-1]
            ),
            "major_road_ratio": length_if(
                lambda data: bool(_tags(data.get("highway")) & {"primary", "secondary", "trunk"})
            ) / max(total, 1),
            "bridge_m": length_if(lambda data: str(data.get("bridge", "")).lower() not in {"", "no", "none"}),
            "tunnel_m": length_if(lambda data: str(data.get("tunnel", "")).lower() not in {"", "no", "none"}),
            "route_bearing_deg": _route_bearing(coordinates),
            "geometry": {"type": "LineString", "coordinates": coordinates},
            "route_source": "osm_srtm_v2",
        }
        ascent = float(sum(max(0, data["grade"] * data["length"]) for data in edges))
        uphill_distance = float(sum(data["length"] for data in edges if data["grade"] > 0))
        current = longest = 0.0
        for data in edges:
            current = current + data["length"] if data["grade"] >= 0.02 else 0.0
            longest = max(longest, current)
        turns = 0
        for point0, point1, point2 in zip(coordinates[:-2], coordinates[1:-1], coordinates[2:]):
            vector1 = np.array(point1) - np.array(point0)
            vector2 = np.array(point2) - np.array(point1)
            if np.linalg.norm(vector1) > 0 and np.linalg.norm(vector2) > 0:
                angle = math.degrees(math.acos(float(np.clip(
                    np.dot(vector1, vector2) / (np.linalg.norm(vector1) * np.linalg.norm(vector2)), -1, 1
                ))))
                turns += angle >= 30
        result.update({
            "ascent_m": ascent,
            "max_slope_deg": float(math.degrees(math.atan(result["max_grade"]))),
            "avg_uphill_slope_deg": float(math.degrees(math.atan(ascent / max(uphill_distance, 1)))),
            "longest_climb_m": longest,
            "turn_count": int(turns),
        })
        result["signal_density_km"] = result["signal_count"] / max(total / 1000, 0.001)
        result["intersection_density_km"] = result["intersection_count"] / max(total / 1000, 0.001)
        payload = [result]
        self.database.execute(
            "INSERT OR REPLACE INTO routes VALUES(?,?)",
            (key, json.dumps(payload, allow_nan=False)),
        )
        self.database.commit()
        return payload


class BicycleETA:
    """Load the offline bundle once and answer single or batch station-pair queries."""

    def __init__(self, bundle_dir: str | Path | None = None, device: str = "cpu"):
        self.root = Path(bundle_dir or Path(__file__).resolve().parent).resolve()
        self.manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.stations = pd.read_csv(self.root / "data" / "stations_deployment.csv", dtype={"station_id": str})
        self.stations["station_id"] = self.stations["station_id"].map(canonical_id)
        self.stations = self.stations.drop_duplicates("station_id").set_index("station_id")
        self.weather = SpatialWeather(self.root / "data" / "weather_2024_district_hourly.csv")
        # The V2 neural checkpoint is retained only because this proven offline
        # runtime initialises it together with the route graph. V4 inference is
        # performed by bicycle_eta.py with artifacts/best.pt.
        self.neural = NeuralPredictor(self.root / "artifacts" / "route_model_v2.pt", device=device)
        print("Loading offline Seoul road graph...", flush=True)
        with gzip.open(self.root / "artifacts" / "seoul_roads_v2_elevation.json.gz", "rt", encoding="utf-8") as stream:
            saved = json.load(stream)
        graph = nx.node_link_graph(saved["graph"], edges="edges")
        self.routes = RouteEngine(
            graph,
            self.root / "artifacts" / "routes_v2.sqlite",
            signature=self.manifest["graph_signature"],
        )
        print("BicycleETA ready", flush=True)

    def close(self):
        self.routes.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def station_table(self) -> pd.DataFrame:
        return self.stations.reset_index().copy()

    def _route_row(self, start_station_id, end_station_id, departure, bike_type, user_type):
        start_id = canonical_id(start_station_id)
        end_id = canonical_id(end_station_id)
        if start_id not in self.stations.index:
            raise KeyError(f"Unknown start station: {start_id}")
        if end_id not in self.stations.index:
            raise KeyError(f"Unknown end station: {end_id}")
        if start_id == end_id:
            raise ValueError("Start and end stations must be different")
        start = self.stations.loc[start_id]
        end = self.stations.loc[end_id]
        candidates = self.routes.candidates(
            float(start.latitude), float(start.longitude), float(end.latitude), float(end.longitude)
        )
        if not candidates:
            raise ValueError(f"No routable bicycle path: {start_id} -> {end_id}")
        route = candidates[0]
        row = {key: value for key, value in route.items() if key not in {"geometry", "profiles"}}
        row.update({
            "started_at": normalize_departure(departure),
            "start_station_id": start_id,
            "end_station_id": end_id,
            "start_district": str(start.district),
            "end_district": str(end.district),
            "start_lat": float(start.latitude),
            "start_lon": float(start.longitude),
            "end_lat": float(end.latitude),
            "end_lon": float(end.longitude),
            "bike_type": bike_type,
            "user_type": user_type,
        })
        return row, route

    def predict(
        self,
        start_station_id,
        end_station_id,
        departure,
        bike_type: str = "regular",
        user_type: str = "unknown",
    ) -> dict:
        return self.predict_batch(
            [(start_station_id, end_station_id)], departure, bike_type=bike_type, user_type=user_type
        )[0]

    def predict_batch(
        self,
        pairs: Iterable[tuple[str, str] | dict],
        departure,
        bike_type: str = "regular",
        user_type: str = "unknown",
    ) -> list[dict]:
        rows, routes = [], []
        for pair in pairs:
            if isinstance(pair, dict):
                start_id = pair["start_station_id"]
                end_id = pair["end_station_id"]
            else:
                start_id, end_id = pair
            row, route = self._route_row(start_id, end_id, departure, bike_type, user_type)
            rows.append(row)
            routes.append(route)
        if not rows:
            return []
        frame, weather_matched = self.weather.add(pd.DataFrame(rows))
        quantiles = self.neural.predict(frame)
        results = []
        for row, route, estimate, matched in zip(rows, routes, quantiles, weather_matched):
            results.append({
                "start_station_id": row["start_station_id"],
                "end_station_id": row["end_station_id"],
                "departure": normalize_departure(departure).isoformat(),
                "distance_m": float(route["route_distance_m"]),
                "base_duration_min": float(route["base_duration_min"]),
                "ascent_m": float(route["ascent_m"]),
                "descent_m": float(route["descent_m"]),
                "signal_count": int(route["signal_count"]),
                "eta": {
                    "p50_min": float(estimate[0]),
                    "p80_min": float(estimate[1]),
                    "p90_min": float(estimate[2]),
                },
                "weather_matched": bool(matched),
                "weather_available_range": {
                    "start": self.weather.minimum_time.isoformat(),
                    "end": self.weather.maximum_time.isoformat(),
                },
                "route": route["geometry"],
                "route_id": route["route_id"],
                "model_version": self.manifest["model_version"],
            })
        return results


__all__ = ["BicycleETA"]
