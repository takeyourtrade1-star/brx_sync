# 🚀 Implementation Roadmap
## BRX Sync Microservice - DevOps Improvements

**Created**: 2026-02-19  
**Status**: In Progress

---

## ✅ Completed (Priority 1)

### 1. MySQL Connection Pooling ✅
- **Status**: ✅ Completed
- **Changes**:
  - Replaced single global connection with thread-safe connection pool
  - Pool size: 5 (configurable via `MYSQL_POOL_SIZE`)
  - Max overflow: 5 (configurable via `MYSQL_POOL_MAX_OVERFLOW`)
  - Context manager for automatic connection return
  - Health check updated to use pool
- **Files Modified**:
  - `app/core/database.py`
  - `app/services/blueprint_mapper.py`
  - `app/core/health.py`
  - `app/core/config.py`

### 2. PostgreSQL Pool Size Increase ✅
- **Status**: ✅ Completed
- **Changes**:
  - Increased default pool size from 10 to 25
  - Increased max overflow from 20 to 50
- **Files Modified**:
  - `app/core/config.py`

### 3. Prometheus Metrics Export ✅
- **Status**: ✅ Completed
- **Changes**:
  - Added Prometheus client library
  - Created comprehensive metrics (HTTP, DB, Celery, CardTrader API, etc.)
  - Added `/metrics` endpoint
- **Files Created**:
  - `app/core/prometheus_metrics.py`
- **Files Modified**:
  - `app/main.py`
  - `requirements.txt`

### 4. CORS Security ✅
- **Status**: ✅ Completed
- **Changes**:
  - Added `ALLOWED_ORIGINS` configuration
  - Warning in production if wildcard is used
  - Restricted HTTP methods
- **Files Modified**:
  - `app/main.py`
  - `app/core/config.py`

---

## 🔄 In Progress

### 5. Connection Pool Metrics
- **Status**: Pending
- **Effort**: 2-3 hours
- **Description**: Add metrics for database connection pool usage (active, idle, overflow)

---

## 📋 Pending (Priority 2)

### 6. Read Replica Routing
- **Status**: Pending
- **Effort**: 4-6 hours
- **Description**: Implement automatic routing of SELECT queries to read replicas
- **Approach**:
  - Create separate engine for read replicas
  - Use SQLAlchemy routing based on query type
  - Add configuration for replica URLs

### 7. Distributed Tracing (OpenTelemetry)
- **Status**: Pending
- **Effort**: 6-8 hours
- **Description**: Add OpenTelemetry for distributed tracing
- **Dependencies**: `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`

### 8. Celery Dead Letter Queue (DLQ)
- **Status**: Pending
- **Effort**: 2-3 hours
- **Description**: Add DLQ for permanently failed tasks
- **Configuration**: Add DLQ queue in Celery config

### 9. Task Deduplication
- **Status**: Pending
- **Effort**: 3-4 hours
- **Description**: Implement idempotency keys for task deduplication
- **Approach**: Use Redis to track task IDs

### 10. API Rate Limiting Middleware
- **Status**: Pending
- **Effort**: 3-4 hours
- **Description**: Add rate limiting middleware for FastAPI endpoints
- **Approach**: Use slowapi or custom middleware with Redis

---

## 📋 Pending (Priority 3)

### 11. Redis Cluster Support
- **Status**: Pending
- **Effort**: 4-6 hours
- **Description**: Add support for Redis Cluster mode

### 12. Database Connection Pool Monitoring
- **Status**: Pending
- **Effort**: 2-3 hours
- **Description**: Add detailed metrics for connection pool health

### 13. Enhanced Error Categorization
- **Status**: Pending
- **Effort**: 2-3 hours
- **Description**: Improve error handling with transient vs permanent categorization

### 14. Bulkhead Pattern
- **Status**: Pending
- **Effort**: 4-6 hours
- **Description**: Implement resource isolation for different operation types

---

## 🧪 Testing Requirements

### Unit Tests
- [ ] MySQL connection pool tests
- [ ] Prometheus metrics tests
- [ ] CORS configuration tests

### Integration Tests
- [ ] Connection pool stress tests
- [ ] Metrics export tests
- [ ] Health check tests

### Load Tests
- [ ] Concurrent connection pool usage
- [ ] High-volume sync operations
- [ ] Rate limiting under load

---

## 📊 Monitoring & Alerts

### Recommended CloudWatch Alarms

1. **Database Connection Pool Exhaustion**
   - Metric: `db_connections_active / db_connections_max`
   - Threshold: > 0.8
   - Action: Scale up or alert

2. **High Error Rate**
   - Metric: `http_requests_total{status_code=~"5.."}`
   - Threshold: > 1% of total requests
   - Action: Alert on-call

3. **CardTrader API Rate Limiting**
   - Metric: `cardtrader_rate_limit_hits_total`
   - Threshold: > 10 in 5 minutes
   - Action: Review rate limit configuration

4. **Celery Queue Depth**
   - Metric: `celery_queue_depth`
   - Threshold: > 1000 tasks
   - Action: Scale workers

5. **Circuit Breaker Open**
   - Metric: `circuit_breaker_state`
   - Threshold: > 0 (OPEN state)
   - Action: Alert immediately

---

## 🚀 Deployment Checklist

Before deploying to production:

- [ ] Update `.env` with production values
- [ ] Set `ALLOWED_ORIGINS` to specific domains
- [ ] Configure Prometheus scraping endpoint
- [ ] Set up CloudWatch alarms
- [ ] Test connection pool under load
- [ ] Verify metrics are being exported
- [ ] Test health checks
- [ ] Review CORS configuration
- [ ] Test MySQL connection pool
- [ ] Verify PostgreSQL pool size is appropriate

---

## 📝 Notes

- All changes are backward compatible
- No breaking changes to existing APIs
- Metrics endpoint is optional (doesn't affect core functionality)
- Connection pool improvements are transparent to application code
