# Analisi Scalabilità - BRX Sync Microservice

## Obiettivo
Verificare che il sistema sia in grado di gestire molti utenti simultanei senza problemi di performance, deadlock, o esaurimento risorse.

---

## ✅ PUNTI DI FORZA (Già Implementati)

### 1. Rate Limiting Per-User ✅
- **Isolamento**: Ogni utente ha il proprio bucket Redis (`rate_limit:{user_id}`)
- **Scalabilità**: Supporta migliaia di utenti senza interferenze
- **Algoritmo**: Token Bucket distribuito via Redis
- **Adattivo**: Si adatta automaticamente ai 429 di CardTrader

### 2. Connection Pooling PostgreSQL ✅
- **Pool Size**: 25 connessioni base
- **Max Overflow**: 50 connessioni aggiuntive
- **Totale**: Max 75 connessioni simultanee
- **Pre-ping**: Verifica connessioni morte
- **Recycle**: Rinnova connessioni ogni ora

### 3. MySQL Connection Pool ✅
- **Pool Size**: 5 connessioni base
- **Max Overflow**: 5 connessioni aggiuntive
- **Totale**: Max 10 connessioni simultanee
- **Thread-safe**: Usa Queue con lock

### 4. Isolamento Chunk Bulk Sync ✅
- Ogni chunk ha sessione DB isolata
- Evita race conditions e deadlock
- Parallelizzazione sicura

### 5. Deadlock Retry ✅
- Retry automatico con exponential backoff
- Max 3 tentativi
- Rileva errori PostgreSQL 40001 e 40P01

### 6. Transaction Timeout ✅
- Timeout configurabile (default 30s)
- Previene transazioni infinite
- Rollback automatico

### 7. Pattern Saga per Purchase ✅
- Chiamate API fuori transazione
- Lock DB minimo
- Compensazione automatica

---

## ⚠️ PROBLEMI POTENZIALI IDENTIFICATI

### 1. 🔴 CRITICO: Redis Sync Connection Pool Mancante

**Problema**:
```python
def get_redis_sync():
    return redis.from_url(...)  # Crea nuova connessione ogni volta!
```

**Impatto**:
- Ogni chiamata a rate limiter crea una nuova connessione Redis
- Con 1000 utenti simultanei → 1000+ connessioni Redis
- Redis ha limite default di 10000 connessioni, ma:
  - Ogni connessione consuma memoria
  - Overhead di gestione connessioni
  - Possibile esaurimento connessioni

**Soluzione**:
- Implementare connection pool per Redis sync
- Usare `redis.ConnectionPool` con max_connections
- Reutilizzare connessioni tra thread

**Priorità**: 🔴 CRITICA

---

### 2. 🟡 MEDIO: PostgreSQL Pool Size Potrebbe Essere Insufficiente

**Situazione Attuale**:
- Pool: 25 base + 50 overflow = 75 max connessioni
- Con 100 utenti simultanei che fanno bulk sync → 100 richieste
- Ogni bulk sync può usare 1-5 connessioni (chunk paralleli)

**Calcolo**:
- 100 utenti × 3 chunk paralleli = 300 richieste potenziali
- Pool max 75 → **COLLO DI BOTTIGLIA**

**Soluzione**:
- Aumentare `DB_POOL_SIZE` a 50-100 in produzione
- Aumentare `DB_MAX_OVERFLOW` a 100-200
- Monitorare metriche pool usage

**Priorità**: 🟡 MEDIA (dipende dal carico reale)

---

### 3. 🟡 MEDIO: MySQL Pool Size Limitato

**Situazione Attuale**:
- Pool: 5 base + 5 overflow = 10 max connessioni
- Ogni blueprint mapping query usa 1 connessione
- Con bulk sync paralleli → molti utenti cercano blueprint simultaneamente

**Calcolo**:
- 50 utenti × 3 chunk paralleli × 1 query blueprint = 150 richieste
- Pool max 10 → **COLLO DI BOTTIGLIA SEVERO**

**Soluzione**:
- Aumentare `MYSQL_POOL_SIZE` a 20-30
- Aumentare `MYSQL_POOL_MAX_OVERFLOW` a 20-30
- Considerare Redis cache più aggressiva per blueprint mapping

**Priorità**: 🟡 MEDIA (ma può diventare critica con molti sync)

---

### 4. 🟡 MEDIO: Isolated Engine Creation in Bulk Sync

**Problema**:
```python
async def get_isolated_db_session():
    engine = create_isolated_async_engine()  # Crea nuovo engine ogni volta!
    # ... usa engine ...
    await engine.dispose()  # Distrugge engine
```

**Impatto**:
- Ogni chunk bulk sync crea un nuovo engine SQLAlchemy
- Con 10 chunk paralleli → 10 engine creati/distrutti
- Overhead di creazione/distruzione engine
- Memory churn

**Soluzione**:
- Considerare pool di engine isolati (più complesso)
- O limitare parallelismo chunk (meno performante)
- Monitorare memory usage

**Priorità**: 🟡 MEDIA (overhead accettabile per ora)

---

### 5. 🟢 BASSO: Global Singleton Instances

**Problema**:
```python
_rate_limiter: Optional[RateLimiter] = None
_blueprint_mapper: Optional[BlueprintMapper] = None
```

**Impatto**:
- In ambiente multi-processo (Gunicorn/Uvicorn workers), ogni worker ha la propria istanza
- Non è un problema reale, ma da documentare

**Priorità**: 🟢 BASSA (funziona correttamente)

---

### 6. 🟡 MEDIO: Redis Async Client Singleton

**Problema**:
```python
_redis_client: Optional[Redis] = None  # Singleton globale
```

**Impatto**:
- Una sola connessione Redis async per processo
- Con molti worker FastAPI → molte connessioni (OK)
- Ma se un worker ha molti task async simultanei → potenziale bottleneck

**Soluzione**:
- Redis ha connection pooling interno
- Verificare che `aioredis.from_url` usi pooling
- Considerare connection pool esplicito se necessario

**Priorità**: 🟡 MEDIA (probabilmente OK, ma da monitorare)

---

## 📊 CALCOLI SCALABILITÀ

### Scenario 1: 100 Utenti Simultanei
- **Bulk Sync**: 100 utenti × 3 chunk = 300 richieste DB
- **PostgreSQL Pool**: 75 max → **⚠️ INSUFFICIENTE**
- **MySQL Pool**: 10 max → **🔴 CRITICO**
- **Redis Sync**: 100+ connessioni → **🔴 CRITICO**

### Scenario 2: 50 Utenti Simultanei
- **Bulk Sync**: 50 utenti × 3 chunk = 150 richieste DB
- **PostgreSQL Pool**: 75 max → **⚠️ AL LIMITE**
- **MySQL Pool**: 10 max → **🔴 CRITICO**
- **Redis Sync**: 50+ connessioni → **⚠️ PROBLEMATICO**

### Scenario 3: 10 Utenti Simultanei
- **Bulk Sync**: 10 utenti × 3 chunk = 30 richieste DB
- **PostgreSQL Pool**: 75 max → ✅ OK
- **MySQL Pool**: 10 max → **⚠️ AL LIMITE**
- **Redis Sync**: 10+ connessioni → ✅ OK (ma inefficiente)

---

## 🔧 RACCOMANDAZIONI PRIORITARIE

### Priorità 1: 🔴 CRITICA - Redis Sync Connection Pool

**File**: `app/core/redis_client.py`

**Implementazione**:
```python
_redis_sync_pool: Optional[redis.ConnectionPool] = None

def get_redis_sync():
    global _redis_sync_pool
    if _redis_sync_pool is None:
        _redis_sync_pool = redis.ConnectionPool.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,  # Limite connessioni pool
        )
    return redis.Redis(connection_pool=_redis_sync_pool)
```

**Benefici**:
- Reutilizzo connessioni
- Limite controllato
- Meno overhead

---

### Priorità 2: 🟡 ALTA - Aumentare Pool Size

**File**: `app/core/config.py`

**Modifiche**:
```python
DB_POOL_SIZE: int = Field(default=50, description="...")  # Da 25 a 50
DB_MAX_OVERFLOW: int = Field(default=100, description="...")  # Da 50 a 100

MYSQL_POOL_SIZE: int = Field(default=20, description="...")  # Da 5 a 20
MYSQL_POOL_MAX_OVERFLOW: int = Field(default=20, description="...")  # Da 5 a 20
```

**Benefici**:
- Supporta più utenti simultanei
- Riduce attesa su pool esaurito

---

### Priorità 3: 🟡 MEDIA - Monitoring e Alerting

**Implementare**:
- Metriche pool usage (PostgreSQL, MySQL, Redis)
- Alert quando pool > 80% utilizzato
- Dashboard Grafana per visualizzazione

**Benefici**:
- Identificare problemi prima che diventino critici
- Capacity planning

---

## 📈 STIMA CAPACITÀ ATTUALE

### Con Configurazione Attuale:
- **Utenti Simultanei**: ~10-15 (con bulk sync)
- **Utenti Simultanei**: ~50-100 (senza bulk sync, solo query)

### Con Ottimizzazioni Priorità 1+2:
- **Utenti Simultanei**: ~50-75 (con bulk sync)
- **Utenti Simultanei**: ~200-500 (senza bulk sync)

### Scalabilità Orizzontale:
- **Multi-istanza**: ✅ Supportata (stateless)
- **Load Balancer**: ✅ Funziona
- **Redis Shared**: ✅ Condiviso tra istanze
- **DB Shared**: ✅ PostgreSQL condiviso

---

## ✅ CONCLUSIONE

### Punti di Forza:
1. ✅ Architettura stateless
2. ✅ Rate limiting per-user isolato
3. ✅ Connection pooling implementato
4. ✅ Deadlock retry automatico
5. ✅ Pattern Saga per operazioni critiche

### Aree di Miglioramento:
1. 🔴 **CRITICO**: Redis sync connection pool
2. 🟡 **ALTO**: Aumentare pool size PostgreSQL/MySQL
3. 🟡 **MEDIO**: Monitoring pool usage

### Raccomandazione Finale:
**Il sistema è SOLIDO per 10-15 utenti simultanei con bulk sync.**
**Con le ottimizzazioni Priorità 1+2, può gestire 50-75 utenti simultanei.**

**Per scale maggiori (>100 utenti simultanei):**
- Implementare tutte le ottimizzazioni
- Aggiungere monitoring
- Considerare read replicas per MySQL (blueprint queries)
- Considerare read replicas per PostgreSQL (query inventario)
