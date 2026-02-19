# 🚀 BRX Sync Microservice - Guida Completa Deployment

## 📋 Indice

1. [Struttura Database](#struttura-database)
2. [Funzionalità e Endpoint](#funzionalità-e-endpoint)
3. [Dockerizzazione](#dockerizzazione)
4. [Deployment](#deployment)
5. [Configurazione](#configurazione)
6. [Monitoraggio](#monitoraggio)

---

## 🗄️ Struttura Database

### PostgreSQL Schema

Il microservizio utilizza **PostgreSQL 16+** con le seguenti tabelle:

#### 1. **user_sync_settings**
Configurazione utente per sincronizzazione CardTrader.

```sql
CREATE TABLE user_sync_settings (
    user_id UUID PRIMARY KEY,
    cardtrader_token_encrypted TEXT NOT NULL,
    webhook_secret VARCHAR(255),
    sync_status sync_status_enum NOT NULL DEFAULT 'idle',
    last_sync_at TIMESTAMP WITH TIME ZONE,
    last_error TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_sync_settings_status ON user_sync_settings(sync_status);
```

**Campi:**
- `user_id`: UUID utente (PK)
- `cardtrader_token_encrypted`: Token CardTrader criptato con Fernet
- `webhook_secret`: Secret per validazione webhook HMAC
- `sync_status`: Enum ('idle', 'initial_sync', 'active', 'error')
- `last_sync_at`: Timestamp ultima sincronizzazione riuscita
- `last_error`: Ultimo errore se sync fallita

#### 2. **user_inventory_items**
Inventario carte sincronizzate da CardTrader.

```sql
CREATE TABLE user_inventory_items (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    blueprint_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    price_cents INTEGER NOT NULL,
    properties JSONB,
    external_stock_id VARCHAR(255),
    description TEXT,
    user_data_field TEXT,
    graded BOOLEAN,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, blueprint_id, external_stock_id)
);

CREATE INDEX idx_inventory_user_id ON user_inventory_items(user_id);
CREATE INDEX idx_inventory_blueprint_id ON user_inventory_items(blueprint_id);
CREATE INDEX idx_inventory_external_stock_id ON user_inventory_items(external_stock_id);
CREATE INDEX idx_inventory_updated_at ON user_inventory_items(updated_at);
CREATE INDEX idx_inventory_user_blueprint_external 
    ON user_inventory_items(user_id, blueprint_id, external_stock_id);
```

**Campi:**
- `id`: ID auto-incrementale (PK)
- `user_id`: UUID utente (FK → user_sync_settings)
- `blueprint_id`: ID blueprint CardTrader
- `quantity`: Quantità disponibile
- `price_cents`: Prezzo in centesimi
- `properties`: JSONB con proprietà (condition, mtg_foil, mtg_language, signed, altered, etc.)
- `external_stock_id`: ID prodotto CardTrader (per update/delete mirati)
- `description`: Descrizione prodotto
- `user_data_field`: Campo custom per uso interno
- `graded`: Se il prodotto è graded

**Indici:**
- Indice composito `(user_id, blueprint_id, external_stock_id)` per ottimizzare bulk sync
- Indici singoli su `user_id`, `blueprint_id`, `external_stock_id`, `updated_at`

#### 3. **sync_operations**
Log operazioni di sincronizzazione per idempotenza e audit.

```sql
CREATE TABLE sync_operations (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL,
    operation_id VARCHAR(255) NOT NULL UNIQUE,
    operation_type VARCHAR(50) NOT NULL,
    status VARCHAR(50) NOT NULL,
    operation_metadata JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_sync_ops_user_id ON sync_operations(user_id);
CREATE INDEX idx_sync_ops_operation_id ON sync_operations(operation_id);
CREATE INDEX idx_sync_ops_status ON sync_operations(status);
```

**Campi:**
- `id`: ID auto-incrementale (PK)
- `user_id`: UUID utente (FK → user_sync_settings)
- `operation_id`: UUID operazione (per idempotenza)
- `operation_type`: Tipo ('bulk_sync', 'update', 'webhook', 'periodic_sync')
- `status`: Status ('pending', 'completed', 'failed')
- `operation_metadata`: JSONB con metadati (progress, stats, etc.)
- `created_at`: Timestamp creazione
- `completed_at`: Timestamp completamento

### Enum Types

```sql
CREATE TYPE sync_status_enum AS ENUM ('idle', 'initial_sync', 'active', 'error');
```

### Estensioni Richieste

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

### Script SQL Completo

Il file `schema.sql` contiene lo schema completo. Per applicarlo:

```bash
psql -U postgres -d brx_sync_db -f schema.sql
```

---

## 🔧 Funzionalità e Endpoint

### Autenticazione

Tutti gli endpoint (eccetto health, metrics, webhook) richiedono **JWT RS256**:

```
Authorization: Bearer <access_token>
```

Il token deve essere emesso dal servizio di autenticazione e contenere:
- `sub`: user_id
- `type`: "access"
- `mfa_verified`: true
- `exp`: expiration timestamp

### Endpoint API

#### 1. **Sincronizzazione**

##### `POST /api/v1/sync/start/{user_id}`
Avvia sincronizzazione bulk iniziale.

**Autenticazione:** ✅ JWT  
**Parametri:**
- `user_id` (path): UUID utente
- `force` (query, optional): Se true, forza sync anche se già in corso

**Response:**
```json
{
  "status": "accepted",
  "task_id": "uuid",
  "user_id": "uuid",
  "message": "Bulk sync started"
}
```

##### `GET /api/v1/sync/status/{user_id}`
Ottiene status sincronizzazione utente.

**Autenticazione:** ✅ JWT  
**Response:**
```json
{
  "user_id": "uuid",
  "sync_status": "idle|initial_sync|active|error",
  "last_sync_at": "2024-01-01T00:00:00Z",
  "last_error": null
}
```

##### `GET /api/v1/sync/progress/{user_id}`
Ottiene progress sincronizzazione in tempo reale.

**Autenticazione:** ✅ JWT  
**Response:**
```json
{
  "user_id": "uuid",
  "operation_id": "uuid",
  "status": "pending|completed|failed",
  "progress_percent": 45,
  "total_chunks": 100,
  "processed_chunks": 45,
  "total_products": 250000,
  "processed": 112500,
  "created": 100000,
  "updated": 12500,
  "skipped": 0
}
```

##### `GET /api/v1/sync/task/{task_id}`
Ottiene status task Celery specifico.

**Autenticazione:** ✅ JWT (verifica ownership)  
**Response:**
```json
{
  "task_id": "uuid",
  "status": "PENDING|STARTED|SUCCESS|FAILURE|RETRY",
  "ready": false,
  "message": "Task is currently running"
}
```

##### `POST /api/v1/sync/sync-from-cardtrader/{user_id}`
Triggera sync manuale da CardTrader (bidirezionale).

**Autenticazione:** ✅ JWT  
**Parametri:**
- `user_id` (path): UUID utente
- `blueprint_id` (query, optional): ID blueprint specifico

**Response:**
```json
{
  "status": "accepted",
  "task_id": "uuid",
  "user_id": "uuid",
  "blueprint_id": null,
  "message": "Sync from CardTrader queued"
}
```

#### 2. **Inventario**

##### `GET /api/v1/sync/inventory/{user_id}`
Ottiene lista inventario utente.

**Autenticazione:** ✅ JWT  
**Parametri:**
- `user_id` (path): UUID utente
- `limit` (query, default: 100, max: 1000): Numero risultati
- `offset` (query, default: 0): Offset paginazione

**Response:**
```json
{
  "user_id": "uuid",
  "items": [
    {
      "id": 1,
      "blueprint_id": 123456,
      "quantity": 5,
      "price_cents": 1000,
      "properties": {
        "condition": "Near Mint",
        "mtg_foil": false,
        "mtg_language": "en",
        "signed": false,
        "altered": false
      },
      "external_stock_id": "789012",
      "description": "Card description",
      "user_data_field": "Warehouse A",
      "graded": false,
      "updated_at": "2024-01-01T00:00:00Z"
    }
  ],
  "total": 250
}
```

##### `PUT /api/v1/sync/inventory/{user_id}/item/{item_id}`
Aggiorna item inventario.

**Autenticazione:** ✅ JWT  
**Body:**
```json
{
  "quantity": 10,
  "price_cents": 1500,
  "description": "Updated description",
  "user_data_field": "Warehouse B",
  "graded": true,
  "properties": {
    "condition": "Mint",
    "mtg_foil": true,
    "mtg_language": "it",
    "signed": true,
    "altered": false
  }
}
```

**Response:**
```json
{
  "item_id": 1,
  "status": "updated",
  "task_id": "uuid",
  "cardtrader_sync_queued": true,
  "external_stock_id": "789012"
}
```

##### `DELETE /api/v1/sync/inventory/{user_id}/item/{item_id}`
Elimina item inventario.

**Autenticazione:** ✅ JWT  
**Response:**
```json
{
  "item_id": 1,
  "status": "deleted",
  "task_id": "uuid",
  "cardtrader_sync_queued": true
}
```

#### 3. **Acquisto**

##### `POST /api/v1/sync/purchase/{user_id}/item/{item_id}`
Simula acquisto carta (rimuove da inventario).

**Autenticazione:** ✅ JWT  
**Body:**
```json
{
  "purchaseQuantity": 2
}
```

**Response:**
```json
{
  "item_id": 1,
  "status": "purchased",
  "quantity_purchased": 2,
  "remaining_quantity": 3,
  "message": "Purchase successful"
}
```

**Errori:**
- `400`: Quantità insufficiente
- `404`: Item non trovato
- `409`: Item non disponibile su CardTrader

#### 4. **Webhook**

##### `POST /api/v1/sync/webhook/user/{user_id}`
Endpoint webhook CardTrader (per utente specifico).

**Autenticazione:** ❌ HMAC Signature (non JWT)  
**Headers:**
- `Signature`: HMAC-SHA256 signature

**Response:**
```json
{
  "status": "accepted",
  "webhook_id": "uuid",
  "user_id": "uuid",
  "processing_time_ms": 45.2
}
```

##### `GET /api/v1/sync/webhook-url/{user_id}`
Ottiene URL webhook da configurare su CardTrader.

**Autenticazione:** ✅ JWT  
**Response:**
```json
{
  "user_id": "uuid",
  "webhook_url": "https://api.example.com/api/v1/sync/webhook/user/{user_id}",
  "instructions": {
    "step_1": "Go to https://www.cardtrader.com/it/full_api_app",
    "step_2": "Copy the webhook URL below",
    "step_3": "Paste it in the 'Indirizzo del tuo endpoint webhook' field",
    "step_4": "Click 'Salva l'endpoint del Webhook'"
  },
  "webhook_secret_configured": true
}
```

#### 5. **Utility**

##### `POST /api/v1/sync/setup-test-user`
Setup utente test (solo per sviluppo locale).

**Autenticazione:** ❌ Pubblico (solo per test)  
**Body:**
```json
{
  "user_id": "uuid",
  "cardtrader_token": "token_plaintext"
}
```

##### `POST /api/v1/sync/migrate/composite-index`
Applica migration indice composito.

**Autenticazione:** ✅ JWT  
**Response:**
```json
{
  "status": "success",
  "message": "Composite index created successfully",
  "index_name": "idx_inventory_user_blueprint_external",
  "columns": ["user_id", "blueprint_id", "external_stock_id"]
}
```

##### `GET /api/v1/sync/debug-logs`
Ottiene log debug (solo per sviluppo).

**Autenticazione:** ✅ JWT

### Endpoint Pubblici

#### Health Checks

##### `GET /health/live`
Liveness probe (Kubernetes).

**Response:**
```json
{
  "status": "alive"
}
```

##### `GET /health/ready`
Readiness probe (verifica dipendenze).

**Response:**
```json
{
  "status": "healthy",
  "postgresql": {
    "status": "healthy",
    "message": "PostgreSQL connection successful"
  },
  "redis": {
    "status": "healthy",
    "message": "Redis connection successful"
  },
  "mysql": {
    "status": "healthy",
    "message": "MySQL connection successful"
  }
}
```

##### `GET /health`
Health check dettagliato.

##### `GET /metrics`
Prometheus metrics endpoint.

---

## 🐳 Dockerizzazione

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Run FastAPI app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Docker Compose (Sviluppo)

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:16-alpine
    container_name: brx-sync-postgres
    environment:
      POSTGRES_DB: brx_sync_db
      POSTGRES_USER: brx_sync_user
      POSTGRES_PASSWORD: brx_sync_pass
    ports:
      - "5433:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U brx_sync_user -d brx_sync_db"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - brx-sync-network

  redis:
    image: redis:7-alpine
    container_name: brx-sync-redis
    ports:
      - "6380:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - brx-sync-network

  brx-sync-api:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: brx-sync-api
    environment:
      DATABASE_URL: postgresql+asyncpg://brx_sync_user:brx_sync_pass@postgres:5432/brx_sync_db
      MYSQL_HOST: ${MYSQL_HOST}
      MYSQL_PORT: ${MYSQL_PORT:-3306}
      MYSQL_USER: ${MYSQL_USER}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD}
      MYSQL_DATABASE: ${MYSQL_DATABASE}
      REDIS_URL: redis://redis:6379/0
      FERNET_KEY: ${FERNET_KEY}
      JWT_PUBLIC_KEY: ${JWT_PUBLIC_KEY}
      DEBUG: "false"
      ENVIRONMENT: production
      AWS_SSM_ENABLED: "true"
      AWS_REGION: eu-south-1
    ports:
      - "8001:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - brx-sync-network
    restart: unless-stopped

  brx-sync-worker:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: brx-sync-worker
    command: celery -A app.tasks.celery_app worker --loglevel=info --queues=high-priority,bulk-sync,default --concurrency=4
    environment:
      DATABASE_URL: postgresql+asyncpg://brx_sync_user:brx_sync_pass@postgres:5432/brx_sync_db
      MYSQL_HOST: ${MYSQL_HOST}
      MYSQL_PORT: ${MYSQL_PORT:-3306}
      MYSQL_USER: ${MYSQL_USER}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD}
      MYSQL_DATABASE: ${MYSQL_DATABASE}
      REDIS_URL: redis://redis:6379/0
      FERNET_KEY: ${FERNET_KEY}
      JWT_PUBLIC_KEY: ${JWT_PUBLIC_KEY}
      DEBUG: "false"
      ENVIRONMENT: production
      AWS_SSM_ENABLED: "true"
      AWS_REGION: eu-south-1
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - brx-sync-network
    restart: unless-stopped

volumes:
  postgres_data:

networks:
  brx-sync-network:
    driver: bridge
```

### Build e Run

```bash
# Build immagine
docker build -t brx-sync:latest .

# Run con docker-compose
docker-compose up -d

# Logs
docker-compose logs -f brx-sync-api
docker-compose logs -f brx-sync-worker

# Stop
docker-compose down

# Stop e rimuovi volumi
docker-compose down -v
```

---

## 🚀 Deployment

### Prerequisiti

1. **PostgreSQL 16+** (RDS o self-hosted)
2. **Redis 7+** (ElastiCache o self-hosted)
3. **MySQL** (per blueprint mapping, read-only)
4. **AWS Account** (per SSM Parameter Store)
5. **Docker** o **Kubernetes**

### Variabili d'Ambiente

#### Richieste

```bash
# PostgreSQL
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname

# MySQL (read-only)
MYSQL_HOST=mysql.example.com
MYSQL_PORT=3306
MYSQL_USER=readonly_user
MYSQL_PASSWORD=password
MYSQL_DATABASE=cards_db

# Redis
REDIS_URL=redis://redis.example.com:6379/0

# Fernet Encryption Key (32-byte base64)
FERNET_KEY=base64_encoded_32_byte_key

# JWT Public Key (PEM format)
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
```

#### Opzionali

```bash
# AWS SSM (se abilitato)
AWS_SSM_ENABLED=true
AWS_REGION=eu-south-1
JWT_PUBLIC_KEY_SSM_PATH=/prod/ebartex/jwt_public_key
FERNET_KEY_SSM_PATH=/prod/ebartex/fernet_key

# Application
DEBUG=false
ENVIRONMENT=production
ALLOWED_ORIGINS=https://ebartex.it,https://www.ebartex.it

# Database Pool
DB_POOL_SIZE=50
DB_MAX_OVERFLOW=100
MYSQL_POOL_SIZE=20
MYSQL_POOL_MAX_OVERFLOW=20

# Rate Limiting
RATE_LIMIT_REQUESTS=200
RATE_LIMIT_WINDOW_SECONDS=10

# Celery
CELERY_BROKER_URL=redis://redis.example.com:6379/0
CELERY_RESULT_BACKEND=redis://redis.example.com:6379/0
```

### Setup Database

#### 1. Crea Database PostgreSQL

```sql
CREATE DATABASE brx_sync_db;
CREATE USER brx_sync_user WITH PASSWORD 'secure_password';
GRANT ALL PRIVILEGES ON DATABASE brx_sync_db TO brx_sync_user;
```

#### 2. Applica Schema

```bash
psql -U brx_sync_user -d brx_sync_db -f schema.sql
```

#### 3. Applica Migration Indice Composito

```bash
psql -U brx_sync_user -d brx_sync_db -f migrations/add_composite_index.sql
```

### Deployment su AWS ECS/Fargate

#### 1. Build e Push Immagine Docker

```bash
# Login ECR
aws ecr get-login-password --region eu-south-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.eu-south-1.amazonaws.com

# Build
docker build -t brx-sync:latest .

# Tag
docker tag brx-sync:latest <account-id>.dkr.ecr.eu-south-1.amazonaws.com/brx-sync:latest

# Push
docker push <account-id>.dkr.ecr.eu-south-1.amazonaws.com/brx-sync:latest
```

#### 2. Task Definition ECS

```json
{
  "family": "brx-sync-api",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "containerDefinitions": [
    {
      "name": "brx-sync-api",
      "image": "<account-id>.dkr.ecr.eu-south-1.amazonaws.com/brx-sync:latest",
      "portMappings": [
        {
          "containerPort": 8000,
          "protocol": "tcp"
        }
      ],
      "environment": [
        {
          "name": "ENVIRONMENT",
          "value": "production"
        },
        {
          "name": "AWS_SSM_ENABLED",
          "value": "true"
        }
      ],
      "secrets": [
        {
          "name": "DATABASE_URL",
          "valueFrom": "arn:aws:secretsmanager:eu-south-1:<account-id>:secret:brx-sync/database-url"
        },
        {
          "name": "FERNET_KEY",
          "valueFrom": "arn:aws:ssm:eu-south-1:<account-id>:parameter/prod/ebartex/fernet_key"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/brx-sync",
          "awslogs-region": "eu-south-1",
          "awslogs-stream-prefix": "api"
        }
      },
      "healthCheck": {
        "command": ["CMD-SHELL", "curl -f http://localhost:8000/health/live || exit 1"],
        "interval": 30,
        "timeout": 5,
        "retries": 3
      }
    }
  ]
}
```

#### 3. Service ECS

```json
{
  "serviceName": "brx-sync-api",
  "cluster": "production",
  "taskDefinition": "brx-sync-api",
  "desiredCount": 2,
  "launchType": "FARGATE",
  "networkConfiguration": {
    "awsvpcConfiguration": {
      "subnets": ["subnet-xxx", "subnet-yyy"],
      "securityGroups": ["sg-xxx"],
      "assignPublicIp": "ENABLED"
    }
  },
  "loadBalancer": {
    "targetGroupArn": "arn:aws:elasticloadbalancing:...",
    "containerName": "brx-sync-api",
    "containerPort": 8000
  },
  "healthCheckGracePeriodSeconds": 60
}
```

### Deployment su Kubernetes

#### 1. ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: brx-sync-config
data:
  ENVIRONMENT: "production"
  AWS_SSM_ENABLED: "true"
  AWS_REGION: "eu-south-1"
  DB_POOL_SIZE: "50"
  DB_MAX_OVERFLOW: "100"
```

#### 2. Secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: brx-sync-secrets
type: Opaque
stringData:
  DATABASE_URL: "postgresql+asyncpg://user:pass@host:5432/dbname"
  REDIS_URL: "redis://redis:6379/0"
  MYSQL_HOST: "mysql.example.com"
  MYSQL_USER: "readonly_user"
  MYSQL_PASSWORD: "password"
  MYSQL_DATABASE: "cards_db"
```

#### 3. Deployment API

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: brx-sync-api
spec:
  replicas: 2
  selector:
    matchLabels:
      app: brx-sync-api
  template:
    metadata:
      labels:
        app: brx-sync-api
    spec:
      containers:
      - name: brx-sync-api
        image: <registry>/brx-sync:latest
        ports:
        - containerPort: 8000
        envFrom:
        - configMapRef:
            name: brx-sync-config
        - secretRef:
            name: brx-sync-secrets
        env:
        - name: FERNET_KEY
          valueFrom:
            secretKeyRef:
              name: brx-sync-secrets
              key: fernet-key
        - name: JWT_PUBLIC_KEY
          valueFrom:
            secretKeyRef:
              name: brx-sync-secrets
              key: jwt-public-key
        livenessProbe:
          httpGet:
            path: /health/live
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health/ready
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 5
        resources:
          requests:
            memory: "1Gi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
```

#### 4. Deployment Worker

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: brx-sync-worker
spec:
  replicas: 3
  selector:
    matchLabels:
      app: brx-sync-worker
  template:
    metadata:
      labels:
        app: brx-sync-worker
    spec:
      containers:
      - name: brx-sync-worker
        image: <registry>/brx-sync:latest
        command: ["celery", "-A", "app.tasks.celery_app", "worker", "--loglevel=info", "--queues=high-priority,bulk-sync,default", "--concurrency=4"]
        envFrom:
        - configMapRef:
            name: brx-sync-config
        - secretRef:
            name: brx-sync-secrets
        resources:
          requests:
            memory: "1Gi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
```

#### 5. Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: brx-sync-api
spec:
  selector:
    app: brx-sync-api
  ports:
  - port: 80
    targetPort: 8000
  type: LoadBalancer
```

#### 6. Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: brx-sync-ingress
  annotations:
    kubernetes.io/ingress.class: nginx
    cert-manager.io/cluster-issuer: letsencrypt-prod
spec:
  tls:
  - hosts:
    - api.brx-sync.example.com
    secretName: brx-sync-tls
  rules:
  - host: api.brx-sync.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: brx-sync-api
            port:
              number: 80
```

---

## ⚙️ Configurazione

### AWS SSM Parameter Store

#### Chiavi da Configurare

1. **JWT Public Key**
   - Path: `/prod/ebartex/jwt_public_key`
   - Type: `String` (non SecureString, è pubblica)
   - Value: Chiave pubblica RSA in formato PEM

2. **Fernet Key**
   - Path: `/prod/ebartex/fernet_key`
   - Type: `SecureString`
   - Value: Chiave Fernet 32-byte in base64

#### Comandi AWS CLI

```bash
# JWT Public Key
aws ssm put-parameter \
  --name "/prod/ebartex/jwt_public_key" \
  --type "String" \
  --value "$(cat jwt_public_key.pem)" \
  --region eu-south-1

# Fernet Key
aws ssm put-parameter \
  --name "/prod/ebartex/fernet_key" \
  --type "SecureString" \
  --value "$(openssl rand -base64 32)" \
  --region eu-south-1
```

### Generazione Chiavi

#### Fernet Key

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

#### JWT Keys (se necessario)

Le chiavi JWT sono generate dal servizio di autenticazione. Il microservizio brx_sync usa solo la chiave pubblica.

---

## 📊 Monitoraggio

### Health Checks

- **Liveness**: `GET /health/live`
- **Readiness**: `GET /health/ready`
- **Detailed**: `GET /health`

### Metrics Prometheus

- **Endpoint**: `GET /metrics`
- **Formato**: Prometheus text format

### Logging

- **API Logs**: Stdout/Stderr (raccolti da CloudWatch/Kubernetes)
- **Worker Logs**: Stdout/Stderr
- **File Logs**: `logs/brx_sync.log` (solo sviluppo locale)

### Alerting Consigliato

1. **Health Check Failures**: Alert se `/health/ready` ritorna 503
2. **High Error Rate**: Alert se error rate > 5%
3. **Task Queue Backlog**: Alert se queue > 1000 task
4. **Database Connection Pool Exhausted**: Alert se pool usage > 90%
5. **Rate Limit Exceeded**: Alert se rate limit hit > 10/min

---

## 🔒 Sicurezza

### Autenticazione

- **JWT RS256**: Tutti gli endpoint (eccetto webhook) richiedono JWT
- **HMAC Signature**: Webhook endpoint usano HMAC-SHA256
- **User ID Verification**: Verifica match user_id token/URL

### Crittografia

- **Fernet**: Token CardTrader criptati a riposo
- **TLS**: Tutte le comunicazioni HTTPS

### Best Practices

1. **Secrets Management**: Usa AWS SSM o Kubernetes Secrets
2. **Network Security**: VPC, Security Groups, Network Policies
3. **Rate Limiting**: Implementato per-user (200 req/10s)
4. **Input Validation**: Pydantic models per validazione
5. **SQL Injection**: SQLAlchemy ORM previene injection
6. **CORS**: Configura `ALLOWED_ORIGINS` in produzione

---

## 📝 Checklist Deployment

- [ ] Database PostgreSQL creato e schema applicato
- [ ] Indice composito applicato
- [ ] Redis configurato e accessibile
- [ ] MySQL accessibile (read-only)
- [ ] AWS SSM Parameter Store configurato
- [ ] Fernet Key generata e salvata
- [ ] JWT Public Key configurata
- [ ] Immagine Docker buildata e pushata
- [ ] Variabili d'ambiente configurate
- [ ] Health checks funzionanti
- [ ] Logging configurato
- [ ] Monitoring/Alerting configurato
- [ ] Backup database configurato
- [ ] Disaster recovery plan documentato

---

## 🆘 Troubleshooting

### Database Connection Issues

```bash
# Test PostgreSQL
psql -U brx_sync_user -d brx_sync_db -h <host> -c "SELECT 1;"

# Test MySQL
mysql -h <host> -u <user> -p<password> <database> -e "SELECT 1;"
```

### Redis Connection Issues

```bash
# Test Redis
redis-cli -h <host> -p <port> ping
```

### Worker Not Processing Tasks

```bash
# Check Celery status
docker exec brx-sync-worker celery -A app.tasks.celery_app inspect active

# Check queues
docker exec brx-sync-worker celery -A app.tasks.celery_app inspect reserved
```

### JWT Authentication Issues

```bash
# Verify JWT Public Key format
openssl rsa -pubin -in jwt_public_key.pem -text -noout

# Test token (usando jwt.io o script Python)
python3 -c "import jwt; print(jwt.decode('<token>', open('jwt_public_key.pem').read(), algorithms=['RS256']))"
```

---

## 📚 Documentazione Aggiuntiva

- `AUTHENTICATION_IMPLEMENTATION.md`: Dettagli autenticazione
- `SCALABILITY_ANALYSIS.md`: Analisi scalabilità
- `WEBHOOKS_GUIDE.md`: Guida webhook
- `TESTING_GUIDE.md`: Guida testing

---

**Versione**: 1.0.0  
**Ultimo Aggiornamento**: 2024-01-01  
**Autore**: BRX Sync Team
