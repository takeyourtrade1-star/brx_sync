ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}
ARG PYTHON_BASE_IMAGE

RUN python -c "import re,sys; assert re.fullmatch(r'[^\\s@]+@sha256:[0-9a-f]{64}', sys.argv[1]), 'PYTHON_BASE_IMAGE must use @sha256'" "$PYTHON_BASE_IMAGE"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# psql is required only by the explicit one-shot migration commands in deploy.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --upgrade pip==26.2 \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 app/ ./app/
COPY --chown=10001:10001 migrations/ ./migrations/
COPY --chown=10001:10001 alembic.ini .

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--no-proxy-headers", "--limit-concurrency", "128", "--limit-max-requests", "10000", "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "30"]
