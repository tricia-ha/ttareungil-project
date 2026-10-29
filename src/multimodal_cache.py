"""Persist expensive bicycle calls; explicit context prevents cross-run stale results."""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


class CachedBikeProvider:
    def __init__(self, provider, path, context, departure_bucket_minutes=5):
        self.provider = provider
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS predictions (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        context = dict(context)
        if hasattr(provider, "eta_quantile"):
            self.eta_quantile = provider.eta_quantile
            context["eta_quantile"] = provider.eta_quantile
        self.context = json.dumps(
            context, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        self.departure_bucket_minutes = int(departure_bucket_minutes)
        if self.departure_bucket_minutes <= 0:
            raise ValueError("departure_bucket_minutes는 양수여야 합니다.")
        self.hits = self.misses = 0
        self.batch_calls = 0
        self._values = {}
        self._buckets = {}
        self._identities = {}

    def _bucket(self, departure):
        memo_key = (
            departure.isoformat() if isinstance(departure, datetime) else departure
        )
        if memo_key in self._buckets:
            return self._buckets[memo_key]
        value = (
            datetime.fromisoformat(departure)
            if isinstance(departure, str)
            else departure
        )
        if value.tzinfo is None:
            raise ValueError("출발시각에는 시간대가 필요합니다.")
        minute = value.minute - value.minute % self.departure_bucket_minutes
        bucket = value.replace(minute=minute, second=0, microsecond=0).isoformat()
        if len(self._buckets) >= 512:
            self._buckets.clear()
        self._buckets[memo_key] = bucket
        return bucket

    def _identity(self, edge, departure):
        bucket = self._bucket(departure)
        stable_edge = {k: v for k, v in edge.items() if k != "inventory_from"}
        # Flat scheduler edges repeat across exact arrival times in one ETA
        # bucket. Keep the original persistent hash and detect edge mutations.
        memo = None
        try:
            memo = (
                tuple((k, type(v).__name__, v) for k, v in sorted(stable_edge.items())),
                bucket,
            )
            hash(memo)
            if memo in self._identities:
                return self._identities[memo], bucket
        except TypeError:
            memo = None
        key = hashlib.sha256(
            (self.context + json.dumps([stable_edge, bucket], sort_keys=True)).encode()
        ).hexdigest()
        if memo is not None:
            if len(self._identities) >= 8192:
                self._identities.clear()
            self._identities[memo] = key
        return key, bucket

    def _lookup(self, key):
        if key in self._values:
            return self._values[key]
        row = self.db.execute(
            "SELECT value FROM predictions WHERE key=?", (key,)
        ).fetchone()
        if row is not None:
            self._values[key] = json.loads(row[0])
            return self._values[key]
        return None

    def needs_prediction(self, edge, departure):
        key, _ = self._identity(edge, departure)
        return self._lookup(key) is None

    def eta_reuse_window(self, edge, departure):
        """ETA is constant in this window; availability must be checked separately."""
        key, bucket = self._identity(edge, departure)
        until = datetime.fromisoformat(bucket) + timedelta(
            minutes=self.departure_bucket_minutes
        )
        return key, until.isoformat()

    def _store(self, key, value):
        self._store_many([(key, value)])

    def _store_many(self, entries):
        entries = list(entries)
        # Keep checkpoints per completed batch, preserving cache keys and the
        # rule that forecast failures stay in query memory only.
        persistent = [
            (key, json.dumps(value, ensure_ascii=False, allow_nan=False))
            for key, value in entries
            if not value.get("weather_error")
        ]
        if persistent:
            with self.db:
                self.db.executemany(
                    "INSERT OR REPLACE INTO predictions VALUES (?,?)", persistent
                )
        self._values.update(entries)

    def prefetch(self, edges, departure, max_new=None):
        """Batch only cache misses among outgoing edges already being examined."""
        if not hasattr(self.provider, "predict_eta_batch"):
            return 0
        if max_new is not None and max_new <= 0:
            return 0
        pending = {}
        for edge in edges:
            key, bucket = self._identity(edge, departure)
            if self._lookup(key) is None:
                pending.setdefault(key, (edge, bucket))
                if max_new is not None and len(pending) >= max_new:
                    break
        entries = list(pending.items())
        if max_new is not None:
            entries = entries[: max(0, max_new)]
        if not entries:
            return 0
        # All edges at this state have the same bucket.
        self.batch_calls += 1
        self.misses += len(entries)
        values = self.provider.predict_eta_batch(
            [value[0] for _, value in entries], entries[0][1][1]
        )
        if len(values) != len(entries):
            raise ValueError("일괄 예측 결과 개수가 요청과 다릅니다.")
        self._store_many((key, value) for (key, _), value in zip(entries, values))
        return len(entries)

    def __call__(self, edge, departure):
        key, bucket = self._identity(edge, departure)
        value = self._lookup(key)
        if value is not None:
            self.hits += 1
        else:
            self.misses += 1
            if hasattr(self.provider, "predict_eta"):
                value = self.provider.predict_eta(edge, bucket)
            else:
                value = self.provider(edge, bucket)
            self._store(key, value)
        value = dict(value, eta_departure_bucket=bucket)
        if hasattr(self.provider, "attach_inventory"):
            return self.provider.attach_inventory(value, edge, departure)
        return value

    def precheck_inventory(self, edge, departure):
        if hasattr(self.provider, "precheck_inventory"):
            return self.provider.precheck_inventory(edge, departure)
        return None

    def close(self):
        self.db.close()
