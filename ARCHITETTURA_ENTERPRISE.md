# 🏗️ Architettura Enterprise BRX Sync - Analisi Infrastrutturale

## 📋 Executive Summary

Questo documento descrive l'architettura enterprise-grade per il microservizio BRX Sync, progettata per gestire **milioni di operazioni simultanee** su **migliaia di utenti** con **rate limiting distribuito avanzato**, **alta disponibilità** e **scalabilità orizzontale**.

---

## 🎯 Obiettivi Architetturali

1. **Scalabilità**: Supportare 10,000+ utenti simultanei con milioni di operazioni/giorno
2. **Affidabilità**: 99.99% uptime (meno di 53 minuti downtime/anno)
3. **Performance**: <100ms latency per webhook, <5s per operazioni sincrone
4. **Rate Limiting**: Gestione perfetta dei limiti CardTrader (200 req/10s per utente)
5. **Resilienza**: Auto-recovery da errori, circuit breaker, retry intelligenti
6. **Osservabilità**: Monitoring completo, tracing distribuito, alerting proattivo

---

## 🏛️ Architettura a Livelli

### Livello 1: Load Balancer & API Gateway

```
┌─────────────────────────────────────────────────────────┐
│  AWS Application Load Balancer (ALB)                     │
│  - Health checks su /health/ready                       │
│  - SSL/TLS termination                                   │
│  - Request routing basato su path                        │
│  - Connection draining                                   │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  AWS API Gateway (opzionale, per rate limiting globale) │
│  - Rate limiting per IP/API key                         │
│  - Request throttling                                   │
│  - API versioning                                        │
└─────────────────────────────────────────────────────────┘
```

**Configurazione ALB:**
- **Target Groups**: 3+ istanze FastAPI (multi-AZ)
- **Health Check**: `/health/ready` ogni 30s
- **Sticky Sessions**: Disabilitate (stateless)
- **Idle Timeout**: 60s
- **Connection Draining**: 300s

---

### Livello 2: Application Layer (FastAPI)

```
┌─────────────────────────────────────────────────────────┐
│  FastAPI Application (Container su ECS Fargate)          │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Instance 1  │  │  Instance 2  │  │  Instance 3  │  │
│  │  (AZ-a)      │  │  (AZ-b)      │  │  (AZ-c)      │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
│                                                          │
│  Features:                                               │
│  - Async request handling                                │
│  - Request validation (Pydantic)                        │
│  - Authentication/Authorization                         │
│  - Rate limiting pre-check (Redis)                      │
│  - Circuit breaker per CardTrader API                    │
│  - Request queuing (Celery)                             │
│  - Structured logging (JSON)                            │
└─────────────────────────────────────────────────────────┘
```

**Auto-Scaling Configuration:**
- **Min Instances**: 3 (una per AZ)
- **Max Instances**: 50
- **Target CPU**: 70%
- **Target Memory**: 80%
- **Scale Up Cooldown**: 60s
- **Scale Down Cooldown**: 300s

**Resource Allocation per Istanza:**
- **CPU**: 1 vCPU (1024 units)
- **Memory**: 2 GB
- **Concurrent Requests**: ~500 per istanza

---

### Livello 3: Task Queue Layer (Celery + Redis)

```
┌─────────────────────────────────────────────────────────┐
│  Celery Workers (ECS Fargate - Separate Service)        │
│                                                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │  High-Priority Workers (5-20 instances)          │  │
│  │  - Webhook processing                             │  │
│  │  - Real-time updates                              │  │
│  │  - Delete operations                              │  │
│  │  Queue: high-priority                             │  │
│  └──────────────────────────────────────────────────┘  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Bulk Sync Workers (2-10 instances)               │  │
│  │  - Initial bulk sync                              │  │
│  │  - Large batch operations                          │  │
│  │  Queue: bulk-sync                                  │  │
│  └──────────────────────────────────────────────────┘  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Default Workers (10-50 instances)                │  │
│  │  - Standard updates                                │  │
│  │  - Background jobs                                │  │
│  │  Queue: default                                    │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Worker Configuration:**

| Queue | Min Workers | Max Workers | CPU | Memory | Prefetch |
|-------|-------------|-------------|-----|--------|----------|
| high-priority | 5 | 20 | 1 vCPU | 2 GB | 1 |
| bulk-sync | 2 | 10 | 2 vCPU | 4 GB | 1 |
| default | 10 | 50 | 1 vCPU | 2 GB | 4 |

**Auto-Scaling Workers:**
- **Metric**: Queue length (tasks pending)
- **Scale Up**: Queue length > 100 per 30s
- **Scale Down**: Queue length < 10 per 5 minuti
- **Cooldown**: 60s (up), 300s (down)

---

### Livello 4: Rate Limiting Distribuito (Redis Cluster)

```
┌─────────────────────────────────────────────────────────┐
│  Redis Cluster (AWS ElastiCache)                         │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Node 1      │  │  Node 2      │  │  Node 3      │  │
│  │  (Primary)   │  │  (Replica)   │  │  (Replica)   │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
│                                                          │
│  Use Cases:                                              │
│  - Rate limiting buckets (per user_id)                  │
│  - Celery broker (separate Redis instance)              │
│  - Celery result backend                                 │
│  - Circuit breaker state                                 │
│  - Distributed locks                                     │
│  - Caching blueprint mappings                            │
└─────────────────────────────────────────────────────────┘
```

**Redis Configuration:**
- **Instance Type**: `cache.r6g.xlarge` (4 vCPU, 26 GB)
- **Cluster Mode**: Enabled (3 shards, 2 replicas per shard = 9 nodes total)
- **Persistence**: AOF (Append Only File) ogni secondo
- **Backup**: Automatico giornaliero, retention 7 giorni
- **Multi-AZ**: Enabled
- **Auto-Failover**: Enabled

**Rate Limiting Strategy:**
- **Algorithm**: Token Bucket distribuito (Redis Lua scripts)
- **Per User**: 200 tokens / 10 secondi
- **Burst**: Fino a 200 richieste simultanee
- **Refill Rate**: 20 tokens/secondo (200/10s)

---

### Livello 5: Database Layer

```
┌─────────────────────────────────────────────────────────┐
│  PostgreSQL (AWS RDS Multi-AZ)                          │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Primary     │  │  Standby 1   │  │  Standby 2   │  │
│  │  (AZ-a)      │  │  (AZ-b)      │  │  (AZ-c)      │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
│                                                          │
│  Instance: db.r6g.4xlarge (16 vCPU, 128 GB RAM)        │
│  - Connection Pool: 50-200 connections                  │
│  - Read Replicas: 2 (per query read-only)               │
│  - Automated Backups: 7 giorni retention                │
│  - Point-in-time Recovery: Enabled                      │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  MySQL (Hostinger - Read-Only)                          │
│  - Blueprint mapping cache                             │
│  - Connection pooling (10 connections)                  │
│  - Query timeout: 5s                                    │
└─────────────────────────────────────────────────────────┘
```

**Database Optimization:**
- **Connection Pooling**: SQLAlchemy pool (50-200 connections)
- **Read Replicas**: Routing automatico query SELECT a repliche
- **Indexing**: 
  - `user_inventory_items(user_id, blueprint_id, external_stock_id)`
  - `user_sync_settings(user_id)`
  - `sync_operations(user_id, status)`
- **Partitioning**: Per `user_id` (se > 1M utenti)
- **Vacuum**: Automatico con `autovacuum`

---

## 🔥 Rate Limiting Enterprise-Grade

### Strategia Multi-Layer

#### Layer 1: Pre-Request Check (Redis Token Bucket)

```python
# Implementazione avanzata con Lua script per atomicità
RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local max_tokens = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local tokens_to_consume = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'refill_time')
local tokens = tonumber(bucket[1]) or max_tokens
local refill_time = tonumber(bucket[2]) or (now + window_seconds)

-- Refill if needed
if now >= refill_time then
    tokens = max_tokens
    refill_time = now + window_seconds
end

-- Check if enough tokens
if tokens >= tokens_to_consume then
    tokens = tokens - tokens_to_consume
    redis.call('HMSET', key, 'tokens', tokens, 'refill_time', refill_time)
    redis.call('EXPIRE', key, window_seconds * 2)
    return {1, 0}  -- allowed, wait_seconds
else
    local wait_seconds = math.max(0, refill_time - now)
    return {0, wait_seconds}  -- not allowed, wait_seconds
end
"""
```

**Vantaggi:**
- **Atomicità**: Operazione atomica (no race conditions)
- **Performance**: Eseguito su Redis (sub-millisecondo)
- **Distribuito**: Funziona con cluster Redis
- **Preciso**: Token bucket matematicamente corretto

#### Layer 2: Adaptive Rate Limiting

```python
class AdaptiveRateLimiter:
    """
    Rate limiter che si adatta dinamicamente ai limiti CardTrader.
    Monitora i 429 e regola automaticamente i limiti.
    """
    
    def __init__(self):
        self.base_limit = 200  # requests per 10s
        self.adaptive_factor = 1.0  # moltiplicatore adattivo
        self.redis = get_redis_sync()
    
    def adjust_limit(self, user_id: str, received_429: bool):
        """Regola il limite basandosi sui 429 ricevuti."""
        key = f"rate_limit_stats:{user_id}"
        
        if received_429:
            # Riduci il limite del 10%
            self.adaptive_factor = max(0.5, self.adaptive_factor * 0.9)
            # Incrementa contatore 429
            self.redis.incr(f"{key}:429_count")
        else:
            # Aumenta gradualmente se nessun 429
            self.adaptive_factor = min(1.5, self.adaptive_factor * 1.01)
        
        # Salva fattore adattivo
        self.redis.setex(
            f"{key}:factor",
            3600,  # 1 ora
            self.adaptive_factor
        )
    
    def get_effective_limit(self, user_id: str) -> int:
        """Ottieni limite effettivo (base * adaptive_factor)."""
        key = f"rate_limit_stats:{user_id}"
        factor = float(self.redis.get(f"{key}:factor") or 1.0)
        return int(self.base_limit * factor)
```

#### Layer 3: Circuit Breaker Pattern

```python
class CardTraderCircuitBreaker:
    """
    Circuit Breaker per CardTrader API.
    Previene chiamate quando il servizio è down o sovraccarico.
    """
    
    def __init__(self):
        self.redis = get_redis_sync()
        self.failure_threshold = 5  # 5 errori consecutivi
        self.success_threshold = 2  # 2 successi per chiudere
        self.timeout = 60  # 60 secondi in stato OPEN
    
    def call(self, func, *args, **kwargs):
        """Esegui chiamata con circuit breaker."""
        state = self.get_state()
        
        if state == "OPEN":
            if self.should_attempt_reset():
                self.set_state("HALF_OPEN")
            else:
                raise CircuitBreakerOpenError("Circuit breaker is OPEN")
        
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except (RateLimitError, CardTraderAPIError) as e:
            self.record_failure()
            raise
    
    def get_state(self) -> str:
        """Ottieni stato corrente (CLOSED, OPEN, HALF_OPEN)."""
        state = self.redis.get("circuit_breaker:cardtrader:state") or "CLOSED"
        return state.decode() if isinstance(state, bytes) else state
    
    def record_failure(self):
        """Registra fallimento e apre circuit se necessario."""
        key = "circuit_breaker:cardtrader"
        failures = self.redis.incr(f"{key}:failures")
        self.redis.expire(f"{key}:failures", 60)
        
        if failures >= self.failure_threshold:
            self.set_state("OPEN")
            self.redis.setex(f"{key}:opened_at", self.timeout, time.time())
    
    def record_success(self):
        """Registra successo e chiude circuit se in HALF_OPEN."""
        key = "circuit_breaker:cardtrader"
        self.redis.delete(f"{key}:failures")
        
        if self.get_state() == "HALF_OPEN":
            successes = self.redis.incr(f"{key}:successes")
            if successes >= self.success_threshold:
                self.set_state("CLOSED")
                self.redis.delete(f"{key}:successes")
```

---

## 📊 Monitoring & Observability

### Metriche Chiave (CloudWatch / Prometheus)

#### Application Metrics
- **Request Rate**: Richieste/secondo per endpoint
- **Latency**: P50, P95, P99 per endpoint
- **Error Rate**: 4xx, 5xx, rate limit errors
- **Active Users**: Utenti con operazioni attive
- **Queue Depth**: Task pending per queue

#### Rate Limiting Metrics
- **Rate Limit Hits**: Numero di 429 ricevuti
- **Rate Limit Wait Time**: Tempo medio di attesa
- **Token Consumption**: Token consumati per user
- **Adaptive Factor**: Fattore adattivo medio

#### Database Metrics
- **Connection Pool Usage**: Connessioni attive/max
- **Query Latency**: P95 query time
- **Replication Lag**: Lag tra primary e repliche
- **Deadlocks**: Numero di deadlock/giorno

#### Infrastructure Metrics
- **CPU Usage**: Per istanza/worker
- **Memory Usage**: Per istanza/worker
- **Network I/O**: Bytes in/out
- **Redis Memory**: Utilizzo memoria Redis

### Logging Strutturato

```python
# Esempio log entry
{
    "timestamp": "2026-02-19T19:06:20.552Z",
    "level": "WARNING",
    "service": "brx-sync",
    "component": "cardtrader_client",
    "event": "rate_limit_429",
    "user_id": "db24fb13-ec73-49b8-932c-f0043dd47e86",
    "retry_after": 10.0,
    "attempt": 1,
    "max_retries": 3,
    "trace_id": "abc123...",
    "span_id": "def456..."
}
```

**Log Aggregation:**
- **CloudWatch Logs**: Log centralizzati con retention 30 giorni
- **Elasticsearch/OpenSearch**: Ricerca avanzata e analisi
- **S3 Archive**: Log compressi per compliance (7 anni)

### Distributed Tracing

```python
# OpenTelemetry integration
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

tracer_provider = TracerProvider()
trace.set_tracer_provider(tracer_provider)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="jaeger:4317"))
)
```

**Trace Points:**
- API request → FastAPI endpoint
- Rate limit check → Redis
- Database query → PostgreSQL
- CardTrader API call → External service
- Celery task execution → Worker

---

## 🚀 Strategie di Scalabilità

### 1. Horizontal Scaling (Auto-Scaling)

**FastAPI Instances:**
```yaml
AutoScalingPolicy:
  MinCapacity: 3
  MaxCapacity: 50
  TargetTrackingScaling:
    - Metric: CPUUtilization
      TargetValue: 70
    - Metric: RequestCountPerTarget
      TargetValue: 500
  ScaleInCooldown: 300
  ScaleOutCooldown: 60
```

**Celery Workers:**
```yaml
Queue-Based Scaling:
  high-priority:
    ScaleUpThreshold: 100 tasks pending
    ScaleDownThreshold: 10 tasks pending
    MinWorkers: 5
    MaxWorkers: 20
  
  bulk-sync:
    ScaleUpThreshold: 50 tasks pending
    ScaleDownThreshold: 5 tasks pending
    MinWorkers: 2
    MaxWorkers: 10
  
  default:
    ScaleUpThreshold: 200 tasks pending
    ScaleDownThreshold: 20 tasks pending
    MinWorkers: 10
    MaxWorkers: 50
```

### 2. Vertical Scaling (Database)

**RDS Auto-Scaling:**
- **CPU**: Auto-scale quando > 80% per 5 minuti
- **Memory**: Auto-scale quando > 90% per 5 minuti
- **Storage**: Auto-increase quando > 85% (max 64 TB)

### 3. Caching Strategy

```python
# Multi-level caching
CACHE_LAYERS = {
    "L1": "In-memory (per worker, 1 min TTL)",
    "L2": "Redis (distributed, 5 min TTL)",
    "L3": "Database (source of truth)"
}

# Cache keys
CACHE_KEYS = {
    "blueprint_mapping": "blueprint:{blueprint_id}:print_id",
    "user_settings": "user:{user_id}:settings",
    "rate_limit_state": "rate_limit:{user_id}",
    "circuit_breaker": "circuit:cardtrader:state"
}
```

**Cache Invalidation:**
- **TTL-based**: Scadenza automatica
- **Event-based**: Invalidation su update
- **Manual**: Admin API per clear cache

---

## 🔒 Sicurezza & Compliance

### 1. Encryption

- **At Rest**: 
  - RDS: Encryption enabled (AES-256)
  - S3: Server-side encryption (SSE-S3)
  - EBS: Encryption at rest
- **In Transit**: 
  - TLS 1.3 per tutte le comunicazioni
  - Certificate rotation automatico

### 2. Secrets Management

- **AWS Secrets Manager**: Token CardTrader, DB passwords
- **AWS SSM Parameter Store**: Fernet keys, config
- **Rotation**: Automatico ogni 30 giorni

### 3. Network Security

- **VPC**: Isolamento completo
- **Security Groups**: Least privilege
- **WAF**: Protezione da DDoS, SQL injection
- **Private Subnets**: Workers e DB in subnet private

---

## 📈 Performance Optimization

### 1. Database Query Optimization

```sql
-- Index ottimizzati
CREATE INDEX CONCURRENTLY idx_user_inventory_user_blueprint 
ON user_inventory_items(user_id, blueprint_id, external_stock_id);

CREATE INDEX CONCURRENTLY idx_sync_operations_user_status 
ON sync_operations(user_id, status) 
WHERE status IN ('pending', 'processing');

-- Materialized views per analytics
CREATE MATERIALIZED VIEW mv_user_inventory_stats AS
SELECT 
    user_id,
    COUNT(*) as total_items,
    SUM(quantity) as total_quantity,
    AVG(price_cents) as avg_price
FROM user_inventory_items
GROUP BY user_id;

-- Refresh ogni 5 minuti
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_user_inventory_stats;
```

### 2. Connection Pooling

```python
# PostgreSQL pool ottimizzato
pg_engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=50,           # Base pool
    max_overflow=150,        # Max connections = 200
    pool_pre_ping=True,      # Health check
    pool_recycle=3600,       # Recycle ogni ora
    pool_timeout=30,         # Timeout per ottenere connessione
    echo=settings.DEBUG,
)
```

### 3. Batch Processing

```python
# Batch updates invece di singole operazioni
async def batch_update_products(updates: List[Dict]) -> Dict:
    """
    Aggiorna multipli prodotti in una singola chiamata CardTrader.
    Riduce drasticamente il numero di richieste API.
    """
    # Raggruppa per user_id per rispettare rate limits
    grouped = defaultdict(list)
    for update in updates:
        grouped[update['user_id']].append(update)
    
    # Processa in batch da 100 prodotti
    results = []
    for user_id, user_updates in grouped.items():
        for batch in chunks(user_updates, 100):
            result = await client.bulk_update_products(batch)
            results.append(result)
    
    return {"batches": len(results), "total": len(updates)}
```

---

## 🎛️ Configuration Management

### Environment Variables (12-Factor App)

```bash
# Production
ENVIRONMENT=production
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://elasticache-cluster:6379/0
RATE_LIMIT_REQUESTS=200
RATE_LIMIT_WINDOW_SECONDS=10
CELERY_WORKER_CONCURRENCY=4
LOG_LEVEL=INFO

# Staging
ENVIRONMENT=staging
RATE_LIMIT_REQUESTS=100  # Limite più conservativo
LOG_LEVEL=DEBUG
```

### Feature Flags

```python
# Feature flags per rollout graduale
FEATURE_FLAGS = {
    "adaptive_rate_limiting": True,
    "circuit_breaker": True,
    "batch_updates": True,
    "new_webhook_handler": False,  # Gradual rollout
}
```

---

## 🔄 Disaster Recovery & High Availability

### Multi-AZ Deployment

- **FastAPI**: 3+ istanze in 3 AZ diverse
- **Celery Workers**: Distribuiti su 3 AZ
- **RDS**: Multi-AZ con automatic failover (<60s)
- **ElastiCache**: Multi-AZ con automatic failover (<30s)

### Backup Strategy

- **RDS**: 
  - Automated backups: 7 giorni retention
  - Manual snapshots: Settimanali, retention 30 giorni
  - Point-in-time recovery: Ultimi 7 giorni
- **S3**: 
  - Versioning abilitato
  - Lifecycle policy: Glacier dopo 90 giorni
- **Redis**: 
  - Snapshot automatici ogni 6 ore
  - Retention: 7 giorni

### Failover Procedures

1. **Database Failover**: Automatico (<60s)
2. **Redis Failover**: Automatico (<30s)
3. **Application Failover**: ALB health checks (<30s)
4. **Worker Failover**: Celery auto-reconnect

---

## 📊 Capacity Planning

### Stima Carico (10,000 utenti attivi)

**Assumptions:**
- 10,000 utenti simultanei
- 5 operazioni/utente/ora = 50,000 ops/ora
- Peak: 3x media = 150,000 ops/ora = 42 ops/secondo
- Webhook: 10,000 webhook/ora = 2.8 webhook/secondo

**Resource Requirements:**

| Component | Instances | CPU | Memory | Total CPU | Total Memory |
|-----------|-----------|-----|--------|-----------|--------------|
| FastAPI | 10 | 1 | 2 GB | 10 | 20 GB |
| High-Priority Workers | 10 | 1 | 2 GB | 10 | 20 GB |
| Bulk Workers | 5 | 2 | 4 GB | 10 | 20 GB |
| Default Workers | 20 | 1 | 2 GB | 20 | 40 GB |
| **Total** | **45** | - | - | **50** | **100 GB** |

**Database:**
- **Instance**: `db.r6g.4xlarge` (16 vCPU, 128 GB)
- **Read Replicas**: 2x `db.r6g.2xlarge` (8 vCPU, 64 GB)

**Redis:**
- **Cluster**: 3 shards x 3 nodes = 9 nodes
- **Instance**: `cache.r6g.xlarge` (4 vCPU, 26 GB per node)

---

## 🎯 Best Practices Implementation

### 1. Idempotency

```python
# Tutte le operazioni devono essere idempotenti
@celery_app.task(bind=True, idempotent=True)
def sync_update_product_to_cardtrader(self, operation_id: str, ...):
    # Check if already processed
    if self.is_already_processed(operation_id):
        return {"status": "already_processed"}
    
    # Process and mark as processed
    result = process_update(...)
    self.mark_as_processed(operation_id)
    return result
```

### 2. Graceful Shutdown

```python
# Signal handlers per shutdown graceful
import signal

def shutdown_handler(signum, frame):
    logger.info("Received shutdown signal, draining connections...")
    # Stop accepting new requests
    # Wait for current requests to complete (max 30s)
    # Close connections gracefully
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)
```

### 3. Health Checks

```python
@app.get("/health/ready")
async def health_ready():
    """Kubernetes/ECS readiness probe."""
    checks = {
        "database": await check_database(),
        "redis": await check_redis(),
        "celery": await check_celery_broker(),
    }
    
    if all(checks.values()):
        return {"status": "ready", "checks": checks}
    else:
        raise HTTPException(503, detail=checks)

@app.get("/health/live")
async def health_live():
    """Kubernetes/ECS liveness probe."""
    return {"status": "alive"}
```

---

## 🚦 Deployment Strategy

### Blue-Green Deployment

1. **Blue Environment**: Versione corrente (production)
2. **Green Environment**: Nuova versione (staging)
3. **Switch**: ALB target group switch (zero downtime)
4. **Rollback**: Switch back a Blue se problemi

### Canary Deployment

1. **10% Traffic**: Nuova versione
2. **Monitor**: Metriche per 15 minuti
3. **50% Traffic**: Se tutto OK
4. **100% Traffic**: Se ancora OK
5. **Rollback**: Se metriche degradate

---

## 📝 Checklist Implementazione

### Fase 1: Foundation (Settimana 1-2)
- [ ] Setup AWS VPC, subnets, security groups
- [ ] Deploy RDS Multi-AZ
- [ ] Deploy ElastiCache Redis cluster
- [ ] Setup ECS Fargate cluster
- [ ] Configure ALB con health checks

### Fase 2: Application (Settimana 3-4)
- [ ] Deploy FastAPI su ECS (3+ istanze)
- [ ] Deploy Celery workers (separate services)
- [ ] Configure auto-scaling policies
- [ ] Setup CloudWatch alarms

### Fase 3: Advanced Features (Settimana 5-6)
- [ ] Implement adaptive rate limiting
- [ ] Implement circuit breaker
- [ ] Setup distributed tracing
- [ ] Configure log aggregation

### Fase 4: Optimization (Settimana 7-8)
- [ ] Database query optimization
- [ ] Cache strategy implementation
- [ ] Load testing e tuning
- [ ] Documentation completa

---

## 💰 Cost Estimation (AWS)

### Monthly Costs (10,000 utenti)

| Service | Configuration | Monthly Cost |
|---------|--------------|--------------|
| ECS Fargate (FastAPI) | 10 instances x 1 vCPU x 2 GB | $150 |
| ECS Fargate (Workers) | 35 instances x 1-2 vCPU x 2-4 GB | $400 |
| RDS PostgreSQL | db.r6g.4xlarge Multi-AZ | $1,200 |
| RDS Read Replicas | 2x db.r6g.2xlarge | $600 |
| ElastiCache Redis | 9x cache.r6g.xlarge | $1,800 |
| ALB | Application Load Balancer | $25 |
| CloudWatch | Logs + Metrics | $100 |
| **Total** | | **~$4,275/mese** |

**Scaling to 100,000 users**: ~$40,000/mese (lineare scaling)

---

## 🎓 Conclusioni

Questa architettura enterprise fornisce:

✅ **Scalabilità**: Da 1 a 100,000+ utenti  
✅ **Affidabilità**: 99.99% uptime  
✅ **Performance**: <100ms latency  
✅ **Rate Limiting**: Perfetto e distribuito  
✅ **Resilienza**: Auto-recovery e circuit breakers  
✅ **Osservabilità**: Monitoring completo  

**Prossimi Passi:**
1. Implementare adaptive rate limiting
2. Aggiungere circuit breaker
3. Setup monitoring avanzato
4. Load testing con scenario realistici
5. Documentazione operativa per team
