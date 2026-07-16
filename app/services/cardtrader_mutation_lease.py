"""Distributed per-account lease shared by CardTrader mutation paths."""

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from app.core.redis_client import get_redis_sync

LEASE_SECONDS = 90
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

    def refresh(self) -> None:
        refreshed = self.redis.eval(
            LOCK_REFRESH_SCRIPT,
            1,
            self.key,
            self.owner,
            LEASE_SECONDS,
        )
        if refreshed != 1:
            raise CardTraderMutationBusyError("CardTrader mutation lease was lost")


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
        raise CardTraderMutationBusyError(
            "CardTrader mutation lease unavailable"
        ) from exc
    if not acquired:
        raise CardTraderMutationBusyError(
            "Another CardTrader mutation is already running"
        )

    lease = CardTraderMutationLease(redis=redis, key=key, owner=owner)
    try:
        yield lease
    finally:
        try:
            redis.eval(LOCK_RELEASE_SCRIPT, 1, key, owner)
        except Exception:
            # The TTL is the final safety net if Redis is unavailable here.
            pass
