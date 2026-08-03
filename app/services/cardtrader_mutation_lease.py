"""Distributed per-account lease shared by CardTrader mutation paths."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.core.redis_client import get_redis_sync

logger = logging.getLogger(__name__)

LEASE_SECONDS = 300
LOCK_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
LOCK_REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class CardTraderMutationBusyError(RuntimeError):
    """Another process owns the CardTrader account mutation lease."""


def cardtrader_mutation_lock_key(user_id: uuid.UUID) -> str:
    return f"cardtrader:outbox:user-lock:{user_id}"


@dataclass
class CardTraderMutationLease:
    redis: object
    key: str
    owner: str
    lost_error: BaseException | None = None

    def refresh(self) -> None:
        if self.lost_error is not None:
            raise CardTraderMutationBusyError(
                "CardTrader mutation lease was lost"
            ) from self.lost_error
        self._refresh_now()

    def _refresh_now(self) -> None:
        refreshed = self.redis.eval(
            LOCK_REFRESH_SCRIPT,
            1,
            self.key,
            self.owner,
            LEASE_SECONDS,
        )
        if refreshed != 1:
            raise CardTraderMutationBusyError("CardTrader mutation lease was lost")


async def _lease_heartbeat(
    lease: CardTraderMutationLease,
    stopped: asyncio.Event,
) -> None:
    interval = max(0.05, LEASE_SECONDS / 3)
    while not stopped.is_set():
        try:
            await asyncio.wait_for(stopped.wait(), timeout=interval)
        except TimeoutError:
            try:
                lease._refresh_now()
            except BaseException as exc:  # Redis client failures vary.
                lease.lost_error = exc
                return


@asynccontextmanager
async def cardtrader_mutation_lease(
    user_id: uuid.UUID,
) -> AsyncIterator[CardTraderMutationLease]:
    key = cardtrader_mutation_lock_key(user_id)
    owner = str(uuid.uuid4())
    try:
        redis = get_redis_sync()
        acquired = redis.set(key, owner, nx=True, ex=LEASE_SECONDS)
    except Exception as exc:
        raise CardTraderMutationBusyError("CardTrader mutation lease unavailable") from exc
    if not acquired:
        raise CardTraderMutationBusyError("Another CardTrader mutation is already running")

    lease = CardTraderMutationLease(redis=redis, key=key, owner=owner)
    stopped = asyncio.Event()
    heartbeat = asyncio.create_task(_lease_heartbeat(lease, stopped))
    body_completed = False
    try:
        yield lease
        body_completed = True
    finally:
        stopped.set()
        await heartbeat
        try:
            redis.eval(LOCK_RELEASE_SCRIPT, 1, key, owner)
        except Exception as exc:  # noqa: BLE001 - Redis client errors vary
            # The TTL is the final safety net if Redis is unavailable here.
            logger.warning(
                "Unable to release CardTrader mutation lease (%s)",
                type(exc).__name__,
            )
        if body_completed and lease.lost_error is not None:
            raise CardTraderMutationBusyError(
                "CardTrader mutation lease was lost during operation"
            ) from lease.lost_error
