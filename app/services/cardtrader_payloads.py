"""Build deterministic CardTrader mutation payloads from local inventory."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from app.core.cardtrader_properties import (
    filter_properties_for_cardtrader,
    normalize_condition,
    validate_and_normalize_properties,
)
from app.models.inventory import UserInventoryItem


def _coerce_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in {"true", "1", "yes"}:
            return True
        if normalised in {"false", "0", "no"}:
            return False
    raise ValueError(f"Invalid boolean value for {field}")


def build_product_update_payload(item: UserInventoryItem) -> Dict[str, Any]:
    if not item.external_stock_id:
        raise ValueError("Inventory item has no CardTrader product ID")

    payload: Dict[str, Any] = {
        "id": int(item.external_stock_id),
        "price": item.price_cents / 100.0,
        "quantity": item.quantity,
    }
    if item.description is not None:
        payload["description"] = item.description
    if item.user_data_field is not None:
        payload["user_data_field"] = item.user_data_field
    if item.graded is not None:
        payload["graded"] = item.graded

    source_properties = item.properties or {}
    normalised = validate_and_normalize_properties(source_properties, strict=False)
    properties = filter_properties_for_cardtrader(normalised, include_read_only=False)
    properties.pop("graded", None)

    if "condition" in source_properties:
        condition = normalize_condition(source_properties["condition"])
        if condition:
            properties["condition"] = condition

    for boolean_property in ("signed", "altered"):
        if boolean_property in source_properties:
            properties[boolean_property] = _coerce_bool(
                source_properties[boolean_property], boolean_property
            )

    if "mtg_foil" in source_properties:
        properties["mtg_foil"] = _coerce_bool(source_properties["mtg_foil"], "mtg_foil")

    language = source_properties.get("mtg_language")
    if isinstance(language, str) and language.strip():
        properties["mtg_language"] = language.strip()[:2].lower()

    if properties:
        payload["properties"] = properties
    return payload


def build_product_create_payload(
    snapshot: Mapping[str, Any],
    *,
    quantity: int,
    user_data_field: Optional[str],
) -> Dict[str, Any]:
    """Recreate a product deleted by a full trade reservation."""
    blueprint_id = int(snapshot["blueprint_id"])
    price_cents = int(snapshot["price_cents"])
    if blueprint_id <= 0 or price_cents <= 0 or quantity <= 0:
        raise ValueError("Invalid CardTrader recreation payload")

    source_properties = dict(snapshot.get("properties") or {})
    normalised = validate_and_normalize_properties(source_properties, strict=True)
    properties = filter_properties_for_cardtrader(
        normalised,
        include_read_only=False,
    )
    properties.pop("graded", None)

    payload: Dict[str, Any] = {
        "blueprint_id": blueprint_id,
        "price": price_cents / 100.0,
        "quantity": quantity,
        "error_mode": "strict",
        "graded": bool(snapshot.get("graded")),
        "user_data_field": user_data_field,
    }
    description = snapshot.get("description")
    if isinstance(description, str):
        payload["description"] = description
    if properties:
        payload["properties"] = properties
    return payload
