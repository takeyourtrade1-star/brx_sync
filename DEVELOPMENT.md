# BRX Sync - Development Guide

## Setup Development Environment

### Prerequisites
- Python 3.9+
- PostgreSQL 16+
- Redis 7+
- MySQL 8+ (for blueprint mapping)

### Installation

1. **Clone repository and navigate to project:**
   ```bash
   cd brx_sync
   ```

2. **Create virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```

4. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

5. **Run database migrations:**
   ```bash
   python run_migration.py
   ```

## Running Tests

### Run all tests:
```bash
pytest
```

### Run with coverage:
```bash
pytest --cov=app --cov-report=html
```

### Run specific test categories:
```bash
pytest -m unit          # Unit tests only
pytest -m integration   # Integration tests only
pytest -m "not slow"    # Exclude slow tests
```

### Run specific test file:
```bash
pytest tests/unit/test_services/test_rate_limiter.py
```

## Code Quality

### Format code:
```bash
black app tests
isort app tests
```

### Lint code:
```bash
flake8 app tests
ruff check app tests
```

### Type checking:
```bash
mypy app
```

### Run all quality checks:
```bash
black --check app tests
isort --check app tests
flake8 app tests
mypy app
pytest
```

## Running the Application

### Development server:
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Celery worker:
```bash
celery -A app.tasks.celery_app worker --loglevel=info -Q bulk-sync,high-priority,default
```

### Start all services (local):
```bash
./start_local.sh
```

## Project Structure

```
brx_sync/
├── app/
│   ├── api/              # API routes
│   │   └── v1/
│   │       ├── routes/   # Endpoint handlers
│   │       └── schemas.py # Pydantic models
│   ├── core/             # Core utilities
│   │   ├── config.py     # Configuration
│   │   ├── database.py   # Database connections
│   │   ├── exceptions.py # Exception hierarchy
│   │   ├── logging.py    # Structured logging
│   │   ├── health.py     # Health checks
│   │   ├── metrics.py    # Metrics collection
│   │   └── validators.py # Input validation
│   ├── models/           # SQLAlchemy models
│   ├── services/         # Business logic services
│   └── tasks/            # Celery tasks
├── tests/
│   ├── unit/             # Unit tests
│   ├── integration/      # Integration tests
│   └── conftest.py       # Test fixtures
├── migrations/           # Database migrations
└── static/              # Static files (frontend)
```

## Code Style

### Type Hints
- Always use type hints for function parameters and return types
- Use `Optional[T]` for nullable types (Python 3.9 compatible)
- Use `Dict[str, Any]` for flexible dictionaries

### Docstrings
- Use Google-style docstrings
- Include Args, Returns, and Raises sections
- Add examples where helpful

### Error Handling
- Use custom exceptions from `app.core.exceptions`
- Always include context (user_id, item_id, etc.) in exceptions
- Log errors with appropriate level and context

### Logging
- Use `get_logger(__name__)` from `app.core.logging`
- Include context (trace_id, user_id) in logs
- Use appropriate log levels (DEBUG, INFO, WARNING, ERROR)

## Testing Guidelines

### Unit Tests
- Test one function/method at a time
- Use mocks for external dependencies
- Aim for >80% code coverage
- Test both success and error cases

### Integration Tests
- Test complete workflows
- Use test database (separate from development)
- Clean up after tests

### Test Naming
- Test files: `test_<module_name>.py`
- Test functions: `test_<function_name>_<scenario>`
- Example: `test_rate_limiter_check_and_consume_success()`

## Git Workflow

1. Create feature branch: `git checkout -b feature/your-feature`
2. Make changes and commit: `git commit -m "Add feature X"`
3. Run tests and quality checks
4. Push and create pull request

## Debugging

### Enable debug logging:
```bash
export DEBUG=true
export LOG_LEVEL=DEBUG
```

### View structured logs:
```bash
tail -f logs/brx_sync.log | jq
```

### Debug Celery tasks:
```bash
celery -A app.tasks.celery_app worker --loglevel=debug
```

## Common Tasks

### Add new API endpoint:
1. Create route in `app/api/v1/routes/`
2. Add Pydantic schema in `app/api/v1/schemas.py`
3. Add exception handling
4. Write tests
5. Update API documentation

### Add new service:
1. Create service in `app/services/`
2. Add type hints and docstrings
3. Use structured logging
4. Write unit tests
5. Add to dependency injection if needed

### Add new Celery task:
1. Create task in `app/tasks/sync_tasks.py`
2. Configure queue in `app/tasks/celery_app.py`
3. Add error handling and retry logic
4. Write integration tests

## Troubleshooting

### Database connection issues:
- Check `DATABASE_URL` in `.env`
- Verify PostgreSQL is running
- Check connection pool settings

### Redis connection issues:
- Check `REDIS_URL` in `.env`
- Verify Redis is running
- Check network connectivity

### Celery task not executing:
- Verify Celery worker is running
- Check queue configuration
- Review Celery logs

### Type checking errors:
- Run `mypy app` to see all errors
- Fix type hints incrementally
- Use `# type: ignore` only when necessary with comment

## Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [SQLAlchemy 2.0 Documentation](https://docs.sqlalchemy.org/en/20/)
- [Celery Documentation](https://docs.celeryq.dev/)
- [Pydantic Documentation](https://docs.pydantic.dev/)
