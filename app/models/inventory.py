"""
SQLAlchemy models for BRX Sync database tables.
"""
import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


class SyncStatusEnum(enum.Enum):
    """Sync status enumeration."""
    IDLE = "idle"
    INITIAL_SYNC = "initial_sync"
    ACTIVE = "active"
    ERROR = "error"
    
    def __str__(self):
        return self.value


class UserSyncSettings(Base):
    """User sync settings and configuration."""

    __tablename__ = "user_sync_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        comment="User UUID (references users table)"
    )
    cardtrader_token_encrypted: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="CardTrader API token encrypted with Fernet"
    )
    webhook_secret: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Webhook shared_secret from CardTrader /info endpoint"
    )
    sync_status: Mapped[str] = mapped_column(
        String(50),  # Use String instead of Enum to avoid validation issues
        nullable=False,
        default=SyncStatusEnum.IDLE.value,
        comment="Current sync status (stored as PostgreSQL enum sync_status_enum)"
    )
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Last successful sync timestamp"
    )
    last_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Last error message if sync failed"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )


class UserInventoryItem(Base):
    """User inventory items synchronized from CardTrader."""

    __tablename__ = "user_inventory_items"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_sync_settings.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User UUID"
    )
    blueprint_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
        comment="CardTrader blueprint_id"
    )
    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Current quantity in stock"
    )
    price_cents: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Price in cents (to avoid floating point errors)"
    )
    properties: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Product properties: {condition, mtg_foil, mtg_language, signed, altered, ...}"
    )
    external_stock_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="CardTrader product.id for targeted updates"
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Product description visible to all users"
    )
    user_data_field: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Custom metadata field for internal use (warehouse location, etc.)"
    )
    graded: Mapped[Optional[bool]] = mapped_column(
        nullable=True,
        comment="Whether the product is graded (top-level field, not in properties)"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "blueprint_id",
            "external_stock_id",
            name="uq_user_blueprint_external_stock"
        ),
    )


class SyncOperation(Base):
    """Sync operations log for idempotency and audit."""

    __tablename__ = "sync_operations"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_sync_settings.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User UUID"
    )
    operation_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="UUID for idempotency"
    )
    operation_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Operation type: bulk_sync, update, webhook"
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Operation status: pending, completed, failed"
    )
    operation_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Additional operation metadata"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
        comment="Operation completion timestamp"
    )
