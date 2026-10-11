"""Reusable offline OSM route and V5.2.1 Seoul Bike ETA service."""

from __future__ import annotations

import gzip
import json
import math
import os
import threading
from pathlib import Path
from typing import Iterable, Mapping

import networkx as nx
import numpy as np
import pandas as pd
import torch
from torch import nn

from route_runtime import RouteEngine, canonical_id, normalize_departure


COMMON_NUMERIC = [
    "straight_distance_m", "bearing_sin", "bearing_cos", "hour_sin", "hour_cos",
    "weekday_sin", "weekday_cos", "month_sin", "month_cos", "is_weekend",
    "mean_temperature_c", "mean_humidity_pct", "mean_rainfall_mm",
    "mean_snowfall_cm", "mean_wind_speed_ms", "headwind_ms", "weather_available",
]
ROUTE_NUMERIC = [
    "route_distance_m", "route_to_straight_ratio", "route_available", "access_ratio",
    "base_duration_min", "ascent_m", "descent_m", "max_grade",
    "avg_uphill_slope_deg", "longest_climb_m", "uphill_2_4_ratio",
    "uphill_4_7_ratio", "uphill_7plus_ratio", "downhill_ratio", "signal_count",
    "crossing_count", "intersection_count", "turn_count", "signal_density_km",
    "intersection_density_km", "major_road_ratio", "bridge_ratio", "tunnel_ratio",
    "ascent_per_km", "headwind_exposure", "rain_exposure",
    "peak_signal_interaction", "peak_intersection_interaction", "cold_wind",
    "climb_load", "route_was_alternative",
]
LOCAL_NUMERIC = [
    "od_prior_min", "od_log_count", "od_iqr_min", "od_unseen", "od_weight",
    "od_vs_base_log_ratio", "origin_log_mean", "origin_log_count",
    "destination_log_mean", "destination_log_count", "district_pair_log_mean",
    "district_pair_log_count",
]
COMMON_CAT = ["bike_type", "user_type"]
LOCAL_CAT = ["start_station_id", "end_station_id", "start_district", "end_district"]
WEATHER_API_VARS = [
    "temperature_c", "rainfall_mm", "humidity_pct", "wind_speed_ms",
    "wind_direction_deg", "snowfall_cm",
]
OD_KEYS = ["start_station_id", "end_station_id"]
OD_SHRINKAGE_K = 20.0
SCOPES = {
    "origin": ["start_station_id"],
    "destination": ["end_station_id"],
    "district_pair": ["start_district", "end_district"],
}


class ResidualBlock(nn.Module):
    def __init__(self, width: int, dropout: float = 0.12):
        super().__init__()
        self.net = nn.Sequential(
            nn.LayerNorm(width), nn.Linear(width, width * 2), nn.GELU(),
            nn.Dropout(dropout), nn.Linear(width * 2, width), nn.Dropout(dropout),
        )

    def forward(self, values):
        return values + self.net(values)


class V51Network(nn.Module):
    def __init__(self, prep: dict, dropout: float = 0.12):
        super().__init__()
        vocab = prep["vocabs"]
        self.common_emb = nn.ModuleList(
            [nn.Embedding(len(vocab[column]) + 1, 5) for column in COMMON_CAT]
        )
        self.station = nn.Embedding(len(vocab["start_station_id"]) + 1, 32)
        self.district = nn.Embedding(len(vocab["start_district"]) + 1, 8)
        self.common_encoder = nn.Sequential(
            nn.Linear(len(COMMON_NUMERIC) * 2 + 10, 128), nn.LayerNorm(128),
            nn.GELU(), ResidualBlock(128, dropout),
        )
        self.route_encoder = nn.Sequential(
            nn.Linear(len(ROUTE_NUMERIC) * 2, 192), nn.LayerNorm(192),
            nn.GELU(), ResidualBlock(192, dropout),
        )
        self.local_numeric = nn.Sequential(
            nn.Linear(len(LOCAL_NUMERIC) * 2, 64), nn.LayerNorm(64), nn.GELU()
        )
        self.route_fusion = nn.Sequential(
            nn.Linear(320, 384), nn.LayerNorm(384), nn.GELU(),
            ResidualBlock(384, dropout), ResidualBlock(384, dropout),
            ResidualBlock(384, dropout),
        )
        self.route_head = nn.Linear(384, 3)
        local_width = 64 + 32 * 4 + 8 * 4
        self.local_fusion = nn.Sequential(
            nn.Linear(local_width, 192), nn.LayerNorm(192), nn.GELU(),
            ResidualBlock(192, dropout), ResidualBlock(192, dropout),
        )
        self.local_head = nn.Linear(192, 3)

    def forward(self, common, route, local, common_cat, local_cat, anchor, gate):
        common_embedding = torch.cat(
            [embedding(common_cat[:, i]) for i, embedding in enumerate(self.common_emb)], 1
        )
        common_state = self.common_encoder(torch.cat([common, common_embedding], 1))
        route_state = self.route_encoder(route)
        route_raw = self.route_head(
            self.route_fusion(torch.cat([common_state, route_state], 1))
        )
        start_station, end_station = self.station(local_cat[:, 0]), self.station(local_cat[:, 1])
        start_district, end_district = self.district(local_cat[:, 2]), self.district(local_cat[:, 3])
        local_state = self.local_fusion(
            torch.cat(
                [
                    self.local_numeric(local), start_station, end_station,
                    torch.abs(start_station - end_station), start_station * end_station,
                    start_district, end_district,
                    torch.abs(start_district - end_district), start_district * end_district,
                ],
                1,
            )
        )
        raw = route_raw + gate[:, None] * self.local_head(local_state)
        q50 = anchor + raw[:, 0]
        q80 = q50 + nn.functional.softplus(raw[:, 1])
        q90 = q80 + nn.functional.softplus(raw[:, 2])
        return torch.stack([q50, q80, q90], 1)


def _haversine_bearing(frame: pd.DataFrame):
    latitude_1 = np.radians(pd.to_numeric(frame.start_lat, errors="coerce"))
    latitude_2 = np.radians(pd.to_numeric(frame.end_lat, errors="coerce"))
    delta_longitude = np.radians(
        pd.to_numeric(frame.end_lon, errors="coerce")
        - pd.to_numeric(frame.start_lon, errors="coerce")
    )
    delta_latitude = latitude_2 - latitude_1
    hav = (
        np.sin(delta_latitude / 2) ** 2
        + np.cos(latitude_1) * np.cos(latitude_2) * np.sin(delta_longitude / 2) ** 2
    )
    distance = 6_371_000 * 2 * np.arctan2(
        np.sqrt(hav), np.sqrt(np.maximum(1 - hav, 0))
    )
    y = np.sin(delta_longitude) * np.cos(latitude_2)
    x = (
        np.cos(latitude_1) * np.sin(latitude_2)
        - np.sin(latitude_1) * np.cos(latitude_2) * np.cos(delta_longitude)
    )
    bearing = np.arctan2(y, x)
    return distance, np.sin(bearing), np.cos(bearing)


class BicycleETAService:
    """Load all artifacts once and answer single or batch ETA queries offline."""

    model_version = "prototype_v5_2_1_temporal_best_osm_kma"

    def __init__(
        self,
        bundle_dir: str | Path | None = None,
        device: str = "cpu",
        weather_provider=None,
    ):
        self.root = Path(bundle_dir or Path(__file__).resolve().parent).resolve()
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.model_version = manifest.get("model_version", self.model_version)
        self.device = torch.device(device)
        self.lock = threading.RLock()
        station_catalog_file = manifest.get("station_catalog_file", "stations_2026_06.csv")
        self.stations = pd.read_csv(
            self.root / "data" / station_catalog_file, dtype={"station_id": str}
        )
        self.stations["station_id"] = self.stations["station_id"].map(canonical_id)
        self.stations = self.stations.drop_duplicates("station_id").set_index("station_id")

        artifact_dir = (self.root / manifest.get("artifact_dir", "artifacts")).resolve()
        self.prep = json.loads((artifact_dir / "preprocessor.json").read_text(encoding="utf-8"))
        trained_stations = set(self.prep["vocabs"]["start_station_id"])
        self.stations["model_station_status"] = np.where(
            self.stations.index.astype(str).isin(trained_stations),
            "trained",
            "route_fallback",
        )
        self.calibrator = json.loads((artifact_dir / "calibrator.json").read_text(encoding="utf-8"))
        self.stats_manifest = json.loads(
            (artifact_dir / "filtered_train_stats" / "manifest.json").read_text(encoding="utf-8")
        )
        self.od_full = pd.read_parquet(
            artifact_dir / "filtered_train_stats" / "od_full.parquet"
        )
        for column in OD_KEYS:
            self.od_full[column] = self.od_full[column].astype(str)
        self.hierarchy = {}
        for name, keys in SCOPES.items():
            stats = pd.read_parquet(
                artifact_dir / "filtered_train_stats" / f"hier_{name}.parquet"
            )
            self.hierarchy[name] = stats.loc[stats.od_fold.eq(-1), keys + ["sum", "count"]]

        # The twelve experimental alternative-route rows used during training do not
        # include geometry. Online inference therefore keeps one internally consistent
        # OSM route: returned geometry and every route feature describe the same path.
        self.overrides = pd.DataFrame()

        checkpoint = torch.load(artifact_dir / "best.pt", map_location="cpu", weights_only=False)
        self.model = V51Network(checkpoint["preprocessor"]).to(self.device)
        self.model.load_state_dict(checkpoint["model"])
        self.model.eval()

        print("Loading offline Seoul OSM road graph...", flush=True)
        with gzip.open(
            artifact_dir / "seoul_roads_v2_elevation.json.gz", "rt", encoding="utf-8"
        ) as stream:
            saved_graph = json.load(stream)
        graph = nx.node_link_graph(saved_graph["graph"], edges="edges")
        cache_dir = Path(os.getenv("BICYCLE_CACHE_DIR", str(self.root / "cache")))
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.routes = RouteEngine(
            graph,
            cache_dir / "routes_v2.sqlite",
            signature=manifest["graph_signature"],
        )
        self.weather_provider = weather_provider
        print("BicycleETAService ready", flush=True)

    def close(self):
        with self.lock:
            self.routes.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def station_table(self, district: str | None = None) -> pd.DataFrame:
        result = self.stations.reset_index().copy()
        if district:
            result = result.loc[result["district"].eq(district)]
        return result

    def _route_row(self, start_station_id, end_station_id, departure, bike_type, user_type):
        start_id, end_id = canonical_id(start_station_id), canonical_id(end_station_id)
        if start_id not in self.stations.index:
            raise KeyError(f"Unknown or inactive start station: {start_id}")
        if end_id not in self.stations.index:
            raise KeyError(f"Unknown or inactive end station: {end_id}")
        if start_id == end_id:
            raise ValueError("Start and end stations must be different")
        start, end = self.stations.loc[start_id], self.stations.loc[end_id]
        with self.lock:
            candidates = self.routes.candidates(
                float(start.latitude), float(start.longitude),
                float(end.latitude), float(end.longitude),
            )
        if not candidates:
            raise ValueError(f"No routable bicycle path: {start_id} -> {end_id}")
        route = candidates[0]
        row = {key: value for key, value in route.items() if key not in {"geometry", "profiles"}}
        row.update(
            {
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
            }
        )
        return row, route

    def _attach_weather(self, frame: pd.DataFrame, weather: Mapping | None):
        result = frame.copy()
        flat_weather = isinstance(weather, Mapping) and any(
            variable in weather for variable in WEATHER_API_VARS
        )
        for prefix in ["origin", "destination", "seoul"]:
            if not isinstance(weather, Mapping):
                values = {}
            elif flat_weather:
                values = weather
            else:
                values = weather.get(prefix, {})
            for variable in WEATHER_API_VARS:
                result[f"{prefix}_{variable}"] = pd.to_numeric(
                    values.get(variable, np.nan), errors="coerce"
                )
        for variable in [
            "temperature_c", "rainfall_mm", "humidity_pct", "wind_speed_ms", "snowfall_cm"
        ]:
            trip_mean = (
                pd.to_numeric(result[f"origin_{variable}"], errors="coerce")
                + pd.to_numeric(result[f"destination_{variable}"], errors="coerce")
            ) / 2
            seoul_value = pd.to_numeric(result[f"seoul_{variable}"], errors="coerce")
            result[f"mean_{variable}"] = trip_mean.where(trip_mean.notna(), seoul_value)
        wind = (
            pd.to_numeric(result.origin_wind_speed_ms, errors="coerce")
            + pd.to_numeric(result.destination_wind_speed_ms, errors="coerce")
        ) / 2
        direction = (
            pd.to_numeric(result.origin_wind_direction_deg, errors="coerce")
            + pd.to_numeric(result.destination_wind_direction_deg, errors="coerce")
        ) / 2
        result["headwind_ms"] = wind * np.cos(
            np.deg2rad(direction - pd.to_numeric(result.route_bearing_deg, errors="coerce"))
        )
        return result, "provided_forecast" if weather is not None else "missing_model_fallback"

    def _apply_overrides(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.copy()
        if self.overrides.empty:
            result["route_was_alternative"] = 0.0
            return result
        renamed = self.overrides.rename(
            columns={column: "v51_" + column for column in self.overrides if column not in OD_KEYS}
        )
        result = result.merge(renamed, on=OD_KEYS, how="left", validate="many_to_one")
        changed = (
            result["v51_route_profile"].notna()
            if "v51_route_profile" in result
            else pd.Series(False, index=result.index)
        )
        route_columns = [
            "route_distance_m", "network_distance_m", "access_distance_m",
            "base_duration_min", "ascent_m", "descent_m", "max_grade",
            "avg_uphill_slope_deg", "longest_climb_m", "uphill_2_4_m",
            "uphill_4_7_m", "uphill_7_10_m", "uphill_10plus_m", "downhill_m",
            "signal_count", "crossing_count", "intersection_count", "turn_count",
            "signal_density_km", "intersection_density_km", "major_road_ratio",
            "bridge_m", "tunnel_m", "route_bearing_deg",
        ]
        for column in route_columns:
            replacement = "v51_" + column
            if replacement in result:
                result[column] = result[replacement].where(changed, result[column])
        result["route_was_alternative"] = changed.astype("float32")
        return result.drop(columns=[column for column in result if column.startswith("v51_")])

    def _add_history(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = frame.merge(self.od_full, on=OD_KEYS, how="left", validate="many_to_one")
        count = pd.to_numeric(result.od_count, errors="coerce").fillna(0)
        base = pd.to_numeric(result.base_duration_min, errors="coerce").clip(lower=0.01)
        median = pd.to_numeric(result.od_median_min, errors="coerce").fillna(base)
        weight = count / (count + OD_SHRINKAGE_K)
        result["od_prior_min"] = weight * median + (1 - weight) * base
        result["od_log_count"] = np.log1p(count)
        result["od_iqr_min"] = pd.to_numeric(result.od_iqr_min, errors="coerce").fillna(0)
        result["od_unseen"] = (count <= 0).astype("float32")
        result["od_weight"] = weight
        result["od_vs_base_log_ratio"] = np.log1p(median) - np.log1p(base)
        for name, keys in SCOPES.items():
            stats = self.hierarchy[name].rename(columns={"sum": "total_sum", "count": "total_count"})
            result = result.merge(stats, on=keys, how="left", validate="many_to_one")
            hierarchy_count = result.total_count.fillna(0)
            hierarchy_sum = result.total_sum.fillna(0)
            fallback = math.log1p(float(self.stats_manifest["global_train_median"]))
            result[name + "_log_mean"] = np.where(
                hierarchy_count > 0,
                hierarchy_sum / np.maximum(hierarchy_count, 1),
                fallback,
            )
            result[name + "_log_count"] = np.log1p(np.maximum(hierarchy_count, 0))
            result = result.drop(columns=["total_sum", "total_count"])
        return result

    def _prepare(self, frame: pd.DataFrame) -> pd.DataFrame:
        result = self._add_history(self._apply_overrides(frame))
        distance, bearing_sin, bearing_cos = _haversine_bearing(result)
        result["straight_distance_m"] = distance
        result["bearing_sin"] = bearing_sin
        result["bearing_cos"] = bearing_cos
        departure = pd.to_datetime(result.started_at, errors="coerce")
        hour = departure.dt.hour + departure.dt.minute / 60
        weekday = departure.dt.dayofweek
        month = departure.dt.month - 1
        for name, value, period in [
            ("hour", hour, 24), ("weekday", weekday, 7), ("month", month, 12)
        ]:
            result[name + "_sin"] = np.sin(2 * np.pi * value / period)
            result[name + "_cos"] = np.cos(2 * np.pi * value / period)
        result["is_weekend"] = (weekday >= 5).astype("float32")
        result["weather_available"] = pd.to_numeric(
            result.get("mean_temperature_c"), errors="coerce"
        ).notna().astype("float32")
        peak = (((hour >= 7) & (hour < 10)) | ((hour >= 17) & (hour < 20))).astype(float)
        route = pd.to_numeric(result.route_distance_m, errors="coerce")
        km = np.maximum(route / 1000, 0.1)
        result["route_available"] = route.notna().astype("float32")
        result["route_to_straight_ratio"] = route / np.maximum(result.straight_distance_m, 1)
        result["access_ratio"] = pd.to_numeric(result.access_distance_m, errors="coerce") / np.maximum(route, 1)
        result["ascent_per_km"] = pd.to_numeric(result.ascent_m, errors="coerce") / km
        result["uphill_2_4_ratio"] = pd.to_numeric(result.uphill_2_4_m, errors="coerce") / np.maximum(route, 1)
        result["uphill_4_7_ratio"] = pd.to_numeric(result.uphill_4_7_m, errors="coerce") / np.maximum(route, 1)
        result["uphill_7plus_ratio"] = (
            pd.to_numeric(result.uphill_7_10_m, errors="coerce")
            + pd.to_numeric(result.uphill_10plus_m, errors="coerce")
        ) / np.maximum(route, 1)
        result["downhill_ratio"] = pd.to_numeric(result.downhill_m, errors="coerce") / np.maximum(route, 1)
        result["bridge_ratio"] = pd.to_numeric(result.bridge_m, errors="coerce") / np.maximum(route, 1)
        result["tunnel_ratio"] = pd.to_numeric(result.tunnel_m, errors="coerce") / np.maximum(route, 1)
        result["headwind_exposure"] = pd.to_numeric(result.headwind_ms, errors="coerce") * km
        result["rain_exposure"] = pd.to_numeric(result.mean_rainfall_mm, errors="coerce") * km
        result["peak_signal_interaction"] = peak * pd.to_numeric(result.signal_density_km, errors="coerce")
        result["peak_intersection_interaction"] = peak * pd.to_numeric(result.intersection_density_km, errors="coerce")
        result["cold_wind"] = np.maximum(
            0, 10 - pd.to_numeric(result.mean_temperature_c, errors="coerce")
        ) * pd.to_numeric(result.mean_wind_speed_ms, errors="coerce")
        result["climb_load"] = pd.to_numeric(result.ascent_m, errors="coerce") * result.ascent_per_km

        station_vocab = self.prep["vocabs"]["start_station_id"]
        route_only = (
            ~result.start_station_id.astype(str).isin(station_vocab)
            | ~result.end_station_id.astype(str).isin(station_vocab)
        )
        result["station_fallback_used"] = route_only
        if route_only.any():
            result.loc[route_only, "od_count"] = 0
            result.loc[route_only, "od_prior_min"] = result.loc[route_only, "base_duration_min"]
            result.loc[route_only, "od_log_count"] = 0
            result.loc[route_only, "od_iqr_min"] = 0
            result.loc[route_only, "od_unseen"] = 1
            result.loc[route_only, "od_weight"] = 0
            result.loc[route_only, "od_vs_base_log_ratio"] = 0
        return result

    def _standardized(self, frame: pd.DataFrame, group: str) -> np.ndarray:
        spec = self.prep[group]
        raw = frame.reindex(columns=spec["columns"]).apply(
            pd.to_numeric, errors="coerce"
        ).to_numpy(np.float32)
        missing = ~np.isfinite(raw)
        mean = np.asarray(spec["mean"], np.float32)
        scale = np.asarray(spec["scale"], np.float32)
        filled = np.where(missing, mean, raw)
        return np.concatenate(
            [np.clip((filled - mean) / scale, -20, 20), missing.astype(np.float32)], 1
        )

    def _categorical(self, frame: pd.DataFrame, columns: list[str]) -> np.ndarray:
        return np.column_stack(
            [
                frame.get(column, pd.Series("unknown", index=frame.index))
                .fillna("unknown").astype(str)
                .map(self.prep["vocabs"][column]).fillna(0).to_numpy(np.int64)
                for column in columns
            ]
        )

    def _infer(self, frame: pd.DataFrame) -> np.ndarray:
        common = torch.tensor(self._standardized(frame, "common"), device=self.device)
        route = torch.tensor(self._standardized(frame, "route"), device=self.device)
        local = torch.tensor(self._standardized(frame, "local"), device=self.device)
        common_cat = torch.tensor(
            self._categorical(frame, COMMON_CAT), dtype=torch.long, device=self.device
        )
        local_cat = torch.tensor(
            self._categorical(frame, LOCAL_CAT), dtype=torch.long, device=self.device
        )
        anchor = torch.log1p(
            torch.tensor(
                pd.to_numeric(frame.od_prior_min, errors="coerce")
                .clip(lower=0.01).to_numpy(np.float32),
                device=self.device,
            )
        )
        gate = torch.tensor(
            pd.to_numeric(frame.od_weight, errors="coerce").fillna(0).to_numpy(np.float32),
            device=self.device,
        )
        with torch.inference_mode():
            log_quantiles = self.model(common, route, local, common_cat, local_cat, anchor, gate)
        raw = np.maximum(0, np.expm1(log_quantiles.cpu().numpy()))
        additive = np.asarray(self.calibrator["quantile_additive_minutes"])
        return np.maximum.accumulate(np.maximum(0, raw + additive), axis=1)

    def predict(
        self,
        start_station_id,
        end_station_id,
        departure,
        bike_type: str = "regular",
        user_type: str = "domestic",
        weather: Mapping | None = None,
    ) -> dict:
        return self.predict_batch(
            [
                {
                    "start_station_id": start_station_id,
                    "end_station_id": end_station_id,
                    "departure": departure,
                    "bike_type": bike_type,
                    "user_type": user_type,
                    "weather": weather,
                }
            ]
        )[0]

    def predict_with_kma(
        self,
        start_station_id,
        end_station_id,
        departure,
        bike_type: str = "regular",
        user_type: str = "domestic",
    ) -> dict:
        """Fetch KMA forecasts for both stations and return one ETA prediction."""
        start_id, end_id = canonical_id(start_station_id), canonical_id(end_station_id)
        if start_id not in self.stations.index:
            raise KeyError(f"Unknown or inactive start station: {start_id}")
        if end_id not in self.stations.index:
            raise KeyError(f"Unknown or inactive end station: {end_id}")
        if self.weather_provider is None:
            from kma_weather_forecast_provider import KMAWeatherForecastProvider

            self.weather_provider = KMAWeatherForecastProvider(
                cache_dir=self.root / "cache" / "kma_forecast"
            )
        start, end = self.stations.loc[start_id], self.stations.loc[end_id]
        forecast = self.weather_provider.for_route(
            float(start.latitude),
            float(start.longitude),
            float(end.latitude),
            float(end.longitude),
            departure,
        )
        weather = self.weather_provider.model_weather(forecast)
        result = self.predict(
            start_id,
            end_id,
            departure,
            bike_type=bike_type,
            user_type=user_type,
            weather=weather,
        )
        result["model"]["weather_source"] = forecast.get("provider", "kma_vilage_forecast")
        result["model"]["weather_unique_grid_calls"] = int(
            forecast.get("unique_grid_calls", 0)
        )
        result["weather_forecast"] = {
            location: {
                "forecast_issued_at": forecast[location].get("forecast_issued_at"),
                "forecast_valid_at": forecast[location].get("forecast_valid_at"),
                "grid_nx": forecast[location].get("grid_nx"),
                "grid_ny": forecast[location].get("grid_ny"),
            }
            for location in ("origin", "destination", "seoul")
        }
        return result

    def predict_batch(self, requests: Iterable[Mapping]) -> list[dict]:
        rows, routes, weather_sources = [], [], []
        for request in requests:
            row, route = self._route_row(
                request["start_station_id"], request["end_station_id"],
                request["departure"], request.get("bike_type", "regular"),
                request.get("user_type", "domestic"),
            )
            weather_frame, weather_source = self._attach_weather(
                pd.DataFrame([row]), request.get("weather")
            )
            rows.append(weather_frame.iloc[0].to_dict())
            routes.append(route)
            weather_sources.append(weather_source)
        if not rows:
            return []
        frame = self._prepare(pd.DataFrame(rows))
        quantiles = self._infer(frame)
        now = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)
        results = []
        for index, (row, route, estimate, weather_source) in enumerate(
            zip(frame.to_dict("records"), routes, quantiles, weather_sources)
        ):
            departure = normalize_departure(row["started_at"])
            p50, p80, p90 = (float(value) for value in estimate)
            od_count = pd.to_numeric(pd.Series([row.get("od_count")]), errors="coerce").fillna(0).iloc[0]
            od_weight = pd.to_numeric(pd.Series([row.get("od_weight")]), errors="coerce").fillna(0).iloc[0]
            results.append(
                {
                    "start_station_id": row["start_station_id"],
                    "end_station_id": row["end_station_id"],
                    "departure": departure.isoformat(),
                    "forecast_horizon_min": float(max(0, (departure - now).total_seconds() / 60)),
                    "eta": {
                        "expected_min": p50,
                        "p50_min": p50,
                        "p80_min": p80,
                        "p90_min": p90,
                    },
                    "route": {
                        "route_id": route["route_id"],
                        "source": route.get("route_source", "osm_srtm_v2"),
                        "geometry": route["geometry"],
                        "distance_m": float(row["route_distance_m"]),
                        "base_duration_min": float(row["base_duration_min"]),
                        "ascent_m": float(row["ascent_m"]),
                        "descent_m": float(row["descent_m"]),
                        "signal_count": int(row["signal_count"]),
                        "intersection_count": int(row["intersection_count"]),
                        "turn_count": int(row["turn_count"]),
                    },
                    "model": {
                        "version": self.model_version,
                        "weather_source": weather_source,
                        "weather_available": bool(row["weather_available"]),
                        "station_fallback_used": bool(row["station_fallback_used"]),
                        "od_history_count": int(od_count),
                        "od_weight": float(od_weight),
                    },
                }
            )
        return results
