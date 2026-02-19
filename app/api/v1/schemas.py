"""
Pydantic schemas for API request/response models.

All schemas include validation, examples, and descriptions for OpenAPI documentation.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# Request Schemas

class UpdateInventoryItemRequest(BaseModel):
    """Request schema for updating an inventory item."""
    
    quantity: Optional[int] = Field(
        None,
        ge=0,
        description="Item quantity (must be >= 0)",
        examples=[5],
    )
    price_cents: Optional[int] = Field(
        None,
        ge=0,
        description="Price in cents (must be >= 0)",
        examples=[1600],
    )
    description: Optional[str] = Field(
        None,
        max_length=5000,
        description="Product description visible to all users",
        examples=["Near Mint condition, first edition"],
    )
    user_data_field: Optional[str] = Field(
        None,
        max_length=1000,
        description="Custom metadata field for internal use (warehouse location, etc.)",
        examples=["Warehouse A, Shelf 3"],
    )
    graded: Optional[bool] = Field(
        None,
        description="Whether the product is graded (top-level field)",
        examples=[True],
    )
    properties: Optional[Dict[str, Any]] = Field(
        None,
        description="Product properties (condition, signed, altered, mtg_foil, mtg_language, etc.)",
        examples=[{
            "condition": "Near Mint",
            "signed": False,
            "altered": False,
            "mtg_foil": True,
            "mtg_language": "en",
        }],
    )
    
    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v: Optional[int]) -> Optional[int]:
        """Validate quantity is non-negative."""
        if v is not None and v < 0:
            raise ValueError("Quantity must be >= 0")
        return v
    
    @field_validator("price_cents")
    @classmethod
    def validate_price_cents(cls, v: Optional[int]) -> Optional[int]:
        """Validate price_cents is non-negative."""
        if v is not None and v < 0:
            raise ValueError("Price must be >= 0")
        return v
    
    @field_validator("description", "user_data_field")
    @classmethod
    def validate_string_fields(cls, v: Optional[str]) -> Optional[str]:
        """Validate string fields are not empty if provided."""
        if v is not None and not v.strip():
            return None  # Convert empty strings to None
        return v
    
    @model_validator(mode="after")
    def validate_at_least_one_field(self) -> "UpdateInventoryItemRequest":
        """Ensure at least one field is provided for update."""
        if all(
            v is None
            for v in [
                self.quantity,
                self.price_cents,
                self.description,
                self.user_data_field,
                self.graded,
                self.properties,
            ]
        ):
            raise ValueError("At least one field must be provided for update")
        return self
    
    class Config:
        """Pydantic config."""
        json_schema_extra = {
            "example": {
                "quantity": 5,
                "price_cents": 1600,
                "description": "Near Mint condition",
                "user_data_field": "Warehouse A",
                "graded": True,
                "properties": {
                    "condition": "Near Mint",
                    "signed": False,
                    "altered": False,
                    "mtg_foil": True,
                    "mtg_language": "en",
                },
            }
        }


class SetupTestUserRequest(BaseModel):
    """Request schema for setting up a test user."""
    
    user_id: str = Field(
        ...,
        description="User UUID",
        examples=["db24fb13-ec73-49b8-932c-f0043dd47e86"],
    )
    cardtrader_token: str = Field(
        ...,
        min_length=1,
        description="CardTrader API token (will be encrypted)",
        examples=["your_cardtrader_token_here"],
    )
    webhook_secret: Optional[str] = Field(
        None,
        description="Webhook secret for signature validation",
        examples=["your_webhook_secret"],
    )
    
    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        """Validate user_id is a valid UUID format."""
        import uuid
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("user_id must be a valid UUID")
        return v


# Response Schemas

class SyncStatusResponse(BaseModel):
    """Response schema for sync status."""
    
    user_id: str = Field(..., description="User UUID")
    sync_status: str = Field(..., description="Current sync status")
    last_sync_at: Optional[str] = Field(None, description="Last sync timestamp (ISO format)")
    last_error: Optional[str] = Field(None, description="Last error message if any")
    
    class Config:
        """Pydantic config."""
        json_schema_extra = {
            "example": {
                "user_id": "db24fb13-ec73-49b8-932c-f0043dd47e86",
                "sync_status": "idle",
                "last_sync_at": "2026-02-19T10:00:00Z",
                "last_error": None,
            }
        }


class InventoryItemResponse(BaseModel):
    """Response schema for inventory item."""
    
    id: int = Field(..., description="Item ID")
    blueprint_id: int = Field(..., description="CardTrader blueprint ID")
    quantity: int = Field(..., description="Current quantity")
    price_cents: int = Field(..., description="Price in cents")
    properties: Optional[Dict[str, Any]] = Field(None, description="Product properties")
    external_stock_id: Optional[str] = Field(
        None,
        description="CardTrader product ID (for targeted updates)",
    )
    description: Optional[str] = Field(None, description="Product description")
    user_data_field: Optional[str] = Field(None, description="Custom metadata field")
    graded: Optional[bool] = Field(None, description="Whether the product is graded")
    updated_at: str = Field(..., description="Last update timestamp (ISO format)")
    created_at: Optional[str] = Field(None, description="Creation timestamp (ISO format)")
    
    class Config:
        """Pydantic config."""
        json_schema_extra = {
            "example": {
                "id": 1,
                "blueprint_id": 230018,
                "quantity": 5,
                "price_cents": 1600,
                "properties": {
                    "condition": "Near Mint",
                    "mtg_foil": True,
                },
                "external_stock_id": "392763036",
                "description": "Near Mint condition",
                "user_data_field": "Warehouse A",
                "graded": True,
                "updated_at": "2026-02-19T10:00:00Z",
                "created_at": "2026-02-19T09:00:00Z",
            }
        }


class InventoryResponse(BaseModel):
    """Response schema for inventory list."""
    
    user_id: str = Field(..., description="User UUID")
    items: List[InventoryItemResponse] = Field(..., description="List of inventory items")
    total: int = Field(..., description="Total number of items")
    
    class Config:
        """Pydantic config."""
        json_schema_extra = {
            "example": {
                "user_id": "db24fb13-ec73-49b8-932c-f0043dd47e86",
                "items": [],
                "total": 0,
            }
        }


class SyncStartResponse(BaseModel):
    """Response schema for sync start operation."""
    
    status: str = Field(..., description="Operation status")
    task_id: str = Field(..., description="Celery task ID")
    user_id: str = Field(..., description="User UUID")
    message: str = Field(..., description="Status message")
    
    class Config:
        """Pydantic config."""
        json_schema_extra = {
            "example": {
                "status": "accepted",
                "task_id": "8ad5ad2b-4d47-4ce0-966a-c3505c6861f9",
                "user_id": "db24fb13-ec73-49b8-932c-f0043dd47e86",
                "message": "Bulk sync started",
            }
        }


class TaskStatusResponse(BaseModel):
    """Response schema for Celery task status."""
    
    task_id: str = Field(..., description="Task ID")
    status: str = Field(..., description="Task status (PENDING, STARTED, SUCCESS, FAILURE, etc.)")
    result: Optional[Dict[str, Any]] = Field(None, description="Task result if completed")
    error: Optional[str] = Field(None, description="Error message if failed")
    
    class Config:
        """Pydantic config."""
        json_schema_extra = {
            "example": {
                "task_id": "8ad5ad2b-4d47-4ce0-966a-c3505c6861f9",
                "status": "SUCCESS",
                "result": {"total_products": 100, "processed": 100},
                "error": None,
            }
        }


class UpdateInventoryItemResponse(BaseModel):
    """Response schema for inventory item update."""
    
    status: str = Field(..., description="Update status")
    item_id: int = Field(..., description="Item ID")
    quantity: int = Field(..., description="Updated quantity")
    price_cents: int = Field(..., description="Updated price in cents")
    description: Optional[str] = Field(None, description="Updated description")
    user_data_field: Optional[str] = Field(None, description="Updated user data field")
    graded: Optional[bool] = Field(None, description="Updated graded status")
    properties: Optional[Dict[str, Any]] = Field(None, description="Updated properties")
    cardtrader_sync_queued: bool = Field(
        ...,
        description="Whether sync to CardTrader was queued",
    )
    external_stock_id: Optional[str] = Field(
        None,
        description="CardTrader product ID (for debugging)",
    )
    has_external_id: bool = Field(
        ...,
        description="Whether item has external_stock_id (for debugging)",
    )


class DeleteInventoryItemResponse(BaseModel):
    """Response schema for inventory item deletion."""
    
    status: str = Field(..., description="Deletion status")
    item_id: int = Field(..., description="Deleted item ID")
    cardtrader_sync_queued: bool = Field(
        ...,
        description="Whether sync to CardTrader was queued",
    )
    external_stock_id: Optional[str] = Field(
        None,
        description="CardTrader product ID that was deleted",
    )


class PurchaseItemRequest(BaseModel):
    """Request schema for purchasing an item."""
    
    quantity: int = Field(
        ...,
        ge=1,
        description="Quantity to purchase (must be >= 1)",
        examples=[1],
    )
    
    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v: int) -> int:
        """Validate quantity is positive."""
        if v < 1:
            raise ValueError("Quantity must be >= 1")
        return v


class PurchaseItemResponse(BaseModel):
    """Response schema for item purchase operation."""
    
    status: str = Field(..., description="Purchase status (success, error)")
    item_id: int = Field(..., description="Item ID that was purchased")
    message: str = Field(..., description="Status message")
    available: bool = Field(..., description="Whether item was available")
    quantity_before: int = Field(..., description="Quantity before purchase")
    quantity_after: int = Field(..., description="Quantity after purchase (should be 0)")
    cardtrader_sync_queued: bool = Field(
        ...,
        description="Whether sync to CardTrader was queued",
    )
    external_stock_id: Optional[str] = Field(
        None,
        description="CardTrader product ID",
    )
    error: Optional[str] = Field(
        None,
        description="Error message if purchase failed",
    )
    
    class Config:
        """Pydantic config."""
        json_schema_extra = {
            "example": {
                "status": "success",
                "item_id": 1,
                "message": "Item purchased successfully",
                "available": True,
                "quantity_before": 1,
                "quantity_after": 0,
                "cardtrader_sync_queued": True,
                "external_stock_id": "392763036",
                "error": None,
            }
        }
