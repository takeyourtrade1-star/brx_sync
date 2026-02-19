# 🧪 BRX Sync - Guida Completa al Testing

## 📋 Indice
1. [Setup Iniziale](#setup-iniziale)
2. [Verifica Installazione](#verifica-installazione)
3. [Test delle Nuove Funzionalità](#test-delle-nuove-funzionalità)
4. [Test End-to-End](#test-end-to-end)
5. [Validazione Code Quality](#validazione-code-quality)
6. [Prossimi Passi](#prossimi-passi)

---

## 🚀 Setup Iniziale

### 1. Installare Dipendenze di Sviluppo

```bash
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync

# Attiva virtual environment
source venv/bin/activate

# Installa dipendenze di sviluppo
pip install -r requirements-dev.txt
```

### 2. Verificare Configurazione

```bash
# Verifica che tutte le variabili d'ambiente siano configurate
python -c "from app.core.config import get_settings; s = get_settings(); print('✅ Config OK')"
```

Se vedi errori, controlla il file `.env`.

---

## ✅ Verifica Installazione

### Test 1: Verifica Import dei Nuovi Moduli

```bash
python -c "
from app.core.exceptions import BRXSyncError, SyncError, InventoryError
from app.core.exception_handlers import EXCEPTION_HANDLERS
from app.core.logging import get_logger, LogContext
from app.core.health import get_health_status
from app.core.metrics import increment_counter, get_metrics
from app.core.validators import validate_uuid, validate_blueprint_id
from app.core.security import sanitize_string
from app.api.v1.schemas import UpdateInventoryItemRequest, InventoryItemResponse
print('✅ Tutti i moduli importati correttamente')
"
```

**Risultato atteso**: `✅ Tutti i moduli importati correttamente`

### Test 2: Verifica Type Checking

```bash
# Verifica type hints (dovrebbe mostrare eventuali errori)
mypy app/core/exceptions.py app/core/logging.py app/core/health.py --show-error-codes
```

**Risultato atteso**: Nessun errore (o solo errori minori da risolvere)

### Test 3: Verifica Linting

```bash
# Verifica formato codice
black --check app/core/ app/api/v1/schemas.py

# Verifica import order
isort --check app/core/ app/api/v1/schemas.py
```

**Risultato atteso**: `All done! ✨ 🍰 ✨`

---

## 🧪 Test delle Nuove Funzionalità

### Test 1: Exception Handling

```bash
# Crea file di test
cat > test_exceptions.py << 'EOF'
import sys
sys.path.insert(0, '.')

from app.core.exceptions import (
    SyncError,
    SyncInProgressError,
    InventoryItemNotFoundError,
    RateLimitError,
    ValidationError
)

# Test SyncInProgressError
try:
    raise SyncInProgressError(user_id="test-123", current_status="active")
except SyncInProgressError as e:
    assert e.status_code == 409
    assert "test-123" in e.detail
    assert e.context["user_id"] == "test-123"
    print("✅ SyncInProgressError: OK")

# Test InventoryItemNotFoundError
try:
    raise InventoryItemNotFoundError(item_id=999, user_id="test-123")
except InventoryItemNotFoundError as e:
    assert e.status_code == 404
    assert e.context["item_id"] == 999
    print("✅ InventoryItemNotFoundError: OK")

# Test ValidationError
try:
    raise ValidationError(detail="Invalid UUID", field="user_id", value="not-a-uuid")
except ValidationError as e:
    assert e.status_code == 400
    assert e.context["field"] == "user_id"
    print("✅ ValidationError: OK")

print("\n🎉 Tutti i test delle eccezioni passati!")
EOF

python test_exceptions.py
```

**Risultato atteso**: Tutti i test passano

### Test 2: Logging Strutturato

```bash
cat > test_logging.py << 'EOF'
import sys
sys.path.insert(0, '.')

from app.core.logging import get_logger, LogContext, setup_logging

setup_logging()
logger = get_logger(__name__)

# Test logging base
logger.info("Test log message", extra={"test": True})
print("✅ Logging base: OK")

# Test con context
with LogContext(trace_id="test-trace-123", user_id="test-user-456"):
    logger.info("Test log with context", extra={"operation": "test"})
    print("✅ Logging con context: OK")

print("\n🎉 Test logging completati!")
EOF

python test_logging.py
```

**Risultato atteso**: Log in formato JSON (se DEBUG=false) o formato leggibile (se DEBUG=true)

### Test 3: Validators

```bash
cat > test_validators.py << 'EOF'
import sys
sys.path.insert(0, '.')

from app.core.validators import (
    validate_uuid,
    validate_blueprint_id,
    validate_external_stock_id,
    validate_quantity,
    validate_price_cents
)
from app.core.exceptions import ValidationError

# Test UUID validation
try:
    uuid = validate_uuid("550e8400-e29b-41d4-a716-446655440000")
    print(f"✅ UUID validation: OK ({uuid})")
except Exception as e:
    print(f"❌ UUID validation failed: {e}")

# Test invalid UUID
try:
    validate_uuid("not-a-uuid")
    print("❌ Should have raised ValidationError")
except ValidationError:
    print("✅ Invalid UUID correctly rejected")

# Test blueprint_id
try:
    bp_id = validate_blueprint_id(12345)
    print(f"✅ Blueprint ID validation: OK ({bp_id})")
except Exception as e:
    print(f"❌ Blueprint ID validation failed: {e}")

# Test quantity
try:
    qty = validate_quantity(10)
    print(f"✅ Quantity validation: OK ({qty})")
except Exception as e:
    print(f"❌ Quantity validation failed: {e}")

print("\n🎉 Test validators completati!")
EOF

python test_validators.py
```

**Risultato atteso**: Tutti i test passano

### Test 4: Health Checks

```bash
cat > test_health.py << 'EOF'
import sys
import asyncio
sys.path.insert(0, '.')

from app.core.health import (
    check_postgresql,
    check_redis,
    check_mysql,
    get_health_status
)

async def test_health():
    print("Testing health checks...")
    
    # Test PostgreSQL
    pg_status = await check_postgresql()
    print(f"PostgreSQL: {pg_status['status']} - {pg_status.get('message', '')}")
    
    # Test Redis
    redis_status = await check_redis()
    print(f"Redis: {redis_status['status']} - {redis_status.get('message', '')}")
    
    # Test MySQL
    mysql_status = check_mysql()
    print(f"MySQL: {mysql_status['status']} - {mysql_status.get('message', '')}")
    
    # Test aggregated status
    health = await get_health_status()
    print(f"\nOverall Health: {health['status']}")
    print(f"Components: {list(health['components'].keys())}")
    
    print("\n🎉 Health checks completati!")

asyncio.run(test_health())
EOF

python test_health.py
```

**Risultato atteso**: Status per ogni componente (healthy/unhealthy/degraded)

### Test 5: Pydantic Schemas

```bash
cat > test_schemas.py << 'EOF'
import sys
sys.path.insert(0, '.')

from app.api.v1.schemas import (
    UpdateInventoryItemRequest,
    InventoryItemResponse,
    SyncStatusResponse
)
from pydantic import ValidationError

# Test UpdateInventoryItemRequest - valid
try:
    request = UpdateInventoryItemRequest(
        quantity=5,
        price_cents=1600,
        description="Test item",
        properties={"condition": "Near Mint"}
    )
    print(f"✅ Valid request: {request.quantity} items, €{request.price_cents/100:.2f}")
except Exception as e:
    print(f"❌ Validation failed: {e}")

# Test UpdateInventoryItemRequest - invalid (negative quantity)
try:
    request = UpdateInventoryItemRequest(quantity=-1)
    print("❌ Should have raised ValidationError")
except ValidationError as e:
    print("✅ Invalid quantity correctly rejected")

# Test InventoryItemResponse
response = InventoryItemResponse(
    id=1,
    blueprint_id=12345,
    quantity=5,
    price_cents=1600,
    updated_at="2026-02-19T10:00:00Z"
)
print(f"✅ Response model: Item {response.id}, {response.quantity} items")

print("\n🎉 Test schemas completati!")
EOF

python test_schemas.py
```

**Risultato atteso**: Tutti i test passano

---

## 🔄 Test End-to-End

### Test 1: Avviare il Server

```bash
# Terminal 1: Avvia FastAPI
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
source venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Verifica**: Dovresti vedere:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete.
```

### Test 2: Health Endpoints

```bash
# Terminal 2: Test health endpoints
curl http://localhost:8000/health/live
# Risultato atteso: {"status":"alive"}

curl http://localhost:8000/health/ready
# Risultato atteso: Status 200 con dettagli componenti

curl http://localhost:8000/health
# Risultato atteso: Status dettagliato per tutti i componenti
```

### Test 3: Exception Handling

```bash
# Test con user_id invalido (dovrebbe restituire ValidationError)
curl -X POST http://localhost:8000/api/v1/sync/start/invalid-uuid \
  -H "Content-Type: application/json"

# Risultato atteso: 
# {
#   "error": {
#     "code": "VALIDATION_ERROR",
#     "message": "Invalid user_id format: must be a valid UUID",
#     "field": "user_id",
#     "trace_id": "..."
#   }
# }
```

### Test 4: API con Schemas

```bash
# Test update inventory item con schema valido
curl -X PUT http://localhost:8000/api/v1/sync/inventory/YOUR_USER_ID/item/1 \
  -H "Content-Type: application/json" \
  -d '{
    "quantity": 10,
    "price_cents": 2000,
    "description": "Test update",
    "properties": {
      "condition": "Near Mint",
      "signed": false
    }
  }'

# Test con dati invalidi (dovrebbe restituire ValidationError)
curl -X PUT http://localhost:8000/api/v1/sync/inventory/YOUR_USER_ID/item/1 \
  -H "Content-Type: application/json" \
  -d '{
    "quantity": -1
  }'

# Risultato atteso: Validation error con dettagli campo
```

---

## 📊 Validazione Code Quality

### 1. Type Checking Completo

```bash
# Verifica type hints in tutto il progetto
mypy app/ --show-error-codes --no-error-summary

# Salva report
mypy app/ --html-report mypy-report
```

**Obiettivo**: < 10 errori type (gradualmente migliorare)

### 2. Code Formatting

```bash
# Formatta tutto il codice
black app/ tests/

# Verifica import order
isort app/ tests/
```

### 3. Linting

```bash
# Verifica con flake8
flake8 app/ --max-line-length=100 --exclude=venv,migrations

# Verifica con ruff (più veloce)
ruff check app/
```

### 4. Test Coverage

```bash
# Esegui test con coverage
pytest --cov=app --cov-report=html --cov-report=term-missing

# Apri report HTML
open htmlcov/index.html
```

**Obiettivo**: > 80% coverage (gradualmente aumentare)

---

## 🎯 Prossimi Passi

### Fase 1: Integrazione Completa (Settimana 1)

#### 1.1 Sostituire Logging Ovunque

```bash
# Trova tutti i file che usano logging.getLogger
grep -r "logging.getLogger" app/ --include="*.py"

# Per ogni file, sostituire:
# OLD: logger = logging.getLogger(__name__)
# NEW: from app.core.logging import get_logger
#      logger = get_logger(__name__)
```

**File da aggiornare**:
- `app/services/cardtrader_client.py`
- `app/tasks/sync_tasks.py`
- `app/api/v1/routes/sync.py`
- `app/services/rate_limiter.py`
- `app/services/adaptive_rate_limiter.py`
- `app/services/circuit_breaker.py`

#### 1.2 Usare Nuove Eccezioni

Sostituire `HTTPException` con eccezioni custom:
- `HTTPException(404, ...)` → `InventoryItemNotFoundError(...)`
- `HTTPException(409, ...)` → `SyncInProgressError(...)`
- `HTTPException(400, ...)` → `ValidationError(...)`

#### 1.3 Aggiungere Context ai Log

```python
# Esempio: Aggiungere trace_id e user_id ai log
from app.core.logging import LogContext

with LogContext(trace_id=trace_id, user_id=user_id):
    logger.info("Operation started", extra={"operation": "sync"})
```

### Fase 2: Testing Completo (Settimana 2)

#### 2.1 Unit Tests

Creare test per ogni service:
- `tests/unit/test_services/test_cardtrader_client.py`
- `tests/unit/test_services/test_adaptive_rate_limiter.py`
- `tests/unit/test_services/test_circuit_breaker.py`
- `tests/unit/test_core/test_validators.py`
- `tests/unit/test_core/test_health.py`

#### 2.2 Integration Tests

Creare test end-to-end:
- `tests/integration/test_api/test_sync_endpoints.py`
- `tests/integration/test_sync_flow/test_bulk_sync.py`
- `tests/integration/test_sync_flow/test_update_delete.py`

### Fase 3: Documentazione (Settimana 3)

#### 3.1 Docstrings Complete

Aggiungere docstrings Google-style a tutti i file:
- Tutte le funzioni pubbliche
- Tutte le classi
- Tutti i moduli

#### 3.2 API Documentation

Verificare che OpenAPI docs siano complete:
```bash
# Avvia server e visita
open http://localhost:8000/docs
```

### Fase 4: Monitoring & Metrics (Settimana 4)

#### 4.1 Integrare Metrics

Aggiungere metriche alle operazioni critiche:
```python
from app.core.metrics import increment_counter, record_histogram

increment_counter("sync_operations", labels={"type": "bulk_sync"})
record_histogram("sync_duration", duration_seconds)
```

#### 4.2 Dashboard Metrics

Creare endpoint per esporre metrics:
```python
@app.get("/metrics")
async def get_metrics():
    from app.core.metrics import get_metrics
    return get_metrics()
```

---

## 🧹 Cleanup e Ottimizzazione

### Rimuovere Codice Legacy

```bash
# Rimuovere _log_to_file da sync_tasks.py (sostituire con logger strutturato)
# Rimuovere DEBUG_LOG_PATH da sync.py
# Rimuovere commenti #region agent log
```

### Ottimizzare Imports

```bash
# Verifica import non usati
ruff check app/ --select F401

# Rimuovi import non usati
ruff check app/ --select F401 --fix
```

---

## ✅ Checklist Finale

Prima di considerare il progetto "enterprise-ready":

- [ ] Tutti i test passano (`pytest`)
- [ ] Type checking senza errori critici (`mypy app/`)
- [ ] Code formatting corretto (`black --check`)
- [ ] Linting pulito (`ruff check`)
- [ ] Coverage > 80% (`pytest --cov`)
- [ ] Health checks funzionanti (`curl /health`)
- [ ] Exception handling testato (tutte le eccezioni gestite)
- [ ] Logging strutturato attivo (log in JSON)
- [ ] Documentazione completa (docstrings + DEVELOPMENT.md)
- [ ] API docs aggiornate (`/docs` endpoint)

---

## 🚨 Troubleshooting

### Errore: "Module not found"
```bash
# Verifica che il virtual environment sia attivo
which python  # Dovrebbe puntare a venv/bin/python

# Reinstalla dipendenze
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### Errore: "ImportError: cannot import name"
```bash
# Verifica che i file siano nella posizione corretta
ls -la app/core/exceptions.py
ls -la app/core/logging.py

# Verifica __init__.py
ls -la app/core/__init__.py
```

### Errore: "TypeError: 'NoneType' object is not callable"
```bash
# Verifica che le funzioni siano chiamate correttamente
# Controlla che non ci siano conflitti di nomi
```

### Health Check Fallisce
```bash
# Verifica connessioni
psql $DATABASE_URL -c "SELECT 1"
redis-cli ping
mysql -h $MYSQL_HOST -u $MYSQL_USER -p$MYSQL_PASSWORD -e "SELECT 1"
```

---

## 📚 Risorse Utili

- **Logs**: `tail -f logs/brx_sync.log | jq`
- **API Docs**: http://localhost:8000/docs
- **Health**: http://localhost:8000/health
- **Metrics**: http://localhost:8000/metrics (da implementare)

---

## 🎓 Conclusione

Dopo aver completato tutti i test sopra, il codice sarà:

✅ **Enterprise-grade**: Exception handling, type safety, logging strutturato  
✅ **Testabile**: Test infrastructure completa  
✅ **Manutenibile**: Documentazione, code style, validazione  
✅ **Osservabile**: Health checks, metrics, structured logging  
✅ **Sicuro**: Input validation, sanitization, error handling  

**Prossimo step**: Integrare gradualmente le nuove funzionalità nei file esistenti e aumentare la coverage dei test.
