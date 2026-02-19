# BRX Sync Microservice

Enterprise-grade microservice for synchronizing millions of cards between Ebartex and CardTrader V2 API.

## Features

- **Distributed Rate Limiting**: Token Bucket algorithm in Redis (200 req/10s per user)
- **Initial Bulk Sync**: Efficient CSV/JSON export processing in 5000-item chunks
- **Real-time Webhooks**: Sub-100ms webhook processing for order notifications
- **MySQL Integration**: Blueprint ID mapping from CardTrader to Ebartex print IDs
- **Security**: Fernet encryption for CardTrader tokens at rest
- **Scalability**: Celery task queue with priority queues

## Technology Stack

- **Backend**: FastAPI (async)
- **Task Queue**: Celery + Redis
- **Database**: PostgreSQL 16 (SQLAlchemy 2.0) + MySQL (pymysql)
- **Security**: Fernet encryption
- **Rate Limiting**: Redis Token Bucket

## Quick Start

### Prerequisites

- Python 3.12+
- PostgreSQL 16
- MySQL (for blueprint mapping)
- Redis 7+

### Local Development

1. **Setup environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

3. **Generate Fernet key:**
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   # Add to .env as FERNET_KEY
   ```

4. **Run database migrations:**
   ```bash
   alembic upgrade head
   ```

5. **Start Redis and PostgreSQL** (or use Docker Compose)

6. **Start FastAPI:**
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

7. **Start Celery worker** (in another terminal):
   ```bash
   celery -A app.tasks.celery_app worker --loglevel=info --queues=high-priority,bulk-sync,default
   ```

### Docker Compose

```bash
docker-compose up -d
```

## API Endpoints

- `POST /api/v1/sync/start/{user_id}`: Start initial bulk sync
- `GET /api/v1/sync/status/{user_id}`: Get sync status
- `POST /api/v1/sync/webhook/{webhook_id}`: CardTrader webhook endpoint
- `GET /api/v1/sync/inventory/{user_id}`: Get user inventory

## Architecture

See the plan file for detailed architecture documentation.
