"""Durable state for CardTrader catalog imports.

The inventory tables are owned by the synchronizer and contain stock.  These
tables deliberately contain only catalog work and observations.  A catalog
retry must therefore never mutate ``user_inventory_items.quantity``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.inventory import Base


class CatalogImportStatus(str, enum.Enum):
    """State machine for one globally deduplicated blueprint import."""

    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    SUCCEEDED = "succeeded"


class CatalogOutboxStatus(str, enum.Enum):
    """State machine for the post-MySQL Search indexing outbox."""

    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    SUCCEEDED = "succeeded"


class CatalogImportJob(Base):
    """One shared import job for provider + game + exact blueprint ID."""

    __tablename__ = "catalog_import_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="cardtrader", server_default="cardtrader"
    )
    game_id: Mapped[int] = mapped_column(Integer, nullable=False)
    blueprint_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CatalogImportStatus.PENDING.value,
        server_default=CatalogImportStatus.PENDING.value, index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    lease_token: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, index=True
    )
    source_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    result_json: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "provider", "game_id", "blueprint_id", name="uq_catalog_import_provider_game_blueprint"
        ),
        CheckConstraint("game_id > 0", name="ck_catalog_import_jobs_game_id"),
        CheckConstraint("blueprint_id > 0", name="ck_catalog_import_jobs_blueprint_id"),
        CheckConstraint("attempts >= 0", name="ck_catalog_import_jobs_attempts"),
        CheckConstraint(
            "status IN ('pending','running','failed','needs_review','succeeded')",
            name="ck_catalog_import_jobs_status",
        ),
        Index("idx_catalog_import_jobs_due", "status", "next_attempt_at"),
    )


class CatalogImportRequest(Base):
    """Per-user stock observation that caused or reused a shared job."""

    __tablename__ = "catalog_import_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog_import_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    game_id: Mapped[int] = mapped_column(Integer, nullable=False)
    blueprint_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_stock_id: Mapped[str] = mapped_column(String(255), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    # Snapshot of the active server-side sync profile used for this request.
    # It is intentionally not copied from the CardTrader product payload.
    mode_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    product_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "user_id",
            "external_stock_id",
            "environment",
            name="uq_catalog_import_request_observation",
        ),
        CheckConstraint("game_id > 0", name="ck_catalog_import_requests_game_id"),
        CheckConstraint("blueprint_id > 0", name="ck_catalog_import_requests_blueprint_id"),
        CheckConstraint(
            "environment IN ('demo','partial','real')",
            name="ck_catalog_import_requests_environment",
        ),
        CheckConstraint("mode_version > 0", name="ck_catalog_import_requests_mode_version"),
    )


class CatalogIndexOutbox(Base):
    """Durable Search publication created after the MySQL commit."""

    __tablename__ = "catalog_index_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("catalog_import_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[str] = mapped_column(String(255), nullable=False)
    document_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CatalogOutboxStatus.PENDING.value,
        server_default=CatalogOutboxStatus.PENDING.value, index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    lease_token: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("job_id", "document_id", name="uq_catalog_index_outbox_job_document"),
        CheckConstraint("attempts >= 0", name="ck_catalog_index_outbox_attempts"),
        CheckConstraint(
            "status IN ('pending','running','failed','needs_review','succeeded')",
            name="ck_catalog_index_outbox_status",
        ),
        Index("idx_catalog_index_outbox_due", "status", "next_attempt_at"),
    )
