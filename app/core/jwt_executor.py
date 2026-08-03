"""Bounded off-event-loop execution for CPU-heavy JWT verification."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar
from weakref import WeakKeyDictionary


T = TypeVar("T")


class JwtVerificationCapacityError(RuntimeError):
    """JWT verification admission is saturated."""


_loop_slots: WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[int, asyncio.BoundedSemaphore]
] = WeakKeyDictionary()


def _slots_for_current_loop(max_concurrency: int) -> asyncio.BoundedSemaphore:
    loop = asyncio.get_running_loop()
    state = _loop_slots.get(loop)
    if state is None:
        semaphore = asyncio.BoundedSemaphore(max_concurrency)
        _loop_slots[loop] = (max_concurrency, semaphore)
        return semaphore
    configured, semaphore = state
    if configured != max_concurrency:
        raise RuntimeError("JWT verification concurrency changed after startup")
    return semaphore


async def run_bounded_jwt_verification(
    operation: Callable[..., T],
    *args: object,
    max_concurrency: int,
    queue_timeout: float,
) -> T:
    """Admit bounded work, run it in a thread and retain the slot on cancel."""

    slots = _slots_for_current_loop(max_concurrency)
    try:
        await asyncio.wait_for(slots.acquire(), timeout=queue_timeout)
    except TimeoutError as exc:
        raise JwtVerificationCapacityError() from exc

    try:
        worker = asyncio.create_task(asyncio.to_thread(operation, *args))
    except Exception:
        slots.release()
        raise

    def release_when_worker_finishes(done: asyncio.Task[T]) -> None:
        slots.release()
        if not done.cancelled():
            done.exception()

    worker.add_done_callback(release_when_worker_finishes)
    return await asyncio.shield(worker)
