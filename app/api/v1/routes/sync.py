"""
API endpoints for sync operations.
"""
import logging
import time
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    TaskStatusResponse,
    UpdateInventoryItemRequest,
    UpdateInventoryItemResponse,
)
from app.api.dependencies import get_current_user_id, verify_user_id_match
from app.core.database import get_db_session, get_sync_db_engine
from sqlalchemy import text
from app.core.exceptions import (
    InventoryItemMissingExternalIdError,
    InventoryItemNotFoundError,
    NotFoundError,
    SyncInProgressError,
    SyncNotFoundError,
    ValidationError as BRXValidationError,
)
from app.core.webhook_validator import WebhookValidationError, verify_webhook
from app.models.inventory import (
    SyncOperation,
    SyncStatusEnum,
    UserInventoryItem,
    UserSyncSettings,
)
from app.tasks.celery_app import celery_app
from app.tasks.sync_tasks import (
    initial_bulk_sync,
    process_webhook_notification,
    sync_delete_product_to_cardtrader,
    sync_update_product_to_cardtrader,
)
from app.tasks.periodic_sync import periodic_sync_from_cardtrader

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/migrate/composite-index", status_code=status.HTTP_200_OK)
async def apply_composite_index_migration(
    user_id_from_token: str = Depends(get_current_user_id),
) -> dict:
    """
    Apply composite index migration for optimized bulk sync.
    
    This endpoint creates the index: idx_inventory_user_blueprint_external
    on (user_id, blueprint_id, external_stock_id) columns.
    
    Requires authentication (admin users only in production).
    
    Returns:
        Migration result
    """
    try:
        engine = get_sync_db_engine()
        
        migration_sql = """
        CREATE INDEX IF NOT EXISTS idx_inventory_user_blueprint_external 
        ON user_inventory_items(user_id, blueprint_id, external_stock_id);
        """
        
        with engine.begin() as conn:
            conn.execute(text(migration_sql))
        
        engine.dispose()
        
        return {
            "status": "success",
            "message": "Composite index created successfully",
            "index_name": "idx_inventory_user_blueprint_external",
            "columns": ["user_id", "blueprint_id", "external_stock_id"],
        }
    except Exception as e:
        logger.error(f"Error applying composite index migration: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error applying migration: {str(e)}"
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format"
        )
    
    # Check if sync settings exist
    stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
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
    
    # Check if sync is already in progress (unless force=True)
    if not force:
        status_value = sync_settings.sync_status if isinstance(sync_settings.sync_status, str) else sync_settings.sync_status.value
        if status_value in (SyncStatusEnum.INITIAL_SYNC.value, SyncStatusEnum.ACTIVE.value):
            raise SyncInProgressError(
                user_id=user_id,
                current_status=status_value,
            )
    
    # Start Celery task
    task = initial_bulk_sync.delay(user_id)
    
    return SyncStartResponse(
        status="accepted",
        task_id=task.id,
        user_id=user_id,
        message="Bulk sync started" + (" (forced)" if force else ""),
    )


@router.get("/task/{task_id}")
async def get_task_status(
    task_id: str,
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
        # Verify task ownership by checking SyncOperation in database
        try:
            user_uuid = uuid.UUID(user_id_from_token)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user_id format in token"
            )
        
        # Query SyncOperation to find task owner
        # Note: operation_id in SyncOperation stores the Celery task_id
        stmt = select(SyncOperation).where(
            SyncOperation.operation_id == task_id
        )
        result = await session.execute(stmt)
        sync_op = result.scalar_one_or_none()
        
        if sync_op:
            # Verify ownership
            if sync_op.user_id != user_uuid:
                logger.warning(
                    f"Task ownership mismatch: task={task_id}, "
                    f"token_user={user_id_from_token}, task_user={sync_op.user_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: Task does not belong to authenticated user",
                )
        else:
            # Task not found in SyncOperation - might be a different task type
            # For security, deny access if we can't verify ownership
            logger.warning(
                f"Task {task_id} not found in SyncOperation, denying access for security"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: Could not verify task ownership",
            )
        
        # Get task status from Celery
        task = celery_app.AsyncResult(task_id)
        
        response = {
            "task_id": task_id,
            "status": task.state,
            "ready": task.ready(),
        }
        
        if task.ready():
            if task.successful():
                response["result"] = task.result
                response["message"] = "Task completed successfully"
            else:
                response["error"] = str(task.info) if task.info else "Unknown error"
                response["message"] = "Task failed"
        else:
            # Task is still running
            if task.state == "PENDING":
                response["message"] = "Task is waiting to be processed"
            elif task.state == "STARTED":
                response["message"] = "Task is currently running"
            elif task.state == "RETRY":
                response["message"] = "Task is being retried"
            else:
                response["message"] = f"Task state: {task.state}"
        
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting task status for {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving task status: {str(e)}",
        )


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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format"
        )
    
    # Get the most recent sync operation for this user
    stmt = (
        select(SyncOperation)
        .where(SyncOperation.user_id == user_uuid)
        .where(SyncOperation.operation_type == "initial_bulk_sync")
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format"
        )
    
    stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
    result = await session.execute(stmt)
    sync_settings = result.scalar_one_or_none()
    
    if not sync_settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found in sync settings"
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
        sync_status=sync_settings.sync_status.value if hasattr(sync_settings.sync_status, 'value') else str(sync_settings.sync_status),
        last_sync_at=sync_settings.last_sync_at.isoformat() if sync_settings.last_sync_at else None,
        last_error=sync_settings.last_error,
        disconnected=disconnected if disconnected else None,
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

    stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
    result = await session.execute(stmt)
    sync_settings = result.scalar_one_or_none()

    if not sync_settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found in sync settings",
        )

    from sqlalchemy import text

    if body.action == "suspend":
        conn = await session.connection()
        await conn.execute(
            text("""
                UPDATE user_sync_settings
                SET sync_status = CAST(:status AS sync_status_enum),
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
    """
    Receive webhook notification from CardTrader for a specific user.
    
    Each user configures their own webhook endpoint on CardTrader:
    https://your-domain.com/api/v1/sync/webhook/user/{user_id}
    
    Must respond in < 100ms. Processing happens asynchronously.
    
    Args:
        user_id: User UUID (extracted from URL path)
        
    Returns:
        Acknowledgment response
    """
    start_time = time.time()
    
    try:
        # Validate user_id format
        try:
            user_uuid = uuid.UUID(user_id)
        except ValueError as e:
            logger.error(f"Invalid user_id format in webhook: {user_id}")
            return {
                "status": "error",
                "user_id": user_id,
                "message": f"Invalid user_id format: {str(e)}",
            }
        
        # Verify user exists and get webhook_secret
        stmt = select(UserSyncSettings).where(
            UserSyncSettings.user_id == user_uuid
        )
        result = await session.execute(stmt)
        sync_settings = result.scalar_one_or_none()
        
        if not sync_settings:
            logger.warning(f"Webhook received for unknown user: {user_id}")
            return {
                "status": "error",
                "user_id": user_id,
                "message": "User not found in sync settings",
            }
        
        # Get raw body for signature validation
        body = await request.body()
        
        # Get signature header
        signature_header = request.headers.get("Signature", "")
        
        # Get webhook payload
        payload = await request.json()
        
        # Extract webhook_id from payload
        webhook_id = payload.get("id", "unknown")
        
        # Validate signature using user's shared_secret
        shared_secret = sync_settings.webhook_secret
        if shared_secret and signature_header:
            try:
                verify_webhook(body, signature_header, shared_secret)
                logger.debug(f"Webhook signature validated for user {user_id}")
            except WebhookValidationError as e:
                logger.warning(
                    f"Webhook signature validation failed for user {user_id}: {e}"
                )
                # In production, you might want to reject invalid signatures
                # For now, we'll still process but log the warning
        elif not shared_secret:
            logger.warning(
                f"No webhook_secret configured for user {user_id}. "
                f"Webhook will be processed without signature validation."
            )
        
        # Queue async processing with user_id
        process_webhook_notification.delay(webhook_id, payload, str(user_uuid))
        
        elapsed = (time.time() - start_time) * 1000  # milliseconds
        logger.info(
            f"Webhook {webhook_id} for user {user_id} acknowledged in {elapsed:.2f}ms"
        )
        
        return {
            "status": "accepted",
            "webhook_id": webhook_id,
            "user_id": user_id,
            "processing_time_ms": round(elapsed, 2),
        }
        
    except Exception as e:
        logger.error(
            f"Error processing webhook for user {user_id}: {e}",
            exc_info=True
        )
        # Still return 200 to avoid CardTrader retries
        return {
            "status": "error",
            "user_id": user_id,
            "message": str(e),
        }


@router.post("/webhook/{webhook_id}", status_code=status.HTTP_200_OK)
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
    start_time = time.time()
    
    try:
        # Get raw body for signature validation
        body = await request.body()
        
        # Get signature header
        signature_header = request.headers.get("Signature", "")
        
        # Get webhook payload
        payload = await request.json()
        
        # Extract user_id from payload (fallback method)
        data = payload.get("data", {})
        user_id_str = None
        
        # Try to extract seller ID from order
        if isinstance(data, dict):
            seller = data.get("seller", {})
            if isinstance(seller, dict):
                user_id_str = str(seller.get("id", "")) if seller.get("id") else None
        
        if not user_id_str:
            logger.warning(
                f"Could not extract user_id from webhook {webhook_id}. "
                f"Payload structure: {list(data.keys()) if isinstance(data, dict) else 'not a dict'}"
            )
            # Still process, but without user-specific validation
            process_webhook_notification.delay(webhook_id, payload, None)
        else:
            # Process with extracted user_id
            process_webhook_notification.delay(webhook_id, payload, user_id_str)
        
        elapsed = (time.time() - start_time) * 1000  # milliseconds
        logger.info(f"Webhook {webhook_id} acknowledged in {elapsed:.2f}ms")
        
        return {
            "status": "accepted",
            "webhook_id": webhook_id,
            "processing_time_ms": round(elapsed, 2),
        }
        
    except Exception as e:
        logger.error(f"Error processing webhook {webhook_id}: {e}", exc_info=True)
        return {
            "status": "error",
            "webhook_id": webhook_id,
            "message": str(e),
        }


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
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid user_id format: {str(e)}"
        )
    
    # Verify user exists
    stmt = select(UserSyncSettings).where(
        UserSyncSettings.user_id == user_uuid
    )
    result = await session.execute(stmt)
    sync_settings = result.scalar_one_or_none()
    
    if not sync_settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found in sync settings"
        )
    
    # Build webhook URL
    base_url = str(request.base_url).rstrip('/')
    webhook_url = f"{base_url}/api/v1/sync/webhook/user/{user_id}"
    
    return {
        "user_id": user_id,
        "webhook_url": webhook_url,
        "instructions": {
            "step_1": "Go to https://www.cardtrader.com/it/full_api_app",
            "step_2": "Copy the webhook URL below",
            "step_3": "Paste it in the 'Indirizzo del tuo endpoint webhook' field",
            "step_4": "Click 'Salva l'endpoint del Webhook'",
            "note": "CardTrader will send notifications to this endpoint when orders/products are created, modified, or deleted"
        },
        "webhook_secret_configured": sync_settings.webhook_secret is not None
    }


@router.post("/setup-test-user")
async def setup_test_user(
    request: SetupTestUserRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """
    Setup test user with CardTrader token (solo per test locale).
    
    Args:
        request: SetupTestUserRequest with user_id and cardtrader_token
        
    Returns:
        User sync settings
    """
    try:
        user_uuid = uuid.UUID(request.user_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid user_id format: {str(e)}"
        )
    
    try:
        from app.core.crypto import get_encryption_manager
        from app.services.cardtrader_client import CardTraderClient
        
        encryption_manager = get_encryption_manager()
        
        # Encrypt token
        try:
            token_encrypted = encryption_manager.encrypt(request.cardtrader_token)
        except Exception as e:
            logger.error(f"Encryption error: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error encrypting token: {str(e)}"
            )
        
        # Get shared_secret from CardTrader /info
        webhook_secret = None
        try:
            async with CardTraderClient(request.cardtrader_token, str(user_uuid)) as client:
                info = await client.get_info()
                webhook_secret = info.get("shared_secret")
                logger.info(f"Retrieved shared_secret for user {user_uuid}")
        except Exception as e:
            logger.warning(f"Could not fetch shared_secret from CardTrader: {e}")
            # Non blocchiamo il setup se fallisce, ma loggiamo
        
        # Create or update sync settings
        stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
        result = await session.execute(stmt)
        sync_settings = result.scalar_one_or_none()
        
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
                        updated_at = NOW()
                    WHERE user_id = CAST(:user_id AS uuid)
                """),
                {
                    "token": token_encrypted,
                    "webhook": webhook_secret,
                    "status": SyncStatusEnum.IDLE.value,
                    "user_id": str(user_uuid)
                }
            )
            logger.info(f"Updated sync settings for user {user_uuid}")
        else:
            # Create new - use direct connection to bypass ORM validation
            conn = await session.connection()
            await conn.execute(
                text("""
                    INSERT INTO user_sync_settings 
                    (user_id, cardtrader_token_encrypted, webhook_secret, sync_status, created_at, updated_at)
                    VALUES 
                    (CAST(:user_id AS uuid), :token, :webhook, CAST(:status AS sync_status_enum), NOW(), NOW())
                """),
                {
                    "user_id": str(user_uuid),
                    "token": token_encrypted,
                    "webhook": webhook_secret,
                    "status": SyncStatusEnum.IDLE.value
                }
            )
            logger.info(f"Created sync settings for user {user_uuid}")
        
        await session.commit()
        
        # Reload sync_settings to get the updated/created object
        stmt = select(UserSyncSettings).where(UserSyncSettings.user_id == user_uuid)
        result = await session.execute(stmt)
        sync_settings = result.scalar_one()
        
        # sync_status è già una stringa, non un enum
        status_value = sync_settings.sync_status if isinstance(sync_settings.sync_status, str) else sync_settings.sync_status.value
        
        return {
            "status": "success",
            "user_id": request.user_id,
            "sync_status": status_value,
            "webhook_secret_configured": webhook_secret is not None,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting up user {request.user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error setting up user: {str(e)}"
        )


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
    
    stmt = select(UserInventoryItem).where(
        UserInventoryItem.id == item_id,
        UserInventoryItem.user_id == user_uuid,
    )
    result = await session.execute(stmt)
    item = result.scalar_one_or_none()
    
    if not item:
        raise InventoryItemNotFoundError(item_id=item_id, user_id=user_id)
    
    # Store external_stock_id before deletion for CardTrader sync
    external_stock_id = item.external_stock_id
    
    # Delete from local database
    await session.delete(item)
    await session.commit()
    
    # Queue async sync to CardTrader (if external_stock_id exists)
    delete_sync_queued = False
    delete_sync_queue_error = None
    delete_sync_task_id = None
    if external_stock_id:
        try:
            task_result = sync_delete_product_to_cardtrader.delay(user_id, int(external_stock_id))
            delete_sync_task_id = task_result.id
            delete_sync_queued = True
            # Register task so get_task_status can verify ownership when frontend polls
            sync_op = SyncOperation(
                user_id=user_uuid,
                operation_id=task_result.id,
                operation_type="sync_delete",
                status="pending",
            )
            session.add(sync_op)
            await session.commit()
            logger.info(
                f"Queued CardTrader deletion sync for item {item_id}, "
                f"external_stock_id {external_stock_id}, task_id={task_result.id}"
            )
        except Exception as sync_error:
            logger.error(
                f"Failed to queue CardTrader deletion sync: {sync_error}",
                exc_info=True
            )
            delete_sync_queue_error = str(sync_error)
    
    return DeleteInventoryItemResponse(
        status="deleted",
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
    purchase_quantity = request.quantity
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format"
        )
    
    # SAGA PATTERN: Split transaction to avoid long locks with external API calls
    # Step 1: Lock DB and verify availability (SHORT transaction)
    async with session.begin():
        # Lock row to prevent concurrent purchases
        stmt = (
            select(UserInventoryItem)
            .where(
                UserInventoryItem.id == item_id,
                UserInventoryItem.user_id == user_uuid
            )
            .with_for_update()  # Row-level lock
        )
        result = await session.execute(stmt)
        item = result.scalar_one_or_none()
        
        if not item:
            raise InventoryItemNotFoundError(item_id=item_id, user_id=user_id)
        
        quantity_before = item.quantity
        external_stock_id_str = str(item.external_stock_id) if item.external_stock_id else None
        
        # Check local DB availability
        if item.quantity < purchase_quantity:
            logger.info(
                f"Purchase failed: Item {item_id} has quantity {item.quantity} in local DB, "
                f"but {purchase_quantity} requested"
            )
            # Transaction will commit and release lock
            available_quantity = item.quantity
            # Will return error after transaction closes
    
    # Transaction closed - lock released
    
    # If insufficient quantity in local DB, sync from CardTrader and return error
    if quantity_before < purchase_quantity:
        if external_stock_id_str:
            try:
                from app.core.crypto import get_encryption_manager
                from app.services.cardtrader_client import CardTraderClient
                
                # Get user sync settings (new query, no lock)
                settings_stmt = select(UserSyncSettings).where(
                    UserSyncSettings.user_id == user_uuid
                )
                settings_result = await session.execute(settings_stmt)
                sync_settings = settings_result.scalar_one_or_none()
                
                if sync_settings:
                    encryption_manager = get_encryption_manager()
                    token = encryption_manager.decrypt(sync_settings.cardtrader_token_encrypted)
                    if token and token.strip():
                        # Check availability on CardTrader (outside transaction)
                        async with CardTraderClient(token, user_id) as client:
                            availability = await client.check_product_availability(external_stock_id_str)
                            cardtrader_quantity = availability.get("quantity", 0)
                            
                            # Update local DB with CardTrader quantity (new transaction)
                            async with session.begin():
                                stmt = (
                                    select(UserInventoryItem)
                                    .where(
                                        UserInventoryItem.id == item_id,
                                        UserInventoryItem.user_id == user_uuid
                                    )
                                )
                                result = await session.execute(stmt)
                                item = result.scalar_one_or_none()
                                if item:
                                    item.quantity = cardtrader_quantity
                                    await session.commit()
                            
                            available_quantity = cardtrader_quantity
            except Exception as e:
                logger.error(f"Error syncing from CardTrader during purchase: {e}")
                available_quantity = quantity_before
        else:
            available_quantity = quantity_before
        
        return PurchaseItemResponse(
            status="error",
            item_id=item_id,
            message=f"Quantità insufficiente. Disponibile: {available_quantity}, Richiesta: {purchase_quantity}",
            available=False,
            quantity_purchased=0,
            quantity_before=quantity_before,
            quantity_after=available_quantity,
            cardtrader_sync_queued=False,
            external_stock_id=external_stock_id_str,
            error=f"Insufficient quantity. Available: {available_quantity}, Requested: {purchase_quantity}",
        )
    
    # Step 2: Verify external_stock_id exists
    if not external_stock_id_str:
        return PurchaseItemResponse(
            status="error",
            item_id=item_id,
            message="Item non ha external_stock_id, impossibile verificare disponibilità su CardTrader",
            available=False,
            quantity_purchased=0,
            quantity_before=quantity_before,
            quantity_after=quantity_before,
            cardtrader_sync_queued=False,
            external_stock_id=None,
            error="Missing external_stock_id",
        )
    
    # Step 3: Get sync settings and token (outside transaction)
    try:
        from app.core.crypto import get_encryption_manager
        from app.services.cardtrader_client import CardTraderClient
        
        # Get user sync settings
        settings_stmt = select(UserSyncSettings).where(
            UserSyncSettings.user_id == user_uuid
        )
        settings_result = await session.execute(settings_stmt)
        sync_settings = settings_result.scalar_one_or_none()
        
        if not sync_settings:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {user_id} not found in sync settings"
            )
        
        encryption_manager = get_encryption_manager()
        token = encryption_manager.decrypt(sync_settings.cardtrader_token_encrypted)
        if not (token and token.strip()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Collegamento CardTrader non configurato. Configura il token nella pagina Sincronizzazione.",
            )

        # Step 4: Check availability and update CardTrader (OUTSIDE transaction)
        cardtrader_updated = False
        cardtrader_quantity_after = None

        async with CardTraderClient(token, user_id) as client:
            availability = await client.check_product_availability(external_stock_id_str)
            cardtrader_quantity = availability.get("quantity", 0)
            
            if cardtrader_quantity < purchase_quantity:
                # Product not available in sufficient quantity on CardTrader
                logger.info(
                    f"Purchase failed: Item {item_id} (product {external_stock_id_str}) "
                    f"has insufficient quantity on CardTrader. Available: {cardtrader_quantity}, "
                    f"Requested: {purchase_quantity}"
                )
                
                # Update local DB with CardTrader quantity (new transaction)
                async with session.begin():
                    stmt = (
                        select(UserInventoryItem)
                        .where(
                            UserInventoryItem.id == item_id,
                            UserInventoryItem.user_id == user_uuid
                        )
                    )
                    result = await session.execute(stmt)
                    item = result.scalar_one_or_none()
                    if item:
                        item.quantity = cardtrader_quantity
                        await session.commit()
                
                return PurchaseItemResponse(
                    status="error",
                    item_id=item_id,
                    message=f"Quantità insufficiente su CardTrader. Disponibile: {cardtrader_quantity}, Richiesta: {purchase_quantity}",
                    available=False,
                    quantity_purchased=0,
                    quantity_before=quantity_before,
                    quantity_after=cardtrader_quantity,
                    cardtrader_sync_queued=False,
                    external_stock_id=external_stock_id_str,
                    error=f"Insufficient quantity on CardTrader. Available: {cardtrader_quantity}, Requested: {purchase_quantity}",
                )
            
            # Step 5: Update CardTrader (decrement or delete)
            new_cardtrader_quantity = cardtrader_quantity - purchase_quantity
            
            if new_cardtrader_quantity > 0:
                # Decrement quantity
                logger.info(
                    f"Decrementing quantity for product {external_stock_id_str} "
                    f"from {cardtrader_quantity} to {new_cardtrader_quantity} "
                    f"(purchasing {purchase_quantity})"
                )
                await client.increment_product_quantity(int(external_stock_id_str), -purchase_quantity)
                cardtrader_updated = True
                cardtrader_quantity_after = new_cardtrader_quantity
            else:
                # Delete product if quantity reaches 0
                logger.info(
                    f"Deleting product {external_stock_id_str} from CardTrader "
                    f"(purchasing {purchase_quantity}, remaining would be {new_cardtrader_quantity})"
                )
                await client.delete_product(int(external_stock_id_str))
                cardtrader_updated = True
                cardtrader_quantity_after = 0
        
        # Step 6: Update local DB (new transaction) - COMPENSATION if this fails
        try:
            async with session.begin():
                stmt = (
                    select(UserInventoryItem)
                    .where(
                        UserInventoryItem.id == item_id,
                        UserInventoryItem.user_id == user_uuid
                    )
                )
                result = await session.execute(stmt)
                item = result.scalar_one_or_none()
                
                if not item:
                    raise InventoryItemNotFoundError(item_id=item_id, user_id=user_id)
                
                # Update quantity
                item.quantity = quantity_before - purchase_quantity
                await session.commit()
            
            logger.info(
                f"Purchase successful: Item {item_id} (product {external_stock_id_str}) "
                f"purchased {purchase_quantity} units. Quantity: {quantity_before} -> {item.quantity}"
            )
            
            return PurchaseItemResponse(
                status="success",
                item_id=item_id,
                message=f"Acquisto completato con successo: {purchase_quantity} unità",
                available=True,
                quantity_purchased=purchase_quantity,
                quantity_before=quantity_before,
                quantity_after=quantity_before - purchase_quantity,
                cardtrader_sync_queued=False,
                external_stock_id=external_stock_id_str,
                error=None,
            )
            
        except Exception as db_error:
            # COMPENSATION: DB update failed, but CardTrader was already updated
            # Try to compensate by restoring CardTrader quantity
            logger.error(
                f"DB update failed after CardTrader update for item {item_id}. "
                f"Attempting compensation... Error: {db_error}",
                exc_info=True
            )
            
            try:
                async with CardTraderClient(token, user_id) as client:
                    if cardtrader_quantity_after == 0:
                        # Product was deleted, try to restore by creating with original quantity
                        # Note: This is best-effort, CardTrader may not support direct creation
                        logger.warning(
                            f"Cannot fully compensate: product {external_stock_id_str} was deleted. "
                            f"Manual intervention may be required."
                        )
                    else:
                        # Restore quantity by incrementing back
                        logger.info(
                            f"Compensating: restoring {purchase_quantity} units to product {external_stock_id_str}"
                        )
                        await client.increment_product_quantity(int(external_stock_id_str), purchase_quantity)
            except Exception as compensation_error:
                logger.error(
                    f"Compensation failed for item {item_id}: {compensation_error}. "
                    f"Manual intervention required!",
                    exc_info=True
                )
            
            # Return error - CardTrader may be inconsistent
            return PurchaseItemResponse(
                status="error",
                item_id=item_id,
                message=f"Errore durante l'aggiornamento del database locale. CardTrader potrebbe essere stato aggiornato. Errore: {str(db_error)}",
                available=False,
                quantity_purchased=0,
                quantity_before=quantity_before,
                quantity_after=quantity_before,
                cardtrader_sync_queued=False,
                external_stock_id=external_stock_id_str,
                error=f"Database update failed: {str(db_error)}",
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during purchase for item {item_id}: {e}", exc_info=True)
        
        return PurchaseItemResponse(
            status="error",
            item_id=item_id,
            message=f"Errore durante l'acquisto: {str(e)}",
            available=False,
            quantity_purchased=0,
            quantity_before=quantity_before if 'quantity_before' in locals() else 0,
            quantity_after=quantity_before if 'quantity_before' in locals() else 0,
            cardtrader_sync_queued=False,
            external_stock_id=external_stock_id_str if 'external_stock_id_str' in locals() else None,
            error=str(e),
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
    
    # CRITICAL DEBUG: Log what we received
    print(f"\n{'='*80}")
    print(f"UPDATE REQUEST RECEIVED for item {item_id}")
    print(f"Properties received: {properties}")
    print(f"Condition in properties: {'condition' in properties if properties else False}")
    print(f"Condition value: {properties.get('condition') if properties and 'condition' in properties else 'NOT PROVIDED'}")
    print(f"{'='*80}\n")
    
    logger.warning(  # Use WARNING level to ensure it's visible
        f"🔵 UPDATE REQUEST - item_id={item_id}, "
        f"properties={properties}, "
        f"has_condition={'condition' in properties if properties else False}, "
        f"condition_value={properties.get('condition') if properties and 'condition' in properties else None}"
    )
    
    stmt = select(UserInventoryItem).where(
        UserInventoryItem.id == item_id,
        UserInventoryItem.user_id == user_uuid,
    )
    result = await session.execute(stmt)
    item = result.scalar_one_or_none()
    
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inventory item not found"
        )
    
    # Store old values for comparison
    old_quantity = item.quantity
    old_price_cents = item.price_cents
    old_description = item.description
    old_user_data_field = item.user_data_field
    old_graded = item.graded
    
    # Store old properties for comparison
    old_properties = item.properties.copy() if item.properties else {}
    
    # Log initial state
    logger.info(
        f"Update request for item {item_id}: "
        f"old_properties={old_properties}, "
        f"received_properties={properties}, "
        f"old_condition={old_properties.get('condition')}, "
        f"received_condition={properties.get('condition') if properties else None}"
    )
    
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
                logger.debug(f"Updated boolean property '{key}' = {value} for item {item_id}")
            elif isinstance(value, str):
                # For strings, always update if provided (including empty strings for condition)
                # This allows clearing condition if needed
                updated_properties[key] = value
                logger.debug(f"Updated string property '{key}' = '{value}' for item {item_id}")
                if key == "condition":
                    print(f"\n{'='*80}")
                    print(f"✅ CONDITION BEING UPDATED for item {item_id}: '{value}'")
                    print(f"{'='*80}\n")
                    logger.warning(f"✅ CONDITION UPDATED for item {item_id}: '{value}'")
            elif value is not None and not isinstance(value, (bool, str)):
                # Update other non-None values
                updated_properties[key] = value
                logger.debug(f"Updated property '{key}' = {value} for item {item_id}")
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
        
        # Log the update for debugging
        logger.info(
            f"Updated properties for item {item_id}: "
            f"old={old_properties}, received={properties}, final={item.properties}, "
            f"condition_in_received={'condition' in properties}, "
            f"condition_value={properties.get('condition') if 'condition' in properties else None}"
        )
    
    item.updated_at = datetime.utcnow()
    
    # CRITICAL: Verify condition is saved before commit
    if properties and "condition" in properties:
        print(f"\n{'='*80}")
        print(f"🔍 BEFORE COMMIT - Item {item_id} properties: {item.properties}")
        print(f"🔍 Condition value in item.properties: {item.properties.get('condition') if item.properties else 'NO PROPERTIES'}")
        print(f"{'='*80}\n")
        logger.warning(
            f"🔍 BEFORE COMMIT - item_id={item_id}, "
            f"item.properties={item.properties}, "
            f"condition={item.properties.get('condition') if item.properties else None}"
        )
    
    await session.commit()
    
    # CRITICAL: Verify condition is saved after commit
    if properties and "condition" in properties:
        # Refresh item to verify it was saved
        await session.refresh(item)
        print(f"\n{'='*80}")
        print(f"🔍 AFTER COMMIT - Item {item_id} properties: {item.properties}")
        print(f"🔍 Condition value after refresh: {item.properties.get('condition') if item.properties else 'NO PROPERTIES'}")
        print(f"{'='*80}\n")
        logger.warning(
            f"🔍 AFTER COMMIT - item_id={item_id}, "
            f"item.properties={item.properties}, "
            f"condition={item.properties.get('condition') if item.properties else None}"
        )
    
    # Check if properties changed
    # IMPORTANT: We need to compare the actual properties dict, not just reference
    # Also check if condition specifically changed
    properties_changed = False
    if properties is not None:
        # Deep comparison of properties
        if old_properties != properties:
            properties_changed = True
        # Also check condition specifically
        old_condition = old_properties.get('condition')
        new_condition = properties.get('condition')
        if old_condition != new_condition:
            properties_changed = True
            logger.info(
                f"Condition changed for item {item_id}: "
                f"'{old_condition}' -> '{new_condition}'"
            )
    
    # Log final state after update
    logger.info(
        f"After update for item {item_id}: "
        f"final_properties={item.properties}, "
        f"final_condition={item.properties.get('condition') if item.properties else None}, "
        f"properties_changed={properties_changed}"
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
    sync_needed = has_external_id and (
        quantity_changed or price_changed or properties_changed or 
        description_changed or user_data_field_changed or graded_changed
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
        
        try:
            # Pass None for individual fields to ensure the task reads the latest from DB
            task_result = sync_update_product_to_cardtrader.delay(
                user_id,
                item_id,
                price_cents=None,
                quantity=None,
                description=None,
                user_data_field=None,
                graded=None,
                properties=None,
            )
            sync_task_id = task_result.id
            # Register task so get_task_status can verify ownership when frontend polls
            sync_op = SyncOperation(
                user_id=user_uuid,
                operation_id=task_result.id,
                operation_type="sync_update",
                status="pending",
            )
            session.add(sync_op)
            await session.commit()
            logger.info(
                f"Queued CardTrader update sync for item {item_id}, "
                f"external_stock_id {item.external_stock_id}, "
                f"task_id={task_result.id}, "
                f"quantity_changed={quantity_changed}, price_changed={price_changed}, "
                f"properties_changed={properties_changed}, description_changed={description_changed}, "
                f"user_data_field_changed={user_data_field_changed}, graded_changed={graded_changed}"
            )
        except Exception as sync_error:
            logger.error(
                f"Failed to queue CardTrader update sync: {sync_error}",
                exc_info=True
            )
            sync_needed = False
            sync_queue_error = str(sync_error)
    
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
    blueprint_id: int,
    limit: int = 100,
    session: AsyncSession = Depends(get_db_session),
) -> ListingsByBlueprintResponse:
    """
    Get all listings (items for sale) for a given blueprint (card/print).
    Public endpoint: no auth required. Returns sellers who have this print in inventory with quantity > 0.
    """
    limit = min(max(1, limit), 200)
    stmt = (
        select(UserInventoryItem)
        .where(
            UserInventoryItem.blueprint_id == blueprint_id,
            UserInventoryItem.quantity > 0,
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
                price_cents=item.price_cents,
                condition=condition,
                mtg_language=mtg_lang,
            )
        )
    return ListingsByBlueprintResponse(blueprint_id=blueprint_id, listings=listings)


@router.get("/inventory/{user_id}", response_model=InventoryResponse)
async def get_inventory(
    user_id: str,
    limit: int = 100,
    offset: int = 0,
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format"
        )
    
    # Validate limit
    limit = min(max(1, limit), 1000)
    offset = max(0, offset)
    
    # Query inventory items
    stmt = (
        select(UserInventoryItem)
        .where(UserInventoryItem.user_id == user_uuid)
        .order_by(UserInventoryItem.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    items = result.scalars().all()
    
    
    # Get total count
    count_stmt = select(UserInventoryItem).where(
        UserInventoryItem.user_id == user_uuid
    )
    total_result = await session.execute(count_stmt)
    total = len(total_result.scalars().all())
    
    return InventoryResponse(
        user_id=user_id,
        items=[
            InventoryItemResponse(
                id=item.id,
                blueprint_id=item.blueprint_id,
                quantity=item.quantity,
                price_cents=item.price_cents,
                properties=item.properties,
                external_stock_id=item.external_stock_id,
                description=item.description,
                user_data_field=item.user_data_field,
                graded=item.graded,
                updated_at=item.updated_at.isoformat(),
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
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid user_id format: {str(e)}"
        )
    
    # Verify user exists
    stmt = select(UserSyncSettings).where(
        UserSyncSettings.user_id == user_uuid
    )
    result = await session.execute(stmt)
    sync_settings = result.scalar_one_or_none()
    
    if not sync_settings:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found in sync settings"
        )
    
    # Queue periodic sync task
    task = periodic_sync_from_cardtrader.delay(
        user_id=user_id,
        blueprint_id=blueprint_id
    )
    
    logger.info(
        f"Queued periodic sync from CardTrader for user {user_id}, "
        f"task_id={task.id}"
    )
    
    return {
        "status": "accepted",
        "task_id": task.id,
        "user_id": user_id,
        "blueprint_id": blueprint_id,
        "message": "Sync from CardTrader queued"
    }


@router.get("/debug-logs")
async def get_debug_logs(limit: int = 100) -> dict:
    """
    Get debug logs for frontend display.
    
    Note: This endpoint reads from application logs, not from a separate debug file.
    For production, consider using a proper log aggregation service.
    
    Args:
        limit: Maximum number of log entries to return (default: 100, max: 1000)
        
    Returns:
        List of log entries (empty for now, as we use structured logging)
    """
    limit = min(max(1, limit), 1000)
    
    # TODO: Implement log reading from structured logging system
    # For now, return empty as we use standard Python logging
    return {
        "logs": [],
        "total": 0,
        "limit": limit,
        "message": "Debug logs endpoint - use application logs for detailed information"
    }
