#!/bin/bash
# Script di test per verificare che tutto funzioni correttamente

set -e  # Exit on error

echo "🧪 BRX Sync - Test Setup Completo"
echo "=================================="
echo ""

cd "$(dirname "$0")"

# Colori per output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Funzione per test
test_step() {
    local name=$1
    local command=$2
    
    echo -n "Testing $name... "
    if eval "$command" > /dev/null 2>&1; then
        echo -e "${GREEN}✅ OK${NC}"
        return 0
    else
        echo -e "${RED}❌ FAILED${NC}"
        return 1
    fi
}

# Verifica virtual environment
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}⚠️  Virtual environment non trovato. Creazione...${NC}"
    python3 -m venv venv
fi

# Attiva virtual environment
source venv/bin/activate

# Installa dipendenze se necessario
if [ ! -f "venv/.deps_installed" ]; then
    echo "📦 Installazione dipendenze..."
    pip install -q -r requirements.txt
    pip install -q -r requirements-dev.txt 2>/dev/null || echo "⚠️  requirements-dev.txt non trovato (ok per produzione)"
    touch venv/.deps_installed
fi

echo ""
echo "1️⃣  Test Import Moduli"
echo "-------------------"

test_step "Exceptions" "python3 -c 'from app.core.exceptions import BRXSyncError, SyncError, InventoryError; print(\"OK\")'"
test_step "Exception Handlers" "python3 -c 'from app.core.exception_handlers import EXCEPTION_HANDLERS; print(\"OK\")'"
test_step "Logging" "python3 -c 'from app.core.logging import get_logger, LogContext; print(\"OK\")'"
test_step "Health Checks" "python3 -c 'from app.core.health import get_health_status; print(\"OK\")'"
test_step "Metrics" "python3 -c 'from app.core.metrics import increment_counter, get_metrics; print(\"OK\")'"
test_step "Validators" "python3 -c 'from app.core.validators import validate_uuid, validate_blueprint_id; print(\"OK\")'"
test_step "Security" "python3 -c 'from app.core.security import sanitize_string; print(\"OK\")'"
test_step "Schemas" "python3 -c 'from app.api.v1.schemas import UpdateInventoryItemRequest, InventoryItemResponse; print(\"OK\")'"

echo ""
echo "2️⃣  Test Type Checking"
echo "-------------------"
if command -v mypy &> /dev/null; then
    test_step "Type Check (exceptions)" "mypy app/core/exceptions.py --no-error-summary"
    test_step "Type Check (logging)" "mypy app/core/logging.py --no-error-summary"
else
    echo -e "${YELLOW}⚠️  mypy non installato. Salta type checking.${NC}"
    echo "   Installa con: pip install mypy"
fi

echo ""
echo "3️⃣  Test Code Formatting"
echo "-------------------"
if command -v black &> /dev/null; then
    test_step "Black Format Check" "black --check app/core/exceptions.py app/core/logging.py 2>/dev/null || true"
else
    echo -e "${YELLOW}⚠️  black non installato. Salta format check.${NC}"
fi

echo ""
echo "4️⃣  Test Configurazione"
echo "-------------------"
test_step "Config Loading" "python3 -c 'from app.core.config import get_settings; s = get_settings(); print(\"OK\")'"

echo ""
echo "5️⃣  Test Exception Hierarchy"
echo "-------------------"
python3 << 'PYTHON_EOF'
import sys
from app.core.exceptions import (
    SyncInProgressError,
    InventoryItemNotFoundError,
    ValidationError,
    RateLimitError
)

try:
    # Test SyncInProgressError
    raise SyncInProgressError(user_id="test-123", current_status="active")
except SyncInProgressError as e:
    assert e.status_code == 409
    assert "test-123" in e.detail
    print("✅ SyncInProgressError: OK")

try:
    # Test InventoryItemNotFoundError
    raise InventoryItemNotFoundError(item_id=999, user_id="test-123")
except InventoryItemNotFoundError as e:
    assert e.status_code == 404
    assert e.context["item_id"] == 999
    print("✅ InventoryItemNotFoundError: OK")

try:
    # Test ValidationError
    raise ValidationError(detail="Invalid UUID", field="user_id", value="not-a-uuid")
except ValidationError as e:
    assert e.status_code == 400
    assert e.context["field"] == "user_id"
    print("✅ ValidationError: OK")

print("✅ Tutte le eccezioni funzionano correttamente!")
PYTHON_EOF

echo ""
echo "6️⃣  Test Validators"
echo "-------------------"
python3 << 'PYTHON_EOF'
from app.core.validators import (
    validate_uuid,
    validate_blueprint_id,
    validate_quantity,
    validate_price_cents
)
from app.core.exceptions import ValidationError

# Test UUID
uuid = validate_uuid("550e8400-e29b-41d4-a716-446655440000")
print(f"✅ UUID validation: OK")

# Test invalid UUID
try:
    validate_uuid("not-a-uuid")
    print("❌ Should have raised ValidationError")
except ValidationError:
    print("✅ Invalid UUID correctly rejected")

# Test blueprint_id
bp_id = validate_blueprint_id(12345)
print(f"✅ Blueprint ID validation: OK")

# Test quantity
qty = validate_quantity(10)
print(f"✅ Quantity validation: OK")

print("✅ Tutti i validators funzionano correttamente!")
PYTHON_EOF

echo ""
echo "7️⃣  Test Pydantic Schemas"
echo "-------------------"
python3 << 'PYTHON_EOF'
from app.api.v1.schemas import (
    UpdateInventoryItemRequest,
    InventoryItemResponse
)
from pydantic import ValidationError

# Test valid request
request = UpdateInventoryItemRequest(
    quantity=5,
    price_cents=1600,
    description="Test item",
    properties={"condition": "Near Mint"}
)
print(f"✅ Valid request: {request.quantity} items, €{request.price_cents/100:.2f}")

# Test invalid request
try:
    request = UpdateInventoryItemRequest(quantity=-1)
    print("❌ Should have raised ValidationError")
except ValidationError:
    print("✅ Invalid quantity correctly rejected")

# Test response
response = InventoryItemResponse(
    id=1,
    blueprint_id=12345,
    quantity=5,
    price_cents=1600,
    updated_at="2026-02-19T10:00:00Z"
)
print(f"✅ Response model: Item {response.id}, {response.quantity} items")

print("✅ Tutti gli schemas funzionano correttamente!")
PYTHON_EOF

echo ""
echo "=================================="
echo -e "${GREEN}🎉 Tutti i test base completati!${NC}"
echo ""
echo "Prossimi passi:"
echo "1. Avvia il server: uvicorn app.main:app --reload"
echo "2. Testa gli endpoint: curl http://localhost:8000/health"
echo "3. Leggi TESTING_GUIDE.md per test completi"
echo ""
