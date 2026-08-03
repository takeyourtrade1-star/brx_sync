"""JWT verification must be off-loop, bounded and cancellation-safe."""

import asyncio
import threading
import unittest
from unittest.mock import patch
from weakref import WeakKeyDictionary

from app.core import jwt_executor


async def _wait_until(predicate, attempts: int = 300) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


class JwtExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_loop_admission_retains_slot_after_request_cancel(self) -> None:
        started = threading.Event()
        release = threading.Event()
        loop_thread = threading.get_ident()

        def blocking_verify() -> int:
            started.set()
            release.wait(timeout=2)
            return threading.get_ident()

        with patch.object(jwt_executor, "_loop_slots", WeakKeyDictionary()):
            first = asyncio.create_task(
                jwt_executor.run_bounded_jwt_verification(
                    blocking_verify,
                    max_concurrency=1,
                    queue_timeout=0.02,
                )
            )
            await _wait_until(started.is_set)
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)

            with self.assertRaises(jwt_executor.JwtVerificationCapacityError):
                await jwt_executor.run_bounded_jwt_verification(
                    lambda: None,
                    max_concurrency=1,
                    queue_timeout=0.02,
                )

            first.cancel()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            with self.assertRaises(jwt_executor.JwtVerificationCapacityError):
                await jwt_executor.run_bounded_jwt_verification(
                    lambda: None,
                    max_concurrency=1,
                    queue_timeout=0.02,
                )

            release.set()
            slots = jwt_executor._slots_for_current_loop(1)
            await _wait_until(lambda: slots._value == 1)
            worker_thread = await jwt_executor.run_bounded_jwt_verification(
                threading.get_ident,
                max_concurrency=1,
                queue_timeout=0.02,
            )
            self.assertNotEqual(worker_thread, loop_thread)


if __name__ == "__main__":
    unittest.main()
