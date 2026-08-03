from __future__ import annotations

import asyncio
import unittest

from app.core.probe_cache import AsyncProbeCache


class ProbeCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_and_cached_requests_execute_one_probe(self) -> None:
        calls = 0
        now = [10.0]

        async def probe() -> bool:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return True

        cache = AsyncProbeCache(
            probe,
            ttl_seconds=3.0,
            timeout_seconds=1.0,
            monotonic_clock=lambda: now[0],
        )
        self.assertEqual([True] * 20, await asyncio.gather(*(cache.get() for _ in range(20))))
        self.assertTrue(await cache.get())
        self.assertEqual(1, calls)
        now[0] += 4.0
        self.assertTrue(await cache.get())
        self.assertEqual(2, calls)

    async def test_probe_timeout_fails_closed_and_is_cached(self) -> None:
        calls = 0

        async def probe() -> bool:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.1)
            return True

        cache = AsyncProbeCache(probe, ttl_seconds=2.0, timeout_seconds=0.01)
        self.assertFalse(await cache.get())
        self.assertFalse(await cache.get())
        self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
