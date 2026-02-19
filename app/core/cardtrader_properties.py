"""
CardTrader API - Property Validation and Mapping

This module provides validation and mapping functions for CardTrader product properties.
Based on CardTrader V2 API documentation.
"""

from typing import Dict, Any, Optional, List

# CardTrader valid condition values (from API documentation line 222-234)
VALID_CONDITIONS = [
    "Mint",
    "Near Mint",
    "Slightly Played",
    "Moderately Played",
    "Played",
    "Heavily Played",
    "Poor"
]

# Mapping from common variations to CardTrader valid values
CONDITION_MAPPING = {
    # Common variations
    "Lightly Played": "Slightly Played",
    "Damaged": "Poor",
    "NM": "Near Mint",
    "SP": "Slightly Played",
    "MP": "Moderately Played",
    "HP": "Heavily Played",
    "PL": "Played",
    "PO": "Poor",
    # Case variations
    "near mint": "Near Mint",
    "slightly played": "Slightly Played",
    "moderately played": "Moderately Played",
    "played": "Played",
    "heavily played": "Heavily Played",
    "poor": "Poor",
    "mint": "Mint",
}

# CardTrader read-only properties that cannot be modified
READ_ONLY_PROPERTIES = {
    "mtg_card_colors",
    "collector_number",
    "tournament_legal",
    "cmc",
    "mtg_rarity",
}

# Properties that should be top-level (not inside properties object)
TOP_LEVEL_PROPERTIES = {
    "price",
    "quantity",
    "id",
    "graded",
    "description",
    "user_data_field",
}


def normalize_condition(condition: str) -> Optional[str]:
    """
    Normalize condition value to CardTrader valid format.
    
    Args:
        condition: Condition string (may be in various formats)
        
    Returns:
        Normalized condition value or None if invalid
    """
    if not condition or not isinstance(condition, str):
        return None
    
    condition = condition.strip()
    
    # If already valid, return as-is
    if condition in VALID_CONDITIONS:
        return condition
    
    # Try mapping
    if condition in CONDITION_MAPPING:
        return CONDITION_MAPPING[condition]
    
    # Case-insensitive match
    condition_lower = condition.lower()
    for valid_condition in VALID_CONDITIONS:
        if valid_condition.lower() == condition_lower:
            return valid_condition
    
    # If no match found, return None (invalid condition)
    return None


def validate_and_normalize_properties(
    properties: Dict[str, Any],
    strict: bool = False
) -> Dict[str, Any]:
    """
    Validate and normalize properties for CardTrader API.
    
    Args:
        properties: Dictionary of properties to validate
        strict: If True, raise ValueError for invalid properties
        
    Returns:
        Normalized properties dictionary
        
    Raises:
        ValueError: If strict=True and invalid properties found
    """
    if not properties:
        return {}
    
    normalized = {}
    errors = []
    
    for key, value in properties.items():
        # Skip top-level properties (should not be in properties object)
        if key in TOP_LEVEL_PROPERTIES:
            continue
        
        # Skip read-only properties
        if key in READ_ONLY_PROPERTIES:
            continue
        
        # Normalize condition
        if key == "condition":
            normalized_condition = normalize_condition(value)
            if normalized_condition:
                normalized[key] = normalized_condition
            else:
                error_msg = f"Invalid condition value: '{value}'. Valid values: {VALID_CONDITIONS}"
                if strict:
                    errors.append(error_msg)
                else:
                    # In non-strict mode, skip invalid condition
                    pass
            continue
        
        # Validate boolean properties
        if key in ("mtg_foil", "signed", "altered"):
            if isinstance(value, bool):
                normalized[key] = value
            elif isinstance(value, str):
                # Convert string to boolean
                if value.lower() in ("true", "1", "yes", "on"):
                    normalized[key] = True
                elif value.lower() in ("false", "0", "no", "off", ""):
                    normalized[key] = False
                else:
                    error_msg = f"Invalid boolean value for '{key}': '{value}'"
                    if strict:
                        errors.append(error_msg)
                    else:
                        normalized[key] = False  # Default to False
            else:
                normalized[key] = bool(value)
            continue
        
        # Validate mtg_language (should be 2-letter code)
        if key == "mtg_language":
            if isinstance(value, str) and len(value.strip()) >= 2:
                normalized[key] = value.strip()[:2].lower()
            else:
                error_msg = f"Invalid language value: '{value}'. Should be 2-letter code (e.g., 'en', 'it', 'fr')"
                if strict:
                    errors.append(error_msg)
                else:
                    # Skip invalid language
                    pass
            continue
        
        # Include other properties as-is (let CardTrader validate them)
        normalized[key] = value
    
    if errors and strict:
        raise ValueError("; ".join(errors))
    
    return normalized


def filter_properties_for_cardtrader(
    properties: Optional[Dict[str, Any]],
    include_read_only: bool = False
) -> Dict[str, Any]:
    """
    Filter properties to only include editable ones for CardTrader.
    
    Args:
        properties: Properties dictionary
        include_read_only: If True, include read-only properties (will be ignored by CardTrader)
        
    Returns:
        Filtered properties dictionary
    """
    if not properties:
        return {}
    
    filtered = {}
    
    for key, value in properties.items():
        # Skip top-level properties
        if key in TOP_LEVEL_PROPERTIES:
            continue
        
        # Skip read-only properties unless explicitly included
        if not include_read_only and key in READ_ONLY_PROPERTIES:
            continue
        
        # Always include boolean properties (even if False)
        if isinstance(value, bool):
            filtered[key] = value
        # Include non-empty strings
        elif isinstance(value, str) and value.strip():
            filtered[key] = value
        # Include other non-None values
        elif value is not None:
            filtered[key] = value
    
    return filtered
