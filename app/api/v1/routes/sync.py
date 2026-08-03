"""
API endpoints for sync operations.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user_id, verify_user_id_match
from app.api.v1.schemas import (
    DeleteInventoryItemResponse,
    DisconnectSyncRequest,
    InventoryItemResponse,
    InventoryResponse,
    ListingItemResponse,
    ListingsByBlueprintResponse,
    PurchaseItemRequest,
    PurchaseItemResponse,
    SetupTestUserRequest,
    SyncStartResponse,
    SyncStatusResponse,
    UpdateInventoryItemRequest,
    UpdateInventoryItemResponse,
)
from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.exceptions import (
    InventoryItemMissingExternalIdError,
    InventoryItemNotFoundError,
    SyncNotFoundError,
)
from app.core.exceptions import (
    ValidationError as BRXValidationError,
)
from app.core.webhook_validator import (
    WebhookValidationError,
    enforce_webhook_rate_limit,
    verify_webhook,
)
from app.models.inventory import (
    CardTraderOutbox,
    InventoryOperation,
    SyncOperation,
    SyncStatusEnum,
    UserInventoryItem,
    UserSyncSettings,
    WebhookInbox,
)
from app.services.cardtrader_payloads import build_product_update_payload
from app.services.sync_policy import (
    CardTraderWriteBlockedError,
    assert_cardtrader_write_allowed,
)
from app.services.webhook_ledger_processor import WebhookLedgerProcessor
from app.tasks.outbox_tasks import process_cardtrader_outbox_command
from app.tasks.periodic_sync import reconcile_user
from app.tasks.sync_tasks import (
    initial_bulk_sync,
    process_webhook_notification,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sync", tags=["sync"])
settings = get_settings()

_ACTIVE_OUTBOX_STATUSES = ("pending", "running", "accepted")
_POLICY_BLOCKING_OUTBOX_STATUSES = ("pending", "running", "accepted", "uncertain")


class WebhookBodyTooLarge(ValueError):
    pass


async def _read_webhook_body(request: Request) -> bytes:
    limit = settings.WEBHOOK_MAX_BODY_BYTES
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > limit:
        raise WebhookBodyTooLarge()

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise WebhookBodyTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


async def _register_task_before_enqueue(
    session: AsyncSession,
    user_id: uuid.UUID,
    task_id: str,
    operation_type: str,
) -> None:
    """Persist task ownership before Celery can start processing it."""

    session.add(
        SyncOperation(
            user_id=user_id,
            operation_id=task_id,
            operation_type=operation_type,
            status="pending",
        )
    )
    if operation_type == "bulk_sync":
        await session.execute(
            text("""
                UPDATE user_sync_settings
                SET sync_status = CAST('initial_sync' AS sync_status_enum),
                    last_error = NULL,
                    updated_at = NOW()
                WHERE user_id = CAST(:user_id AS uuid)
                """),
            {"user_id": str(user_id)},
        )
    await session.commit()


async def _assert_no_unresolved_inventory_mutation(
    session: AsyncSession,
    item: UserInventoryItem,
) -> None:
    """Reject edits until the preceding CardTrader mutation is resolved."""
    if item.source == "cardtrader" and item.game_id != 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Oggetto non riconciliato come Magic; modifica CardTrader bloccata.",
        )
    if item.sync_state != "synced" or item.sync_uncertain_event_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Oggetto in sincronizzazione o in riconciliazione; "
                "attendi la conclusione prima di modificarlo di nuovo."
            ),
        )

    unresolved_command_id = (
        await session.execute(
            select(CardTraderOutbox.id)
            .where(
                CardTraderOutbox.inventory_item_id == item.id,
                CardTraderOutbox.status.in_(_ACTIVE_OUTBOX_STATUSES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if unresolved_command_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=("Esiste già una modifica CardTrader non conclusa per questo oggetto."),
        )


async def _assert_user_has_no_unresolved_mutations(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> None:
    unresolved_outbox = (
        await session.execute(
            select(CardTraderOutbox.id)
            .where(
                CardTraderOutbox.user_id == user_id,
                CardTraderOutbox.status.in_(_POLICY_BLOCKING_OUTBOX_STATUSES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    processing_inventory = (
        await session.execute(
            select(InventoryOperation.id)
            .where(
                InventoryOperation.status == "processing",
                InventoryOperation.payload_json["user_id"].astext == str(user_id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if unresolved_outbox is not None or processing_inventory is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Operazioni inventario/CardTrader ancora in corso; "
                "completare la riconciliazione prima di cambiare collegamento."
            ),
        )


async def _mark_enqueue_failed(
    session: AsyncSession,
    task_id: str,
    error: Exception,
) -> None:
    await session.execute(
        update(SyncOperation)
        .where(SyncOperation.operation_id == task_id)
        .values(
            status="failed",
            completed_at=datetime.utcnow(),
            operation_metadata={"enqueue_error_type": type(error).__name__},
        )
    )
    await session.execute(
        text("""
            UPDATE user_sync_settings AS settings
            SET sync_status = CAST('error' AS sync_status_enum),
                last_error = :error,
                updated_at = NOW()
            FROM sync_operations AS operation
            WHERE operation.operation_id = :task_id
              AND operation.operation_type = 'bulk_sync'
              AND settings.user_id = operation.user_id
            """),
        {"task_id": task_id, "error": "enqueue failed"},
    )
    await session.commit()


@router.post("/migrate/composite-index", status_code=status.HTTP_200_OK)
async def apply_composite_index_migration(
    user_id_from_token: str = Depends(get_current_user_id),
) -> dict:
    """Legacy endpoint intentionally disabled: migrations run only at deploy."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Endpoint migrazione disabilitato; usare la pipeline di deploy.",
    )


@router.post("/start/{user_id}", status_code=status.HTTP_202_ACCEPTED)
async def start_sync(
    user_id: str,
    force: bool = False,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> SyncStartResponse:
    """
    Start initial bulk sync for user.

    Args:
        user_id: User UUID
        force: If True, allow sync even if status is 'active' or 'initial_sync'

    Returns:
        Task ID and status
    """
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id format"
        )

    # Check if sync settings exist
    # This row is the per-user admission mutex. Keep it locked through the
    # active-operation check and durable task registration so two API workers
    # cannot both publish bulk/reconcile work for the same user.
    stmt = (
        select(UserSyncSettings)
        .where(UserSyncSettings.user_id == user_uuid)
        .with_for_update()
    )
    result = await session.execute(stmt)
    sync_settings = result.scalar_one_or_none()

    if not sync_settings:
        raise SyncNotFoundError(user_id=user_id)

    # Reject if CardTrader link was removed (empty token)
    try:
        from app.core.crypto import get_encryption_manager

        enc = get_encryption_manager()
        token = enc.decrypt(sync_settings.cardtrader_token_encrypted)
        if not (token and token.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Collegamento CardTrader non configurato. Inserisci il token nello Step 1 e salva.",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collegamento CardTrader non configurato. Inserisci il token nello Step 1 e salva.",
        )

    if force:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Il force sync è disabilitato: usa la riconciliazione single-flight.",
        )

    status_value = (
        sync_settings.sync_status
        if isinstance(sync_settings.sync_status, str)
        else sync_settings.sync_status.value
    )
    if sync_settings.execution_mode not in {"partial", "real"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Seleziona prima la modalita parziale o reale.",
        )

    if status_value == SyncStatusEnum.INITIAL_SYNC.value:
        active_after = datetime.now(timezone.utc) - timedelta(minutes=45)
        existing = (
            await session.execute(
                select(SyncOperation)
                .where(
                    SyncOperation.user_id == user_uuid,
                    SyncOperation.operation_type == "bulk_sync",
                    SyncOperation.status.in_(["pending", "processing"]),
                    SyncOperation.created_at >= active_after,
                )
                .order_by(SyncOperation.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return SyncStartResponse(
                status="accepted",
                task_id=existing.operation_id,
                user_id=user_id,
                message="Sincronizzazione iniziale già in corso",
            )
        await session.execute(
            update(SyncOperation)
            .where(
                SyncOperation.user_id == user_uuid,
                SyncOperation.operation_type == "bulk_sync",
                SyncOperation.status.in_(["pending", "processing"]),
            )
            .values(
                status="failed",
                completed_at=datetime.now(timezone.utc),
                operation_metadata={"error": "stale operation recovered by API"},
            )
        )
        status_value = SyncStatusEnum.ERROR.value

    operation_type = "reconcile" if status_value == SyncStatusEnum.ACTIVE.value else "bulk_sync"
    if operation_type == "reconcile":
        active_after = datetime.now(timezone.utc) - timedelta(minutes=45)
        existing = (
            await session.execute(
                select(SyncOperation)
                .where(
                    SyncOperation.user_id == user_uuid,
                    SyncOperation.operation_type == "reconcile",
                    SyncOperation.status.in_(["pending", "processing"]),
                    SyncOperation.created_at >= active_after,
                )
                .order_by(SyncOperation.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return SyncStartResponse(
                status="accepted",
                task_id=existing.operation_id,
                user_id=user_id,
                message="Riconciliazione gia in corso",
            )
    task_id = str(uuid.uuid4())
    await _register_task_before_enqueue(session, user_uuid, task_id, operation_type)

    try:
        if operation_type == "reconcile":
            task = reconcile_user.apply_async(
                kwargs={"user_id": user_id},
                task_id=task_id,
            )
        else:
            task = initial_bulk_sync.apply_async(args=[user_id], task_id=task_id)
    except Exception as exc:
        await _mark_enqueue_failed(session, task_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Impossibile accodare la sincronizzazione",
        ) from exc

    return SyncStartResponse(
        status="accepted",
        task_id=task.id,
        user_id=user_id,
        message=(
            "Riconciliazione avviata"
            if operation_type == "reconcile"
            else "Sincronizzazione iniziale avviata"
        ),
    )


@router.get("/task/{task_id}")
async def get_task_status(
    task_id: str = Path(
        ...,
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$",
    ),
    user_id_from_token: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Get Celery task status by task ID.

    Verifies that the task belongs to the authenticated user by checking SyncOperation.

    Args:
        task_id: Celery task ID
        user_id_from_token: User ID from JWT token (automatically extracted)

    Returns:
        Task status and result

    Raises:
        HTTPException 403: If task doesn't belong to the authenticated user
    """
    try:
        try:
            user_uuid = uuid.UUID(user_id_from_token)
            normalized_task_id = str(uuid.UUID(task_id))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
            ) from exc

        # Query by owner as well as task ID: callers cannot use status differences
        # to discover another user's task identifiers.
        stmt = select(SyncOperation).where(
            SyncOperation.operation_id == normalized_task_id,
            SyncOperation.user_id == user_uuid,
        )
        result = await session.execute(stmt)
        sync_op = result.scalar_one_or_none()

        if sync_op is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Task not found",
            )

        if sync_op.status in {"completed", "failed", "uncertain", "cancelled"}:
            successful = sync_op.status == "completed"
            metadata = sync_op.operation_metadata or {}
            safe_result_keys = {
                "processed",
                "created",
                "updated",
                "skipped",
                "total_products",
                "progress_percent",
            }
            safe_result = {
                key: value
                for key, value in metadata.items()
                if key in safe_result_keys and isinstance(value, (bool, int, float, str))
            }
            return {
                "task_id": normalized_task_id,
                "status": "SUCCESS" if successful else "FAILURE",
                "ready": True,
                "result": safe_result if successful else None,
                "error": None if successful else "Task failed",
                "message": (
                    "Task completed successfully"
                    if successful else "Task could not be completed"
                ),
            }

        public_state = {
            "pending": "PENDING",
            "processing": "STARTED",
            "running": "STARTED",
            "retry": "RETRY",
        }.get(sync_op.status, "PENDING")
        return {
            "task_id": normalized_task_id,
            "status": public_state,
            "ready": False,
            "message": (
                "Task is currently running"
                if public_state == "STARTED"
                else "Task is waiting to be processed"
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Task status lookup failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error retrieving task status",
        ) from exc


@router.get("/progress/{user_id}")
async def get_sync_progress(
    user_id: str,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Get real-time sync progress for a user.

    Args:
        user_id: User UUID

    Returns:
        Progress information including percentage, chunks processed, etc.
    """
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id format"
        )

    # Get the most recent sync operation for this user
    stmt = (
        select(SyncOperation)
        .where(SyncOperation.user_id == user_uuid)
        .where(SyncOperation.operation_type.in_(["bulk_sync", "reconcile"]))
        .order_by(SyncOperation.created_at.desc())
        .limit(1)
    )

    result = await session.execute(stmt)
    sync_op = result.scalar_one_or_none()

    if not sync_op:
        return {
            "user_id": user_id,
            "status": "no_sync_found",
            "message": "No sync operation found for this user",
            "progress_percent": 0,
        }

    # Extract progress from metadata
    metadata = sync_op.operation_metadata or {}
    progress_pct = metadata.get("progress_percent", 0)
    total_chunks = metadata.get("total_chunks", 0)
    processed_chunks = metadata.get("processed_chunks", 0)
    total_products = metadata.get("total_products", 0)
    processed = metadata.get("processed", 0)
    created = metadata.get("created", 0)
    updated = metadata.get("updated", 0)
    skipped = metadata.get("skipped", 0)

    return {
        "user_id": user_id,
        "operation_id": sync_op.operation_id,
        "status": sync_op.status,
        "progress_percent": progress_pct,
        "total_chunks": total_chunks,
        "processed_chunks": processed_chunks,
        "total_products": total_products,
        "processed": processed,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "created_at": sync_op.created_at.isoformat() if sync_op.created_at else None,
        "completed_at": sync_op.completed_at.isoformat() if sync_op.completed_at else None,
    }


@router.get("/status/{user_id}")
async def get_sync_status(
    user_id: str,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> SyncStatusResponse:
    """
    Get current sync status for a user.

    Args:
        user_id: User UUID

    Returns:
        Sync status information
    """
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id format"
        )

    stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
    result = await session.execute(stmt)
    sync_settings = result.scalar_one_or_none()

    if not sync_settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found in sync settings",
        )
    # Check if token was cleared (disconnected)
    disconnected = False
    try:
        from app.core.crypto import get_encryption_manager

        enc = get_encryption_manager()
        token = enc.decrypt(sync_settings.cardtrader_token_encrypted)
        if not (token and token.strip()):
            disconnected = True
    except Exception:
        disconnected = True

    return SyncStatusResponse(
        user_id=user_id,
        sync_status=(
            sync_settings.sync_status.value
            if hasattr(sync_settings.sync_status, "value")
            else str(sync_settings.sync_status)
        ),
        last_sync_at=sync_settings.last_sync_at.isoformat() if sync_settings.last_sync_at else None,
        last_error=(
            "Synchronization failed; retry or contact support"
            if sync_settings.last_error
            else None
        ),
        disconnected=disconnected if disconnected else None,
        execution_mode=sync_settings.execution_mode,
        mode_version=sync_settings.mode_version,
        writes_enabled=sync_settings.writes_enabled,
    )


@router.post("/disconnect/{user_id}", status_code=status.HTTP_200_OK)
async def disconnect_sync(
    user_id: str,
    body: DisconnectSyncRequest,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Suspend or remove CardTrader sync for the user.

    - suspend: set sync_status to idle (keeps token; user can start sync again).
    - remove: set sync_status to idle and clear token/webhook (user must re-enter token).
    """
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid).with_for_update()
    result = await session.execute(stmt)
    sync_settings = result.scalar_one_or_none()

    if not sync_settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found in sync settings",
        )
    await _assert_user_has_no_unresolved_mutations(session, user_uuid)

    from sqlalchemy import text

    if body.action == "suspend":
        conn = await session.connection()
        await conn.execute(
            text("""
                UPDATE user_sync_settings
                SET sync_status = CAST(:status AS sync_status_enum),
                    execution_mode = 'demo',
                    writes_enabled = FALSE,
                    mode_version = mode_version + 1,
                    mode_changed_at = NOW(),
                    updated_at = NOW()
                WHERE user_id = CAST(:user_id AS uuid)
            """),
            {"status": SyncStatusEnum.IDLE.value, "user_id": str(user_uuid)},
        )
        await session.commit()
        return {
            "status": "success",
            "message": "Sincronizzazione sospesa. Puoi riavviarla quando vuoi.",
            "action": "suspend",
            "sync_status": SyncStatusEnum.IDLE.value,
        }
    else:
        # remove: clear token and webhook
        from app.core.crypto import get_encryption_manager

        enc = get_encryption_manager()
        empty_token_encrypted = enc.encrypt("")
        conn = await session.connection()
        await conn.execute(
            text("""
                UPDATE user_sync_settings
                SET sync_status = CAST(:status AS sync_status_enum),
                    cardtrader_token_encrypted = :token,
                    webhook_secret = NULL,
                    execution_mode = 'demo',
                    writes_enabled = FALSE,
                    mode_version = mode_version + 1,
                    mode_changed_at = NOW(),
                    updated_at = NOW()
                WHERE user_id = CAST(:user_id AS uuid)
            """),
            {
                "status": SyncStatusEnum.IDLE.value,
                "token": empty_token_encrypted,
                "user_id": str(user_uuid),
            },
        )
        await session.commit()
        return {
            "status": "success",
            "message": "Collegamento CardTrader rimosso. Inserisci di nuovo il token per sincronizzare.",
            "action": "remove",
            "sync_status": SyncStatusEnum.IDLE.value,
        }


@router.post("/webhook/user/{user_id}", status_code=status.HTTP_200_OK)
async def receive_webhook(
    user_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Validate and durably enqueue a CardTrader webhook for one user."""
    start_time = time.time()

    try:
        try:
            user_uuid = uuid.UUID(user_id)
        except ValueError as e:
            logger.warning("Webhook rejected for malformed user id")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user_id format",
            ) from e

        await enforce_webhook_rate_limit(request, str(user_uuid))

        stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
        result = await session.execute(stmt)
        sync_settings = result.scalar_one_or_none()

        if not sync_settings:
            logger.warning("Webhook rejected for unknown sync owner")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Webhook authentication failed",
            )

        try:
            body = await _read_webhook_body(request)
        except WebhookBodyTooLarge as exc:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Webhook body too large",
            ) from exc

        signature_header = request.headers.get("Signature", "")
        stored_secret = sync_settings.webhook_secret
        if not stored_secret:
            logger.error("Webhook rejected because its secret is unavailable")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Webhook secret not configured",
            )
        from app.core.crypto import get_encryption_manager

        encryption_manager = get_encryption_manager()
        try:
            shared_secret = encryption_manager.decrypt_at_rest_secret(stored_secret)
            verify_webhook(body, signature_header, shared_secret)
        except (WebhookValidationError, ValueError) as e:
            logger.warning("Webhook rejected because its signature is invalid")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature",
            ) from e

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid webhook JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook payload must be an object",
            )

        # CardTrader documents this top-level value as the unique identifier
        # for a single endpoint call. It is the stable idempotency key across
        # retries; ``object_id``/``data.id`` instead identify the order.
        webhook_id_raw = payload.get("id")
        if not isinstance(webhook_id_raw, (str, int)) or not str(webhook_id_raw).strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing webhook id",
            )
        webhook_id = str(webhook_id_raw).strip()
        if len(webhook_id) > 128 or not all(
            character.isalnum() or character in ":_-" for character in webhook_id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid webhook id",
            )

        # Serialize inbox marking with snapshot apply. The route owns the
        # settings row before it publishes this event.
        sync_settings = (
            await session.execute(
                select(UserSyncSettings)
                .where(UserSyncSettings.user_id == user_uuid)
                .with_for_update()
            )
        ).scalar_one()
        locked_stored_secret = sync_settings.webhook_secret
        if not locked_stored_secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Webhook secret not configured",
            )
        try:
            locked_secret = encryption_manager.decrypt_at_rest_secret(
                locked_stored_secret
            )
            verify_webhook(body, signature_header, locked_secret)
        except (WebhookValidationError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Webhook signature no longer matches current secret",
            ) from exc

        insert_result = await session.execute(
            pg_insert(WebhookInbox)
            .values(
                webhook_id=webhook_id,
                user_id=user_uuid,
                cause=str(payload.get("cause") or ""),
                mode=str(payload.get("mode") or "live").lower(),
                payload_json=payload,
                signature_valid=True,
                status="received",
            )
            .on_conflict_do_nothing(index_elements=[WebhookInbox.webhook_id])
            .returning(WebhookInbox.id)
        )
        inserted_id = insert_result.scalar_one_or_none()
        if inserted_id is not None:
            inbox = await session.get(WebhookInbox, inserted_id)
        else:
            inbox = (
                await session.execute(
                    select(WebhookInbox)
                    .where(WebhookInbox.webhook_id == webhook_id)
                    .with_for_update()
                )
            ).scalar_one()
        if inbox is None:
            raise RuntimeError("Webhook inbox row disappeared")
        if inbox.user_id != user_uuid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Webhook ID associato a un altro utente",
            )
        if inserted_id is None and dict(inbox.payload_json or {}) != payload:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Webhook ID collision",
            )
        if not locked_stored_secret.startswith("fernet:"):
            sync_settings.webhook_secret = encryption_manager.encrypt_at_rest_secret(
                locked_secret
            )
        if inserted_id is None and inbox.status in ("completed", "ignored"):
            await session.commit()
            elapsed = (time.time() - start_time) * 1000
            return {
                "status": "duplicate",
                "webhook_id": webhook_id,
                "user_id": user_id,
                "processing_time_ms": round(elapsed, 2),
            }

        # Strong pre-ACK invariant: inbox, quarantine and marketplace
        # deactivation commit atomically.
        preack_result = await WebhookLedgerProcessor().prepare_inbox(
            session,
            inbox=inbox,
            payload=payload,
            settings=sync_settings,
        )
        await session.commit()

        if preack_result.get("status") == "reconcile_required":
            try:
                process_webhook_notification.delay(webhook_id)
            except Exception as exc:
                # Quarantine is already durable. Preserve reconcile_pending so
                # a delivery retry or the periodic reconciler can recover it.
                await session.execute(
                    update(WebhookInbox)
                    .where(WebhookInbox.webhook_id == webhook_id)
                    .values(last_error="webhook enqueue failed")
                )
                await session.commit()
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Webhook salvato ma non accodato; riprovare",
                ) from exc

        elapsed = (time.time() - start_time) * 1000  # milliseconds
        logger.info("Webhook id=%s acknowledged in %.2fms", webhook_id, elapsed)

        return {
            "status": "accepted",
            "webhook_id": webhook_id,
            "user_id": user_id,
            "processing_time_ms": round(elapsed, 2),
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Webhook processing failed (%s)", type(exc).__name__)
        # A non-2xx response is intentional: CardTrader must retry transient
        # parsing, database, Redis or queue failures instead of losing events.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook processing temporarily unavailable",
        ) from exc


@router.post("/webhook/{webhook_id}", status_code=status.HTTP_410_GONE)
async def receive_webhook_legacy(
    webhook_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Legacy webhook endpoint (for backward compatibility).

    This endpoint extracts user_id from the webhook payload.
    New implementations should use /webhook/user/{user_id} instead.

    Returns:
        Acknowledgment response
    """
    # Endpoint legacy non firmabile: non esiste modo di risolvere il webhook_secret
    # dell'utente dal payload, quindi la firma non è verificabile. Per sicurezza
    # (fail-closed) NON viene più processato. L'URL fornito agli utenti è sempre
    # /webhook/user/{user_id}. Se questo log compare, un utente va ricollegato lì.
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Legacy webhook endpoint disabled",
    )


@router.get("/webhook-url/{user_id}")
async def get_webhook_url(
    user_id: str,
    request: Request,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Get the webhook URL that the user should configure on CardTrader.

    Each user configures their own webhook endpoint on CardTrader:
    https://www.cardtrader.com/it/full_api_app

    Args:
        user_id: User UUID

    Returns:
        Webhook URL and configuration instructions
    """
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        ) from exc

    # Verify user exists
    stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
    result = await session.execute(stmt)
    sync_settings = result.scalar_one_or_none()

    if not sync_settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found in sync settings",
        )

    # Build webhook URL
    base_url = settings.PUBLIC_BASE_URL
    webhook_url = f"{base_url}/api/v1/sync/webhook/user/{user_id}"

    return {
        "user_id": user_id,
        "webhook_url": webhook_url,
        "instructions": {
            "step_1": "Go to https://www.cardtrader.com/it/full_api_app",
            "step_2": "Copy the webhook URL below",
            "step_3": "Paste it in the 'Indirizzo del tuo endpoint webhook' field",
            "step_4": "Click 'Salva l'endpoint del Webhook'",
            "note": "CardTrader will send notifications to this endpoint when orders/products are created, modified, or deleted",
        },
        "webhook_secret_configured": sync_settings.webhook_secret is not None,
    }


@router.post("/link-cardtrader")
@router.post("/setup-test-user")
async def setup_test_user(
    request: SetupTestUserRequest,
    http_request: Request,
    user_id_from_token: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Setup test user with CardTrader token (solo per test locale).

    Args:
        request: SetupTestUserRequest with user_id and cardtrader_token

    Returns:
        User sync settings
    """
    if http_request.url.path.endswith("/setup-test-user") and not settings.test_endpoints_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        owns_requested_user = uuid.UUID(user_id_from_token) == uuid.UUID(request.user_id)
    except ValueError:
        owns_requested_user = False
    if not owns_requested_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot configure CardTrader sync for another user",
        )

    try:
        user_uuid = uuid.UUID(request.user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        ) from exc
    try:
        token_user_uuid = uuid.UUID(user_id_from_token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authenticated user ID",
        ) from exc
    if token_user_uuid != user_uuid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: User ID mismatch",
        )

    try:
        from app.core.crypto import get_encryption_manager
        from app.services.cardtrader_client import CardTraderClient

        encryption_manager = get_encryption_manager()

        # Encrypt token
        try:
            token_encrypted = encryption_manager.encrypt(request.cardtrader_token)
        except Exception as e:
            logger.error("Credential encryption failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Credential encryption failed",
            )

        # Get shared_secret from CardTrader /info
        webhook_secret = None
        try:
            async with CardTraderClient(request.cardtrader_token, str(user_uuid)) as client:
                info = await client.get_info()
                webhook_secret = info.get("shared_secret")
                logger.info("Retrieved CardTrader webhook credential")
        except Exception as e:
            logger.error("Could not verify CardTrader credentials")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Token CardTrader non verificabile. Nessun collegamento salvato.",
            ) from e
        if not webhook_secret:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CardTrader non ha restituito il webhook secret. Nessun collegamento salvato.",
            )
        if not isinstance(webhook_secret, str) or len(webhook_secret) > 4096:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CardTrader ha restituito credenziali non valide.",
            )
        webhook_secret_encrypted = encryption_manager.encrypt_at_rest_secret(
            webhook_secret
        )

        # Create or update sync settings
        stmt = (
            select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid).with_for_update()
        )
        result = await session.execute(stmt)
        sync_settings = result.scalar_one_or_none()
        await _assert_user_has_no_unresolved_mutations(session, user_uuid)

        # Use raw SQL with explicit CAST for PostgreSQL enum
        # Bypass SQLAlchemy ORM validation by using direct connection
        from sqlalchemy import text

        if sync_settings:
            # Update existing - use direct connection to bypass ORM validation
            conn = await session.connection()
            await conn.execute(
                text("""
                    UPDATE user_sync_settings 
                    SET cardtrader_token_encrypted = :token,
                        webhook_secret = :webhook,
                        sync_status = CAST(:status AS sync_status_enum),
                        execution_mode = 'partial',
                        writes_enabled = FALSE,
                        mode_version = mode_version + 1,
                        mode_changed_at = NOW(),
                        updated_at = NOW()
                    WHERE user_id = CAST(:user_id AS uuid)
                """),
                {
                    "token": token_encrypted,
                    "webhook": webhook_secret_encrypted,
                    "status": SyncStatusEnum.IDLE.value,
                    "user_id": str(user_uuid),
                },
            )
            logger.info(f"Updated sync settings for user {user_uuid}")
        else:
            # Create new - use direct connection to bypass ORM validation
            conn = await session.connection()
            await conn.execute(
                text("""
                    INSERT INTO user_sync_settings 
                    (user_id, cardtrader_token_encrypted, webhook_secret, sync_status,
                     execution_mode, mode_version, writes_enabled, mode_changed_at,
                     created_at, updated_at)
                    VALUES 
                    (CAST(:user_id AS uuid), :token, :webhook,
                     CAST(:status AS sync_status_enum), 'partial', 1, FALSE, NOW(),
                     NOW(), NOW())
                """),
                {
                    "user_id": str(user_uuid),
                    "token": token_encrypted,
                    "webhook": webhook_secret_encrypted,
                    "status": SyncStatusEnum.IDLE.value,
                },
            )
            logger.info(f"Created sync settings for user {user_uuid}")

        marketplace_config_exists = (
            await conn.execute(text("SELECT to_regclass('public.mkt_sync_config')"))
        ).scalar_one_or_none()
        if marketplace_config_exists:
            await conn.execute(
                text("""
                    INSERT INTO mkt_sync_config
                        (id, user_id, sync_mode, mode_version, writes_enabled,
                         is_active,
                         created_at, updated_at)
                    SELECT CAST(:config_id AS uuid), user_id, 'partial',
                           mode_version, FALSE, TRUE, NOW(), NOW()
                    FROM user_sync_settings
                    WHERE user_id = CAST(:user_id AS uuid)
                    ON CONFLICT (user_id) DO UPDATE SET
                        sync_mode = 'partial',
                        mode_version = EXCLUDED.mode_version,
                        writes_enabled = FALSE,
                        updated_at = NOW()
                """),
                {"user_id": str(user_uuid), "config_id": str(uuid.uuid4())},
            )

        await session.commit()

        # Reload sync_settings to get the updated/created object
        stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
        result = await session.execute(stmt)
        sync_settings = result.scalar_one()

        # sync_status è già una stringa, non un enum
        status_value = (
            sync_settings.sync_status
            if isinstance(sync_settings.sync_status, str)
            else sync_settings.sync_status.value
        )

        return {
            "status": "success",
            "user_id": request.user_id,
            "sync_status": status_value,
            "webhook_secret_configured": webhook_secret is not None,
            "execution_mode": sync_settings.execution_mode,
            "mode_version": sync_settings.mode_version,
            "writes_enabled": sync_settings.writes_enabled,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("CardTrader link setup failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to configure CardTrader link",
        ) from exc


@router.delete("/inventory/{user_id}/item/{item_id}")
async def delete_inventory_item(
    user_id: str,
    item_id: int,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> DeleteInventoryItemResponse:
    """
    Delete an inventory item.

    Args:
        user_id: User UUID
        item_id: Inventory item ID

    Returns:
        Deletion result
    """
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError as e:
        raise BRXValidationError(
            detail="Invalid user_id format",
            field="user_id",
            value=user_id,
        ) from e

    stmt = (
        select(UserInventoryItem)
        .where(
            UserInventoryItem.id == item_id,
            UserInventoryItem.user_id == user_uuid,
        )
        .with_for_update()
    )
    result = await session.execute(stmt)
    item = result.scalar_one_or_none()

    if not item:
        raise InventoryItemNotFoundError(item_id=item_id, user_id=user_id)
    await _assert_no_unresolved_inventory_mutation(session, item)
    if item.reserved_quantity > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Oggetto bloccato in uno scambio attivo",
        )

    policy = None
    if item.source == "cardtrader" and item.external_stock_id:
        try:
            policy = await assert_cardtrader_write_allowed(session, user_uuid)
        except CardTraderWriteBlockedError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="CardTrader writes are not available",
            ) from exc
        if item.environment != policy.execution_mode:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="L'oggetto appartiene a un altro ambiente di sincronizzazione.",
            )

    # Store external_stock_id before deletion for CardTrader sync
    external_stock_id = item.external_stock_id

    delete_sync_queued = False
    delete_sync_queue_error = None
    delete_sync_task_id = None
    cardtrader_delete = item.source == "cardtrader" and bool(external_stock_id)
    if cardtrader_delete:
        if policy is None:
            try:
                policy = await assert_cardtrader_write_allowed(session, user_uuid)
            except CardTraderWriteBlockedError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="CardTrader writes are not available",
                ) from exc
        command_id = uuid.uuid4()
        item.quantity = 0
        item.lifecycle_status = "pending_delete"
        item.sync_state = "pending"
        item.row_version += 1
        session.add(
            CardTraderOutbox(
                id=command_id,
                user_id=user_uuid,
                mode_version=policy.mode_version,
                operation_type="delete_product",
                target_product_id=str(external_stock_id),
                inventory_item_id=item.id,
                expected_row_version=item.row_version,
                payload_json={"id": int(external_stock_id)},
                status="pending",
            )
        )
        session.add(
            SyncOperation(
                user_id=user_uuid,
                operation_id=str(command_id),
                operation_type="sync_delete",
                status="pending",
                operation_metadata={"outbox_status": "pending"},
            )
        )
        await session.commit()
        delete_sync_task_id = str(command_id)
        delete_sync_queued = True
        try:
            process_cardtrader_outbox_command.apply_async(
                args=[str(command_id)],
                task_id=str(command_id),
            )
            logger.info(
                "Created durable CardTrader deletion command %s for item %s",
                command_id,
                item_id,
            )
        except Exception as sync_error:
            logger.error(
                "Outbox command %s persisted but dispatch failed (%s)",
                command_id,
                type(sync_error).__name__,
            )
            delete_sync_queue_error = "Command persisted; background dispatch delayed"
    else:
        await session.delete(item)
        await session.commit()

    return DeleteInventoryItemResponse(
        status="pending_sync" if cardtrader_delete else "deleted",
        item_id=item_id,
        cardtrader_sync_queued=delete_sync_queued,
        external_stock_id=external_stock_id,
        sync_queue_error=delete_sync_queue_error,
        sync_task_id=delete_sync_task_id,
    )


@router.post(
    "/purchase/{user_id}/item/{item_id}",
    status_code=status.HTTP_200_OK,
    response_model=PurchaseItemResponse,
)
async def purchase_item(
    user_id: str,
    item_id: int,
    request: PurchaseItemRequest,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> PurchaseItemResponse:
    """
    Purchase an item (simulate buyer purchase).

    This endpoint:
    1. Checks local DB availability (with row lock for concurrency)
    2. Verifies availability on CardTrader
    3. If available: decrements quantity on CardTrader and local DB
    4. If not available: updates local DB and returns error

    Args:
        user_id: User UUID (seller)
        item_id: Item ID to purchase
        request: Purchase request with quantity to purchase

    Returns:
        Purchase result with status and details
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Endpoint acquisto legacy disabilitato. "
            "Usare il flusso ordini marketplace con outbox verificata."
        ),
    )


@router.put("/inventory/{user_id}/item/{item_id}")
async def update_inventory_item(
    user_id: str,
    item_id: int,
    update_data: UpdateInventoryItemRequest,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> UpdateInventoryItemResponse:
    """
    Update an inventory item.

    Args:
        user_id: User UUID
        item_id: Inventory item ID
        update_data: Update request data (Pydantic model)

    Returns:
        Update result
    """
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError as e:
        raise BRXValidationError(
            detail="Invalid user_id format",
            field="user_id",
            value=user_id,
        ) from e

    # Extract values from Pydantic model
    quantity = update_data.quantity
    price_cents = update_data.price_cents
    description = update_data.description
    user_data_field = update_data.user_data_field
    graded = update_data.graded
    properties = update_data.properties

    stmt = (
        select(UserInventoryItem)
        .where(
            UserInventoryItem.id == item_id,
            UserInventoryItem.user_id == user_uuid,
        )
        .with_for_update()
    )
    result = await session.execute(stmt)
    item = result.scalar_one_or_none()

    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Inventory item not found"
        )
    await _assert_no_unresolved_inventory_mutation(session, item)
    if item.reserved_quantity > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Oggetto bloccato in uno scambio attivo",
        )

    policy = None
    if item.source == "cardtrader" and item.external_stock_id:
        try:
            policy = await assert_cardtrader_write_allowed(session, user_uuid)
        except CardTraderWriteBlockedError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="CardTrader writes are not available",
            ) from exc
        if item.environment != policy.execution_mode:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="L'oggetto appartiene a un altro ambiente di sincronizzazione.",
            )

    # Store old values for comparison
    old_quantity = item.quantity
    old_price_cents = item.price_cents
    old_description = item.description
    old_user_data_field = item.user_data_field
    old_graded = item.graded

    # Store old properties for comparison
    old_properties = item.properties.copy() if item.properties else {}

    logger.info("Validated inventory update for item %s", item_id)

    # Update local database
    if quantity is not None:
        item.quantity = quantity
    if price_cents is not None:
        item.price_cents = price_cents
    if description is not None:
        item.description = description
    if user_data_field is not None:
        item.user_data_field = user_data_field
    if graded is not None:
        item.graded = graded
    if properties is not None:
        # Merge properties (update existing, keep others)
        # IMPORTANT: We need to handle properties correctly:
        # - Boolean properties (signed, altered, mtg_foil) are ALWAYS included (even if False)
        # - String properties (condition, mtg_language) are ALWAYS included if provided (even if empty)

        # CRITICAL: Create a new dict to ensure SQLAlchemy detects the change
        # SQLAlchemy doesn't automatically detect changes to JSONB fields when you modify nested keys
        updated_properties = item.properties.copy() if item.properties else {}

        # Update properties - merge strategy:
        # For booleans: always update (even if False, to explicitly set it)
        # For strings: always update if provided (including empty strings for condition)
        # Special handling for condition: always update if provided, even if empty
        for key, value in properties.items():
            if isinstance(value, bool):
                # Always update boolean properties
                updated_properties[key] = value
            elif isinstance(value, str):
                # For strings, always update if provided (including empty strings for condition)
                # This allows clearing condition if needed
                updated_properties[key] = value
            elif value is not None and not isinstance(value, (bool, str)):
                # Update other non-None values
                updated_properties[key] = value
            # If value is None, don't update (keep existing value)

        # CRITICAL: Assign the new dict completely
        # In SQLAlchemy 2.0, assigning a new dict to a JSONB field should be detected automatically
        # If not, we can use object_session to mark it as modified
        item.properties = updated_properties

        # For SQLAlchemy 2.0, try to flag as modified if possible
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(item, "properties")
        except (ImportError, AttributeError):
            # If flag_modified is not available, SQLAlchemy 2.0 should detect the change
            # by the complete dict assignment above
            pass

        logger.info("Inventory properties updated for item %s", item_id)

    item.updated_at = datetime.utcnow()
    await session.flush()

    # Check if properties changed
    # IMPORTANT: We need to compare the actual properties dict, not just reference
    # Also check if condition specifically changed
    properties_changed = False
    if properties is not None:
        # Deep comparison of properties
        if old_properties != (item.properties or {}):
            properties_changed = True
        # Also check condition specifically
        old_condition = old_properties.get("condition")
        new_condition = properties.get("condition")
        if old_condition != new_condition:
            properties_changed = True
            logger.info("Inventory condition updated for item %s", item_id)

    # Log final state after update
    logger.info(
        "Inventory update applied for item %s (properties_changed=%s)",
        item_id,
        properties_changed,
    )

    # Queue async sync to CardTrader (if external_stock_id exists and values changed)
    quantity_changed = quantity is not None and quantity != old_quantity
    price_changed = price_cents is not None and price_cents != old_price_cents
    description_changed = description is not None and description != old_description
    user_data_field_changed = user_data_field is not None and user_data_field != old_user_data_field
    graded_changed = graded is not None and graded != old_graded
    # Check that external_stock_id exists and is not empty
    external_stock_id_str = str(item.external_stock_id).strip() if item.external_stock_id else ""
    has_external_id = bool(external_stock_id_str)
    sync_needed = (
        item.source == "cardtrader"
        and has_external_id
        and (
            quantity_changed
            or price_changed
            or properties_changed
            or description_changed
            or user_data_field_changed
            or graded_changed
        )
    )
    sync_queue_error = None
    sync_task_id = None

    # Log for debugging
    logger.info(
        f"Update item {item_id}: external_stock_id={item.external_stock_id}, "
        f"has_external_id={has_external_id}, quantity_changed={quantity_changed}, "
        f"price_changed={price_changed}, properties_changed={properties_changed}, "
        f"description_changed={description_changed}, user_data_field_changed={user_data_field_changed}, "
        f"graded_changed={graded_changed}, sync_needed={sync_needed}"
    )

    if sync_needed:
        if not has_external_id:
            raise InventoryItemMissingExternalIdError(item_id=item_id, user_id=user_id)

        if policy is None:
            policy = await assert_cardtrader_write_allowed(session, user_uuid)

        command_id = uuid.uuid4()
        item.row_version += 1
        item.sync_state = "pending"
        item.lifecycle_status = "sold_out" if item.quantity == 0 else "active"
        payload = build_product_update_payload(item)
        session.add(
            CardTraderOutbox(
                id=command_id,
                user_id=user_uuid,
                mode_version=policy.mode_version,
                operation_type="update_product",
                target_product_id=external_stock_id_str,
                inventory_item_id=item.id,
                expected_row_version=item.row_version,
                payload_json=payload,
                context_json={
                    "type": "inventory_edit",
                    "old_quantity": old_quantity,
                },
                status="pending",
            )
        )
        session.add(
            SyncOperation(
                user_id=user_uuid,
                operation_id=str(command_id),
                operation_type="sync_update",
                status="pending",
                operation_metadata={"outbox_status": "pending"},
            )
        )
        await session.commit()
        sync_task_id = str(command_id)
        try:
            process_cardtrader_outbox_command.apply_async(
                args=[str(command_id)],
                task_id=str(command_id),
            )
            logger.info(
                "Created durable CardTrader update command %s for item %s",
                command_id,
                item_id,
            )
        except Exception as sync_error:
            logger.error(
                "Outbox command %s persisted but dispatch failed (%s)",
                command_id,
                type(sync_error).__name__,
            )
            sync_queue_error = "Command persisted; background dispatch delayed"
    else:
        await session.commit()

    return UpdateInventoryItemResponse(
        status="updated",
        item_id=item_id,
        quantity=item.quantity,
        price_cents=item.price_cents,
        description=item.description,
        user_data_field=item.user_data_field,
        graded=item.graded,
        properties=item.properties,
        cardtrader_sync_queued=sync_needed,
        external_stock_id=item.external_stock_id,
        has_external_id=has_external_id,
        sync_queue_error=sync_queue_error,
        sync_task_id=sync_task_id,
    )


@router.get(
    "/listings/blueprint/{blueprint_id}",
    response_model=ListingsByBlueprintResponse,
    summary="Listings by blueprint (public)",
)
async def get_listings_by_blueprint(
    blueprint_id: int = Path(ge=1),
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> ListingsByBlueprintResponse:
    """
    Get all listings (items for sale) for a given blueprint (card/print).
    Public endpoint: no auth required. Trade-locked CardTrader rows stay visible,
    while cards received from a trade are not listed automatically.
    """
    from app.core.config import get_settings

    if not get_settings().CARDTRADER_WRITES_ENABLED:
        return ListingsByBlueprintResponse(blueprint_id=blueprint_id, listings=[])
    stmt = (
        select(UserInventoryItem)
        .where(
            UserInventoryItem.blueprint_id == blueprint_id,
            UserInventoryItem.source == "cardtrader",
            UserInventoryItem.game_id == 1,
            UserInventoryItem.environment == "real",
            UserInventoryItem.lifecycle_status == "active",
            UserInventoryItem.sync_state == "synced",
            UserInventoryItem.sync_uncertain_event_id.is_(None),
            UserInventoryItem.user_id.in_(
                select(UserSyncSettings.user_id).where(
                    UserSyncSettings.execution_mode == "real",
                    UserSyncSettings.writes_enabled.is_(True),
                    UserSyncSettings.sync_status == SyncStatusEnum.ACTIVE.value,
                )
            ),
            or_(
                UserInventoryItem.quantity > 0,
                UserInventoryItem.reserved_quantity > 0,
            ),
        )
        .order_by(UserInventoryItem.price_cents.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    items = result.scalars().all()
    listings: List[ListingItemResponse] = []
    for item in items:
        props = item.properties or {}
        condition = props.get("condition") if isinstance(props.get("condition"), str) else None
        mtg_lang = props.get("mtg_language") if isinstance(props.get("mtg_language"), str) else None
        seller_id_str = str(item.user_id)
        display_name = f"Venditore #{seller_id_str[:8]}"
        listings.append(
            ListingItemResponse(
                item_id=item.id,
                seller_id=seller_id_str,
                seller_display_name=display_name,
                country=None,
                quantity=item.quantity,
                reserved_quantity=item.reserved_quantity,
                price_cents=item.price_cents,
                source=item.source,
                condition=condition,
                mtg_language=mtg_lang,
            )
        )
    return ListingsByBlueprintResponse(blueprint_id=blueprint_id, listings=listings)


@router.get("/inventory/{user_id}", response_model=InventoryResponse)
async def get_inventory(
    user_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10_000),
    include_history: bool = False,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> InventoryResponse:
    """
    Get user inventory items.

    Args:
        user_id: User UUID
        limit: Maximum number of items to return (default: 100, max: 1000)
        offset: Offset for pagination

    Returns:
        List of inventory items
    """
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user_id format"
        )

    # Query inventory items
    settings_mode = (
        await session.execute(
            select(UserSyncSettings.execution_mode).where(UserSyncSettings.user_id == user_uuid)
        )
    ).scalar_one_or_none() or "demo"
    inventory_filters = [
        UserInventoryItem.user_id == user_uuid,
        or_(
            UserInventoryItem.source == "trade",
            and_(
                UserInventoryItem.source == "cardtrader",
                UserInventoryItem.environment == settings_mode,
            ),
            and_(
                UserInventoryItem.source == "internal_test",
                UserInventoryItem.environment == "demo",
                settings_mode == "demo",
            ),
        ),
    ]
    if not include_history:
        inventory_filters.append(
            UserInventoryItem.lifecycle_status.notin_(["archived", "pending_delete"])
        )

    stmt = (
        select(UserInventoryItem)
        .where(*inventory_filters)
        .order_by(UserInventoryItem.updated_at.desc(), UserInventoryItem.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    items = result.scalars().all()

    # Get total count
    count_stmt = select(func.count()).select_from(UserInventoryItem).where(*inventory_filters)
    total_result = await session.execute(count_stmt)
    total = total_result.scalar_one()

    return InventoryResponse(
        user_id=user_id,
        items=[
            InventoryItemResponse(
                id=item.id,
                blueprint_id=item.blueprint_id,
                quantity=item.quantity,
                reserved_quantity=item.reserved_quantity,
                price_cents=item.price_cents,
                properties=item.properties,
                external_stock_id=item.external_stock_id,
                source=item.source,
                environment=item.environment,
                lifecycle_status=item.lifecycle_status,
                sync_state=item.sync_state,
                mapping_status=item.mapping_status,
                row_version=item.row_version,
                description=item.description,
                user_data_field=item.user_data_field,
                graded=item.graded,
                updated_at=item.updated_at.isoformat(),
                created_at=item.created_at.isoformat() if item.created_at else None,
            )
            for item in items
        ],
        total=total,
    )


@router.post("/sync-from-cardtrader/{user_id}", status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync_from_cardtrader(
    user_id: str,
    blueprint_id: Optional[int] = None,
    verified_user_id: str = Depends(verify_user_id_match),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Manually trigger sync from CardTrader to local database.

    This syncs products that might have been modified directly on CardTrader
    (not via our API) to ensure bidirectional synchronization.

    Args:
        user_id: User UUID
        blueprint_id: Optional blueprint_id to sync specific product

    Returns:
        Task information
    """
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        ) from exc

    # Verify user exists
    stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid).with_for_update()
    result = await session.execute(stmt)
    sync_settings = result.scalar_one_or_none()

    if not sync_settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found in sync settings",
        )

    if str(sync_settings.sync_status) != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Sync non attivo per l'utente (stato: {sync_settings.sync_status})",
        )
    if sync_settings.execution_mode not in {"partial", "real"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La modalita demo non puo leggere l'inventario CardTrader.",
        )

    existing = (
        await session.execute(
            select(SyncOperation)
            .where(
                SyncOperation.user_id == user_uuid,
                SyncOperation.operation_type == "reconcile",
                SyncOperation.status.in_(["pending", "processing"]),
                SyncOperation.created_at >= datetime.now(timezone.utc) - timedelta(minutes=45),
            )
            .order_by(SyncOperation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "status": "accepted",
            "task_id": existing.operation_id,
            "user_id": user_id,
            "blueprint_id": blueprint_id,
            "message": "Riconciliazione gia in corso",
        }

    # Riconciliazione completa dell'inventario utente (reconciler v2).
    # blueprint_id è accettato per compatibilità ma il reconciler lavora
    # sempre sull'export completo (più sicuro: vede anche gli articoli spariti).
    task_id = str(uuid.uuid4())
    await _register_task_before_enqueue(session, user_uuid, task_id, "reconcile")
    try:
        task = reconcile_user.apply_async(
            kwargs={"user_id": user_id},
            task_id=task_id,
        )
    except Exception as exc:
        await _mark_enqueue_failed(session, task_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Impossibile accodare la riconciliazione",
        ) from exc

    logger.info(f"Queued reconcile from CardTrader for user {user_id}, " f"task_id={task.id}")

    return {
        "status": "accepted",
        "task_id": task.id,
        "user_id": user_id,
        "blueprint_id": blueprint_id,
        "message": "Sync from CardTrader queued",
    }


@router.get("/debug-logs", include_in_schema=False)
async def get_debug_logs(
    limit: int = Query(default=100, ge=1, le=1000),
    _user_id: str = Depends(get_current_user_id),
) -> dict:
    """
    Get debug logs for frontend display.

    Note: This endpoint reads from application logs, not from a separate debug file.
    For production, consider using a proper log aggregation service.

    Args:
        limit: Maximum number of log entries to return (default: 100, max: 1000)

    Returns:
        List of log entries (empty for now, as we use structured logging)
    """
    # TODO: Implement log reading from structured logging system
    # For now, return empty as we use standard Python logging
    return {
        "logs": [],
        "total": 0,
        "limit": limit,
        "message": "Debug logs endpoint - use application logs for detailed information",
    }
