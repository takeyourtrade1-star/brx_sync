# 🧪 Test Locale - BRX Sync

Guida per testare il microservizio in locale.

## 📋 Prerequisiti

1. **PostgreSQL 16** in esecuzione
2. **MySQL** accessibile (per blueprint mapping)
3. **Redis** in esecuzione
4. **Python 3.12+**

## 🚀 Setup Rapido

### 1. Configurazione Ambiente

```bash
# Copia e modifica .env
cp .env.example .env
# Modifica .env con le tue credenziali
```

Variabili d'ambiente necessarie:
- `DATABASE_URL`: PostgreSQL connection string
- `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`
- `REDIS_URL`: Redis connection (es: `redis://localhost:6379/0`)
- `FERNET_KEY`: Chiave Fernet per encryption (genera con: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)

### 2. Setup Database

```bash
# Crea database PostgreSQL
createdb brx_sync_db

# Esegui schema SQL
psql -d brx_sync_db -f schema.sql

# Oppure usa Alembic (dopo aver configurato DATABASE_URL)
alembic upgrade head
```

### 3. Avvio con Script Automatico

```bash
./start_local.sh
```

Lo script:
- Verifica/crea .env
- Crea virtual environment
- Installa dipendenze
- Esegue test connessioni
- Avvia FastAPI server

### 4. Avvio Manuale

```bash
# Crea virtual environment
python3 -m venv venv
source venv/bin/activate  # Su Windows: venv\Scripts\activate

# Installa dipendenze
pip install -r requirements.txt

# Test connessioni
python test_local.py

# Avvia FastAPI
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 🌐 Frontend Test

Apri nel browser:
```
http://localhost:8000/static/index.html
```

Il frontend permette di:
- ✅ Testare connessioni
- ✅ Configurare User ID e Token CardTrader
- ✅ Avviare bulk sync
- ✅ Verificare stato sincronizzazione
- ✅ Visualizzare inventario
- ✅ Testare endpoint API

## 🧪 Test con Python

### Test Connessioni Base

```bash
python test_local.py
```

### Test con Token CardTrader

```bash
export CARDTRADER_TOKEN='your_token_here'
python test_local.py
```

## 📡 Endpoint Disponibili

### Health Checks
- `GET /health/live` - Liveness probe
- `GET /health/ready` - Readiness probe

### Sync API
- `POST /api/v1/sync/start/{user_id}` - Avvia bulk sync
- `GET /api/v1/sync/status/{user_id}` - Stato sincronizzazione
- `GET /api/v1/sync/inventory/{user_id}` - Lista inventario
- `POST /api/v1/sync/webhook/{webhook_id}` - Webhook CardTrader

### Documentazione
- `GET /docs` - Swagger UI (solo in DEBUG mode)
- `GET /redoc` - ReDoc (solo in DEBUG mode)

## 🔧 Test CardTrader API

Per testare la connessione a CardTrader:

1. Ottieni il token dalla pagina settings di CardTrader
2. Esporta il token:
   ```bash
   export CARDTRADER_TOKEN='your_token'
   ```
3. Esegui test:
   ```bash
   python test_local.py
   ```

Il test verificherà:
- ✅ Endpoint `/info` (app info e shared_secret)
- ✅ Endpoint `/expansions/export` (lista expansions)
- ✅ Endpoint `/products/export` (primi prodotti)

## 📊 Verifica Database

### PostgreSQL

```sql
-- Verifica tabelle
SELECT table_name FROM information_schema.tables 
WHERE table_schema = 'public';

-- Verifica sync settings
SELECT * FROM user_sync_settings;

-- Verifica inventory
SELECT COUNT(*) FROM user_inventory_items;
```

### MySQL (Blueprint Mapping)

```sql
-- Test mapping
SELECT id, cardtrader_id FROM cards_prints WHERE cardtrader_id = 1 LIMIT 1;
SELECT id, cardtrader_id FROM op_prints WHERE cardtrader_id = 1 LIMIT 1;
SELECT id, cardtrader_id FROM pk_prints WHERE cardtrader_id = 1 LIMIT 1;
```

## 🐛 Troubleshooting

### Errore: "FERNET_KEY not configured"
```bash
# Genera chiave Fernet
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Aggiungi a .env come FERNET_KEY=...
```

### Errore: "DATABASE_URL must use asyncpg driver"
Assicurati che DATABASE_URL inizi con `postgresql+asyncpg://`

### Errore: "Failed to connect to MySQL"
Verifica che MySQL sia accessibile e le credenziali siano corrette in .env

### Errore: "Redis not available"
Avvia Redis:
```bash
redis-server
# oppure con Docker
docker run -d -p 6379:6379 redis:7-alpine
```

## 🎯 Prossimi Passi

1. ✅ Testa connessioni base
2. ✅ Configura User ID e Token CardTrader
3. ✅ Testa endpoint `/info` per ottenere shared_secret
4. ✅ Avvia bulk sync iniziale
5. ✅ Verifica inventario popolato
6. ✅ Testa webhook (richiede configurazione su CardTrader)

## 📝 Note

- Il frontend salva User ID e Token in localStorage (solo locale)
- Per produzione, le credenziali andranno su AWS Secrets Manager
- Il webhook richiede un endpoint pubblico (usa ngrok per test locale)
