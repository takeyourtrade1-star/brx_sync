"""Authoritative CardTrader execution-mode policy.

Every CardTrader mutation must pass through this module immediately before the
external request.  The policy is fail-closed: missing or stale configuration
never grants write access.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import UserSyncSettings
from app.core.config import get_settings


class SyncExecutionMode(str, enum.Enum):
    DEMO = "demo"
    PARTIAL = "partial"
    REAL = "real"


class CardTraderWriteBlockedError(PermissionError):
    """Raised when an external mutation is not explicitly authorised."""


@dataclass(frozen=True)
class SyncPolicySnapshot:
    user_id: uuid.UUID
    execution_mode: SyncExecutionMode
    mode_version: int
    writes_enabled: bool


def policy_from_settings(settings: UserSyncSettings) -> SyncPolicySnapshot:
    try:
        execution_mode = SyncExecutionMode(settings.execution_mode)
    except ValueError as exc:
        raise CardTraderWriteBlockedError("Unknown sync execution mode") from exc

    return SyncPolicySnapshot(
        user_id=settings.user_id,
        execution_mode=execution_mode,
        mode_version=settings.mode_version,
        writes_enabled=settings.writes_enabled,
    )


async def load_sync_policy(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> SyncPolicySnapshot:
    stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    settings = result.scalar_one_or_none()
    if settings is None:
        raise CardTraderWriteBlockedError("Sync settings not found")
    return policy_from_settings(settings)


async def assert_cardtrader_write_allowed(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    expected_mode_version: Optional[int] = None,
) -> SyncPolicySnapshot:
    """Return the current policy only when a real write is authorised."""

    if not get_settings().CARDTRADER_WRITES_ENABLED:
        raise CardTraderWriteBlockedError("Global CardTrader write switch is disabled")

    policy = await load_sync_policy(session, user_id)
    if policy.execution_mode is not SyncExecutionMode.REAL:
        raise CardTraderWriteBlockedError(
            f"CardTrader writes are disabled in {policy.execution_mode.value} mode"
        )
    if not policy.writes_enabled:
        raise CardTraderWriteBlockedError("CardTrader write kill switch is disabled")
    if expected_mode_version is not None and policy.mode_version != expected_mode_version:
        raise CardTraderWriteBlockedError(
            "Sync mode changed after the command was created"
        )
    return policy
