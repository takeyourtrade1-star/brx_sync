"""Durable PostgreSQL gate for catalog mappings.

CardTrader stock can be observed before the catalog worker has published the
canonical document to Search.  The MySQL print lookup is therefore not, by
itself, sufficient evidence that a blueprint is ready for marketplace use.
This module keeps that decision in PostgreSQL so every synchronizer process
uses the same durable state.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import exists, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import CatalogImportJob, CatalogIndexOutbox

_PROVIDER = "cardtrader"
_MAGIC_GAME_ID = 1
_JOB_SUCCEEDED = "succeeded"
_OUTBOX_SUCCEEDED = "succeeded"


def catalog_mapping_allowed_for(blueprint_reference: Any):
    """Return a SQL predicate allowing a canonical mapping for a blueprint.

    No queue row means the mapping predates the catalog repair workflow and is
    allowed.  Once a global catalog job exists, both the job and one Search
    outbox row must be durably ``succeeded``.  The expression is intentionally
    correlated to the caller's blueprint column when used in an inventory
    UPDATE/INSERT ... SELECT, so the final write is guarded in the same PG
    statement as the stock upsert.
    """

    search_ack = exists(
        select(1).where(
            CatalogIndexOutbox.job_id == CatalogImportJob.id,
            CatalogIndexOutbox.status == _OUTBOX_SUCCEEDED,
        )
    )
    blocking_job = exists(
        select(1).where(
            CatalogImportJob.provider == _PROVIDER,
            CatalogImportJob.game_id == _MAGIC_GAME_ID,
            CatalogImportJob.blueprint_id == blueprint_reference,
            or_(
                CatalogImportJob.status != _JOB_SUCCEEDED,
                ~search_ack,
            ),
        )
    )
    return ~blocking_job


async def blocked_catalog_blueprints(
    session: AsyncSession,
    blueprint_ids: Iterable[int],
) -> set[int]:
    """Return only blueprint IDs whose catalog repair is not Search-ACKed.

    The result deliberately omits blueprints with no job.  It is used to split
    a mapper result before persistence; the SQL predicate above remains the
    write-time defence for a job committed between that read and the upsert.
    """

    ids = sorted({int(value) for value in blueprint_ids})
    if not ids:
        return set()

    search_ack = exists(
        select(1).where(
            CatalogIndexOutbox.job_id == CatalogImportJob.id,
            CatalogIndexOutbox.status == _OUTBOX_SUCCEEDED,
        )
    ).label("search_ack")
    result = await session.execute(
        select(CatalogImportJob.blueprint_id, CatalogImportJob.status, search_ack).where(
            CatalogImportJob.provider == _PROVIDER,
            CatalogImportJob.game_id == _MAGIC_GAME_ID,
            CatalogImportJob.blueprint_id.in_(ids),
        )
    )
    return {
        int(blueprint_id)
        for blueprint_id, status, has_search_ack in result.all()
        if str(status) != _JOB_SUCCEEDED or not bool(has_search_ack)
    }


async def catalog_mapping_blocked(
    session: AsyncSession,
    blueprint_id: int,
) -> bool:
    """Evaluate the durable gate for one blueprint at the current statement."""

    result = await session.execute(
        select(~catalog_mapping_allowed_for(int(blueprint_id)))
    )
    return bool(result.scalar_one())
