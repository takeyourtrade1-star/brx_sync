# 🚀 BRX Sync - Quick Start Guide

## ⚡ Avvio Rapido (5 minuti)

### 1. Verifica Setup

```bash
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync

# Attiva virtual environment
source venv/bin/activate

# Verifica che tutto sia installato
pip list | grep -E "fastapi|sqlalchemy|celery|redis"
```

### 2. Test Import Moduli

```bash
# Test rapido che tutti i moduli si importino correttamente
python3 -c "
from app.core.exceptions import BRXSyncError
from app.core.logging import get_logger
from app.core.health import get_health_status
from app.api.v1.schemas import UpdateInventoryItemRequest
print('✅ Tutti i moduli OK')
"
```

**Se vedi errori**: Verifica che tutte le dipendenze siano installate:
```bash
pip install -r requirements.txt
```

### 3. Avvia il Server

```bash
# Terminal 1: FastAPI Server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Verifica**: Dovresti vedere:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete.
```

### 4. Avvia il Worker Celery

**IMPORTANTE**: Il worker Celery è necessario per processare le sincronizzazioni!

```bash
# Terminal 2: Celery Worker
cd /Users/julianrovera/Desktop/EBARTEX_AWS_Terraform-MacBook/Main-app/backend/brx_sync
source venv/bin/activate

# Metodo 1: Script rapido (consigliato)
./restart_worker.sh

# Metodo 2: Manuale
celery -A app.tasks.celery_app worker --loglevel=info -Q bulk-sync,high-priority,default
```

**Verifica**: Dovresti vedere:
```
[INFO/MainProcess] Connected to redis://localhost:6379//
[INFO/MainProcess] celery@hostname ready.
```

**Per riavviare il worker**: Vedi `WORKER_GUIDE.md` per dettagli completi.

### 5. Test Health Endpoints

```bash
# Terminal 2: Test endpoints
curl http://localhost:8000/health/live
# Risultato: {"status":"alive"}

curl http://localhost:8000/health | jq
# Risultato: Status dettagliato per tutti i componenti
```

### 6. Test Exception Handling

```bash
# Test con user_id invalido (dovrebbe restituire ValidationError strutturato)
curl -X POST http://localhost:8000/api/v1/sync/start/invalid-uuid | jq

# Dovresti vedere un errore strutturato con:
# - error.code: "VALIDATION_ERROR"
# - error.message
# - error.context
# - trace_id
```

---

## 🧪 Test Completo

Esegui lo script di test automatico:

```bash
./test_setup.sh
```

Questo verifica:
- ✅ Import di tutti i moduli
- ✅ Exception hierarchy
- ✅ Validators
- ✅ Pydantic schemas
- ✅ Type checking base

---

## 📋 Checklist Verifica

Prima di procedere, verifica:

- [ ] Server si avvia senza errori (`uvicorn app.main:app`)
- [ ] Worker Celery è avviato (`./restart_worker.sh` o manualmente)
- [ ] Health endpoints funzionano (`/health/live`, `/health/ready`, `/health`)
- [ ] Exception handling restituisce errori strutturati
- [ ] Logs sono in formato strutturato (JSON se DEBUG=false)
- [ ] Frontend test accessibile: http://localhost:8000/static/index.html

---

## 🐛 Risoluzione Problemi

### Errore: "Can't patch loop of type uvloop.Loop"

**Risolto**: Ho rimosso `nest_asyncio.apply()` dal livello di modulo in `sync_tasks.py`. 
Non è più necessario perché usiamo `get_isolated_db_session` che crea event loop isolati.

### Errore: "Module not found"

```bash
# Reinstalla dipendenze
pip install -r requirements.txt
```

### Errore: "ImportError: cannot import name"

Verifica che i file siano nella posizione corretta:
```bash
ls -la app/core/exceptions.py
ls -la app/core/logging.py
ls -la app/api/v1/schemas.py
```

---

## 📚 Documentazione Completa

- **TESTING_GUIDE.md**: Guida completa al testing
- **NEXT_STEPS.md**: Prossimi passi e priorità
- **DEVELOPMENT.md**: Guida sviluppo e code style
- **ARCHITETTURA_ENTERPRISE.md**: Architettura e design

---

## ✅ Prossimi Passi

1. **Esegui test**: `./test_setup.sh`
2. **Avvia server**: `uvicorn app.main:app --reload`
3. **Testa endpoints**: Usa `curl` o il frontend in `/static/index.html`
4. **Leggi NEXT_STEPS.md**: Per integrazione completa
