"""Bounded single-flight cache for unauthenticated readiness probes."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class AsyncProbeCache:
    def __init__(
        self,
        probe: Callable[[], Awaitable[bool]],
        *,
        ttl_seconds: float = 3.0,
        timeout_seconds: float = 2.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 2.0 <= ttl_seconds <= 5.0:
            raise ValueError("probe cache TTL must be between 2 and 5 seconds")
        if not 0 < timeout_seconds <= ttl_seconds:
            raise ValueError("probe timeout must be positive and no greater than its TTL")
        self._probe = probe
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._clock = monotonic_clock
        self._lock = asyncio.Lock()
        self._in_flight: asyncio.Task[bool] | None = None
        self._cached_value: bool | None = None
        self._expires_at = 0.0

    async def get(self) -> bool:
        if self._cached_value is not None and self._clock() < self._expires_at:
            return self._cached_value
        async with self._lock:
            if self._cached_value is not None and self._clock() < self._expires_at:
                return self._cached_value
            if self._in_flight is None:
                self._in_flight = asyncio.create_task(self._execute())
            task = self._in_flight
        return await asyncio.shield(task)

    async def _execute(self) -> bool:
        current = asyncio.current_task()
        try:
            value = bool(
                await asyncio.wait_for(self._probe(), timeout=self._timeout_seconds)
            )
        except Exception:
            value = False
        async with self._lock:
            if self._in_flight is current:
                self._cached_value = value
                self._expires_at = self._clock() + self._ttl_seconds
                self._in_flight = None
        return value
