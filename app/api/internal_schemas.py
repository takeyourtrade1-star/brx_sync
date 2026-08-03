"""Strict schemas for the service-to-service inventory API."""

from typing import Any, Dict, List
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictInternalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReservationItemRequest(StrictInternalRequest):
    item_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=2_147_483_647)


class ReserveInventoryRequest(StrictInternalRequest):
    op_key: str = Field(..., min_length=1, max_length=255, pattern=r"^[A-Za-z0-9:_-]+$")
    user_id: UUID
    items: List[ReservationItemRequest] = Field(..., min_length=1, max_length=30)

    @field_validator("items")
    @classmethod
    def reject_duplicate_items(
        cls, items: List[ReservationItemRequest]
    ) -> List[ReservationItemRequest]:
        item_ids = [item.item_id for item in items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("item_id duplicati nello stesso batch")
        return items


class ReleaseInventoryRequest(StrictInternalRequest):
    op_key: str = Field(..., min_length=1, max_length=255, pattern=r"^[A-Za-z0-9:_-]+$")
    reservation_op_key: str = Field(..., min_length=1, max_length=255, pattern=r"^[A-Za-z0-9:_-]+$")
    user_id: UUID


class CreditInventoryItemRequest(StrictInternalRequest):
    blueprint_id: int = Field(..., gt=0, le=2_147_483_647)
    quantity: int = Field(..., gt=0, le=2_147_483_647)
    price_cents: int = Field(..., ge=0, le=2_147_483_647)
    properties: Dict[str, Any] | None = None
    description: str | None = Field(default=None, max_length=5000)
    graded: bool | None = None

    @field_validator("properties")
    @classmethod
    def validate_properties(cls, value: Dict[str, Any] | None) -> Dict[str, Any] | None:
        if value is None:
            return None
        if len(value) > 32:
            raise ValueError("properties may contain at most 32 entries")
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 64
                or not all(character.isalnum() or character in "_-" for character in key)
            ):
                raise ValueError("invalid property name")
            if item is None or isinstance(item, bool):
                continue
            if isinstance(item, str) and len(item) <= 256:
                continue
            if isinstance(item, int) and -(2**31) <= item <= 2**31 - 1:
                continue
            raise ValueError("property values must be bounded scalar values")
        return value


class CreditInventoryRequest(StrictInternalRequest):
    op_key: str = Field(..., min_length=1, max_length=255, pattern=r"^[A-Za-z0-9:_-]+$")
    user_id: UUID
    items: List[CreditInventoryItemRequest] = Field(..., min_length=1, max_length=30)


class InventoryOperationResponse(BaseModel):
    op_key: str
    kind: str
    status: str
    user_id: UUID
    items: List[Dict[str, Any]]
    replayed: bool = False
