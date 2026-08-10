"""
SQLAlchemy models for BRX Sync database tables.
"""

import enum
import uuid
from datetime import datetime
from typing import Literal, Optional

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
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
        UUID(as_uuid=True), primary_key=True, comment="User UUID (references users table)"
    )
    cardtrader_token_encrypted: Mapped[str] = mapped_column(
        Text, nullable=False, comment="CardTrader API token encrypted with Fernet"
    )
    webhook_secret: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Fernet-encrypted CardTrader webhook shared secret",
    )
    sync_status: Mapped[str] = mapped_column(
        PGEnum(
            *(status.value for status in SyncStatusEnum),
            name="sync_status_enum",
        ),
        nullable=False,
        default=SyncStatusEnum.IDLE.value,
        server_default=SyncStatusEnum.IDLE.value,
        comment="Current sync status",
    )
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="Last successful sync timestamp"
    )
    last_error: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="Last error message if sync failed"
    )
    execution_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="demo", server_default="demo"
    )
    mode_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    writes_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    mode_changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "execution_mode IN ('demo', 'partial', 'real')",
            name="ck_user_sync_settings_execution_mode",
        ),
        CheckConstraint("mode_version > 0", name="ck_user_sync_settings_mode_version"),
        CheckConstraint(
            "NOT writes_enabled OR execution_mode = 'real'",
            name="ck_user_sync_settings_real_writes_only",
        ),
    )


class UserInventoryItem(Base):
    """User inventory items synchronized from CardTrader."""

    __tablename__ = "user_inventory_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="User UUID (FK reale verso users.id, posseduta dal DB condiviso)",
    )
    blueprint_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="CardTrader blueprint_id"
    )
    game_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="CardTrader game identifier; only Magic (1) is currently supported",
    )
    quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Current quantity in stock"
    )
    reserved_quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Quantity held by accepted trades and temporarily unavailable",
    )
    price_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Price in cents (to avoid floating point errors)"
    )
    properties: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Product properties: {condition, mtg_foil, mtg_language, signed, altered, ...}",
    )
    external_stock_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True, comment="CardTrader product.id for targeted updates"
    )
    source: Mapped[Literal["cardtrader", "trade", "internal_test"]] = mapped_column(
        String(32),
        nullable=False,
        default="internal_test",
        server_default="internal_test",
        comment="Inventory origin: cardtrader, trade, or internal_test",
    )
    environment: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="real",
        server_default="real",
        comment="Inventory namespace: demo, partial, or real",
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default="active",
    )
    sync_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="synced",
        server_default="synced",
    )
    sync_uncertain_event_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Webhook inbox watermark that must be reconciled before the row is tradable",
    )
    row_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    mapping_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="mapped", server_default="mapped"
    )
    missing_snapshot_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_seen_snapshot_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    last_external_update_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="Product description visible to all users"
    )
    user_data_field: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Custom metadata field for internal use (warehouse location, etc.)",
    )
    graded: Mapped[Optional[bool]] = mapped_column(
        nullable=True, comment="Whether the product is graded (top-level field, not in properties)"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "environment",
            "blueprint_id",
            "external_stock_id",
            name="uq_user_environment_blueprint_external_stock",
        ),
        CheckConstraint(
            "source IN ('cardtrader', 'trade', 'internal_test')",
            name="ck_user_inventory_items_source",
        ),
        CheckConstraint(
            "reserved_quantity >= 0",
            name="ck_user_inventory_items_reserved_quantity",
        ),
        CheckConstraint(
            "environment IN ('demo', 'partial', 'real')",
            name="ck_user_inventory_items_environment",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active','sold_out','stale','archived','pending_delete','sync_failed')",
            name="ck_user_inventory_items_lifecycle_status",
        ),
        CheckConstraint(
            "sync_state IN ('synced','pending','accepted','failed','uncertain')",
            name="ck_user_inventory_items_sync_state",
        ),
        CheckConstraint("row_version > 0", name="ck_user_inventory_items_row_version"),
        CheckConstraint(
            "mapping_status IN ('mapped','unsupported','missing','error')",
            name="ck_user_inventory_items_mapping_status",
        ),
        CheckConstraint(
            "missing_snapshot_count >= 0",
            name="ck_user_inventory_items_missing_snapshot_count",
        ),
        CheckConstraint(
            "game_id IS NULL OR game_id = 1",
            name="ck_user_inventory_items_magic_only",
        ),
        Index(
            "idx_inventory_user_source_quantity",
            "user_id",
            "source",
            "quantity",
        ),
        Index(
            "idx_inventory_sync_uncertain_watermark",
            "user_id",
            "sync_state",
            "sync_uncertain_event_id",
        ),
        Index("idx_inventory_user_game", "user_id", "game_id"),
    )


class InventoryOperation(Base):
    """Idempotency and audit log for internal inventory mutations."""

    __tablename__ = "inventory_ops"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    op_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('reserve', 'release', 'consume', 'credit')",
            name="ck_inventory_ops_kind",
        ),
        CheckConstraint(
            "status IN ('processing', 'succeeded', 'failed')",
            name="ck_inventory_ops_status",
        ),
    )


class SyncOperation(Base):
    """Sync operations log for idempotency and audit."""

    __tablename__ = "sync_operations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_sync_settings.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User UUID",
    )
    operation_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True, comment="UUID for idempotency"
    )
    operation_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="Operation type: bulk_sync, update, webhook"
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Operation status: pending, completed, failed",
    )
    operation_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True, comment="Additional operation metadata"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    completed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, comment="Operation completion timestamp"
    )


class WebhookInbox(Base):
    """Durable, idempotent inbox for CardTrader webhook delivery."""

    __tablename__ = "cardtrader_webhook_inbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    webhook_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    cause: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="live")
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="received", server_default="received", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    result_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('received','processing','completed','failed','ignored',"
            "'deferred','reconcile_pending')",
            name="ck_cardtrader_webhook_inbox_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_cardtrader_webhook_inbox_attempts"),
    )


class OrderStockLedger(Base):
    """Exactly-once stock delta applied for each CardTrader order item."""

    __tablename__ = "cardtrader_order_stock_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    external_stock_id: Mapped[str] = mapped_column(String(255), nullable=False)
    environment: Mapped[str] = mapped_column(
        String(20), nullable=False, default="real", server_default="real"
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    via_cardtrader_zero: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    decrement_applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    restore_applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_webhook_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "order_id",
            "external_stock_id",
            "environment",
            name="uq_cardtrader_order_stock_item",
        ),
        CheckConstraint(
            "environment IN ('partial','real')",
            name="ck_cardtrader_order_stock_environment",
        ),
        CheckConstraint("quantity >= 0", name="ck_cardtrader_order_stock_quantity"),
        CheckConstraint(
            "NOT restore_applied OR decrement_applied",
            name="ck_cardtrader_order_stock_restore_after_decrement",
        ),
    )


class CardTraderOutbox(Base):
    """Durable CardTrader mutation command."""

    __tablename__ = "cardtrader_sync_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    mode_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    inventory_item_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    expected_row_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    context_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending", index=True
    )
    job_uuid: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "operation_type IN ('create_product','update_product','delete_product')",
            name="ck_cardtrader_sync_outbox_operation",
        ),
        CheckConstraint(
            "status IN ('pending','running','accepted','verified','failed','uncertain','cancelled')",
            name="ck_cardtrader_sync_outbox_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_cardtrader_sync_outbox_attempts"),
        Index("idx_cardtrader_sync_outbox_pending", "status", "created_at"),
    )


class SyncSnapshot(Base):
    """Durable evidence for one complete CardTrader export."""

    __tablename__ = "cardtrader_sync_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    environment: Mapped[str] = mapped_column(
        String(20), nullable=False, default="real", server_default="real"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    product_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    problems_json: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    result_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('validating','rejected','applied')",
            name="ck_cardtrader_sync_snapshots_status",
        ),
        CheckConstraint(
            "environment IN ('partial','real')",
            name="ck_cardtrader_sync_snapshots_environment",
        ),
        Index(
            "idx_cardtrader_sync_snapshots_user_environment_created",
            "user_id",
            "environment",
            "created_at",
        ),
    )
