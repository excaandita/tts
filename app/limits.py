"""Batasan akses: CORS dan rate limit per-IP.

Semua nilai dapat diatur lewat environment variable agar satu image bisa dipakai
di beberapa deployment (VPS antrian, server publik, atau lokal).
"""

from __future__ import annotations

import os
import time
from collections import deque


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_rate_limit_per_minute() -> int:
    """0 berarti rate limit dimatikan."""
    return _env_int("RATE_LIMIT_PER_MINUTE", 120, minimum=0)


def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def get_trust_proxy_headers() -> bool:
    """Aktif bila aplikasi berada di belakang reverse proxy (nginx, Caddy)."""
    return _env_bool("TRUST_PROXY_HEADERS", True)


class SlidingWindowLimiter:
    """Rate limiter in-memory: `limit` permintaan per `window_seconds` per kunci.

    Cukup untuk satu proses (uvicorn default 1 worker). Pemakaian memori dibatasi
    lewat `max_keys`, sehingga kunci lama dibuang dan tidak menumpuk.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        max_keys: int = 20_000,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def check(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Kembalikan (diizinkan, detik_tunggu)."""
        if not self.enabled:
            return True, 0.0

        moment = time.monotonic() if now is None else now
        window_start = moment - self.window_seconds

        bucket = self._hits.get(key)
        if bucket is None:
            if len(self._hits) >= self.max_keys:
                self._evict_oldest()
            bucket = deque()
            self._hits[key] = bucket

        while bucket and bucket[0] <= window_start:
            bucket.popleft()

        if len(bucket) >= self.limit:
            retry_after = max(1.0, bucket[0] + self.window_seconds - moment)
            return False, retry_after

        bucket.append(moment)
        return True, 0.0

    def reset(self) -> None:
        self._hits.clear()

    def _evict_oldest(self) -> None:
        oldest_key: str | None = None
        oldest_hit = float("inf")
        for key, bucket in self._hits.items():
            if not bucket:
                oldest_key = key
                break
            if bucket[-1] < oldest_hit:
                oldest_hit = bucket[-1]
                oldest_key = key
        if oldest_key is not None:
            self._hits.pop(oldest_key, None)


def client_key(headers_forwarded_for: str | None, client_host: str | None) -> str:
    """Ambil IP asli klien; hop pertama X-Forwarded-For bila memakai proxy."""
    if headers_forwarded_for:
        first = headers_forwarded_for.split(",")[0].strip()
        if first:
            return first
    return client_host or "unknown"
