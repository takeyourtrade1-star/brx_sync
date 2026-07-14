"""Private service-to-service routes for inventory mutations."""

from typing import Any, Dict, NoReturn

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.internal_dependencies import verify_internal_token
from app.api.internal_schemas import (
    CreditInventoryRequest,
    InventoryOperationResponse,
    ReleaseInventoryRequest,
    ReserveInventoryRequest,
)
from app.core.database import get_db_session
from app.services.inventory_operations import (
    InventoryOperationError,
    consume_inventory,
    credit_inventory,
    release_inventory,
    reserve_inventory,
)

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(verify_internal_token)],
)


def _raise_operation_error(error: InventoryOperationError) -> NoReturn:
    detail: Dict[str, Any] = {"code": error.code, "message": error.message}
    if error.result is not None:
        detail["result"] = error.result
    raise HTTPException(status_code=error.status_code, detail=detail)


@router.post("/reservations", response_model=InventoryOperationResponse)
async def create_reservation(
    request: ReserveInventoryRequest,
    session: AsyncSession = Depends(get_db_session),
) -> InventoryOperationResponse:
    try:
        result = await reserve_inventory(session, request)
    except InventoryOperationError as error:
        _raise_operation_error(error)
    return InventoryOperationResponse.model_validate(result)


@router.post("/reservations/release", response_model=InventoryOperationResponse)
async def release_reservation(
    request: ReleaseInventoryRequest,
    session: AsyncSession = Depends(get_db_session),
) -> InventoryOperationResponse:
    try:
        result = await release_inventory(session, request)
    except InventoryOperationError as error:
        _raise_operation_error(error)
    return InventoryOperationResponse.model_validate(result)


@router.post("/reservations/consume", response_model=InventoryOperationResponse)
async def consume_reservation(
    request: ReleaseInventoryRequest,
    session: AsyncSession = Depends(get_db_session),
) -> InventoryOperationResponse:
    try:
        result = await consume_inventory(session, request)
    except InventoryOperationError as error:
        _raise_operation_error(error)
    return InventoryOperationResponse.model_validate(result)


@router.post("/credit", response_model=InventoryOperationResponse)
async def credit_trade_inventory(
    request: CreditInventoryRequest,
    session: AsyncSession = Depends(get_db_session),
) -> InventoryOperationResponse:
    try:
        result = await credit_inventory(session, request)
    except InventoryOperationError as error:
        _raise_operation_error(error)
    return InventoryOperationResponse.model_validate(result)
