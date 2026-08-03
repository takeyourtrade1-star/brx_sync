"""
Configuration settings for BRX Sync Microservice.
12-Factor config: no hardcoded secrets, fail fast on missing critical env.
"""

import ipaddress
import json
import re
import ssl
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

import boto3
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_JWT_LEGACY_ROLLOUT_ACK = "I_ACKNOWLEDGE_TEMPORARY_LEGACY_JWT_ACCEPTANCE"
_JWT_LEGACY_ROLLOUT_MAX_WINDOW = timedelta(hours=2)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    # App (non-sensitive, safe defaults)
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "BRX Sync"
    APP_NAME: str = "brx-sync"
    APP_VERSION: str = "1.0.0"
    SERVICE_ROLE: str = Field(default="api", pattern=r"^(api|worker|migration)$")
    DEBUG: bool = Field(default=False, description="Debug mode")
    ENVIRONMENT: Literal["development", "test", "staging", "production"] = Field(
        default="production",
        description="Validated deployment tier; staging and production are hardened",
    )
    ENABLE_TEST_ENDPOINTS: bool = Field(
        default=False,
        description=("Expose local-only test helpers. Ignored outside development/test."),
    )
    ALLOWED_ORIGINS: str = Field(
        default="*",
        description="Comma-separated list of allowed CORS origins (use '*' for all in dev only)",
    )
    PUBLIC_BASE_URL: str = Field(
        default="https://sync.ebartex.com",
        description="Canonical public origin used to build CardTrader webhook URLs",
    )
    TRUSTED_HOSTS: str = Field(
        default="sync.ebartex.com,brx-sync-api,localhost,127.0.0.1,testserver",
        description="Comma-separated Host header allowlist",
    )

    # PostgreSQL — required, no defaults (fail fast)
    DATABASE_URL: str = Field(
        ...,
        repr=False,
        description="PostgreSQL connection string (asyncpg format)",
        examples=["postgresql+asyncpg://user:pass@host:5432/dbname"],
    )
    DB_POOL_SIZE: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Bounded per-process asynchronous PostgreSQL pool size",
    )
    DB_MAX_OVERFLOW: int = Field(
        default=5,
        ge=0,
        le=20,
        description="Bounded per-process asynchronous PostgreSQL overflow",
    )
    DB_SYNC_POOL_SIZE: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Per-process synchronous PostgreSQL pool used by Celery",
    )
    DB_SYNC_MAX_OVERFLOW: int = Field(
        default=1,
        ge=0,
        le=5,
        description="Per-process synchronous PostgreSQL overflow used by Celery",
    )
    DB_ISOLATED_POOL_SIZE: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Pool size for short-lived event-loop isolated engines",
    )
    DB_ISOLATED_MAX_OVERFLOW: int = Field(
        default=1,
        ge=0,
        le=5,
        description="Overflow for short-lived event-loop isolated engines",
    )
    DB_TRANSACTION_TIMEOUT: int = Field(
        default=30,
        ge=1,
        le=120,
        description="Database transaction timeout in seconds",
    )
    DATABASE_SSL_CA_FILE: Optional[str] = Field(
        default=None,
        description="Optional CA bundle for verified PostgreSQL TLS",
    )

    # MySQL Connection Pool
    MYSQL_POOL_SIZE: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Bounded per-process MySQL pool size",
    )
    MYSQL_POOL_MAX_OVERFLOW: int = Field(
        default=5,
        ge=0,
        le=20,
        description="Bounded per-process MySQL overflow",
    )

    # MySQL — required for blueprint mapping (read-only)
    MYSQL_HOST: str = Field(..., description="MySQL host")
    MYSQL_PORT: int = Field(default=3306, description="MySQL port")
    MYSQL_USER: str = Field(..., description="MySQL user")
    MYSQL_PASSWORD: SecretStr = Field(..., description="MySQL password")
    MYSQL_DATABASE: str = Field(..., description="MySQL database name")
    MYSQL_SSL_CA_FILE: Optional[str] = Field(
        default=None,
        description="Optional CA bundle for verified MySQL TLS",
    )

    # Redis — required for rate limiting and Celery
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        repr=False,
        description="Redis connection URL for rate limiting and Celery broker",
    )
    REDIS_SSL_CA_FILE: Optional[str] = Field(
        default=None,
        description="Optional CA bundle for external rediss:// endpoints",
    )

    # Fernet Encryption — required for token encryption
    FERNET_KEY_SSM_PATH: Optional[str] = Field(
        default="/prod/ebartex/fernet_key",
        description="SSM path for Fernet encryption key (32-byte base64)",
    )
    FERNET_KEY: Optional[str] = Field(
        default=None,
        repr=False,
        description="Primary Fernet key (base64) - fallback to env",
    )
    FERNET_PREVIOUS_KEYS: Optional[str] = Field(
        default=None,
        repr=False,
        description="Comma-separated previous Fernet keys accepted during rotation",
    )

    # JWT Authentication — required for RS256 token verification
    JWT_PUBLIC_KEY_SSM_PATH: Optional[str] = Field(
        default="/prod/ebartex/jwt_public_key",
        description="SSM path for JWT public key (PEM format)",
    )
    JWT_PUBLIC_KEY: Optional[str] = Field(
        default=None,
        description="JWT public key (PEM format) - fallback to env",
    )
    JWT_ALGORITHM: str = Field(
        default="RS256",
        description="JWT signing algorithm (must match Auth Service)",
    )
    JWT_ISSUER: str = Field(default="ebartex-auth", min_length=1, max_length=255)
    JWT_AUDIENCE: str = Field(default="ebartex-services", min_length=1, max_length=255)
    JWT_REQUIRE_ISSUER_AUDIENCE: bool | None = Field(
        default=None,
        description="Defaults to strict in production; development/test may remain permissive",
    )
    JWT_REQUIRE_JTI: bool | None = Field(
        default=None,
        description="Defaults to strict in production; development/test may remain permissive",
    )
    JWT_LEGACY_ROLLOUT_ACK: str = Field(default="", repr=False, max_length=128)
    JWT_LEGACY_ROLLOUT_EXPIRES_AT: str = Field(default="", max_length=64)
    JWT_LEEWAY_SECONDS: int = Field(default=30, ge=0, le=120)
    JWT_MAX_TOKEN_BYTES: int = Field(default=8192, ge=1024, le=16384)
    JWT_MAX_ACCESS_TOKEN_SECONDS: int = Field(default=3660, ge=300, le=86400)
    JWT_VERIFY_MAX_CONCURRENCY: int = Field(default=4, ge=1, le=16)
    JWT_VERIFY_QUEUE_TIMEOUT_SECONDS: float = Field(
        default=0.05, ge=0.005, le=1.0
    )

    # Internal service-to-service API. No insecure default: internal routes
    # fail closed when the token is absent.
    INTERNAL_API_TOKEN: Optional[SecretStr] = Field(
        default=None,
        description="Legacy shared token accepted only in development/test",
    )
    INTERNAL_CALLER_TOKENS: Optional[SecretStr] = Field(
        default=None,
        description=(
            "JSON caller map: {caller: {token: ..., scopes: [...]}}; "
            "when set, the legacy shared token is disabled"
        ),
    )
    INTERNAL_API_TOKEN_SCOPES: str = Field(
        default="inventory:write,metrics:read",
        description="Legacy shared-token scopes during caller-map migration",
    )
    INTERNAL_API_ALLOWED_CIDRS: str = Field(
        default="127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
        description="Direct peer networks allowed to call private endpoints",
    )
    INTERNAL_API_RATE_LIMIT_PER_MINUTE: int = Field(
        default=300,
        ge=10,
        le=5000,
        description="Aggregate per-peer limit for internal inventory mutations.",
    )

    # AWS Integration (optional)
    AWS_REGION: str = Field(default="eu-south-1", description="AWS region for SSM")
    AWS_SSM_ENABLED: bool = Field(default=True, description="Enable AWS SSM Parameter Store")
    AWS_SSM_PREFIX: str = Field(default="/prod/ebartex", description="SSM parameter prefix")

    # CardTrader API
    CARDTRADER_API_BASE_URL: str = Field(
        default="https://api.cardtrader.com/api/v2",
        description="CardTrader V2 API base URL",
    )
    WEBHOOK_MAX_BODY_BYTES: int = Field(
        default=1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024,
        description="Maximum accepted CardTrader webhook request body.",
    )
    WEBHOOK_RATE_LIMIT_PER_MINUTE: int = Field(default=120, ge=10, le=2000)
    WEBHOOK_PEER_RATE_LIMIT_PER_MINUTE: int = Field(
        default=5000,
        ge=20,
        le=10_000,
        description="Aggregate webhook limit per direct network peer",
    )
    REQUEST_MAX_BODY_BYTES: int = Field(
        default=2 * 1024 * 1024,
        ge=16 * 1024,
        le=8 * 1024 * 1024,
        description="Global request body limit before FastAPI parsing",
    )
    REQUEST_MAX_BODY_MESSAGES: int = Field(
        default=1024,
        ge=16,
        le=4096,
        description="Maximum ASGI request fragments, including empty fragments",
    )
    CARDTRADER_MAX_RESPONSE_BYTES: int = Field(
        default=64 * 1024 * 1024,
        ge=1024 * 1024,
        le=256 * 1024 * 1024,
        description="Maximum buffered CardTrader response size",
    )
    CARDTRADER_WRITES_ENABLED: bool = Field(
        default=False,
        description="Global emergency switch for all CardTrader mutations",
    )
    CARDTRADER_JOB_POLL_TIMEOUT_SECONDS: int = Field(default=180, ge=5, le=600)
    CARDTRADER_JOB_POLL_INTERVAL_SECONDS: float = Field(default=1.1, ge=1.0, le=10.0)
    CARDTRADER_MAX_RETRY_AFTER_SECONDS: float = Field(default=60.0, ge=1.0, le=300.0)
    TRADE_CARDTRADER_MUTATION_TIMEOUT_SECONDS: float = Field(
        default=15.0,
        ge=1.0,
        le=30.0,
        description=(
            "Bound for synchronous trade stock mutations. Unknown outcomes are "
            "kept reserved and resolved by the recovery task."
        ),
    )

    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = Field(default=200, description="Rate limit requests per window")
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=10, description="Rate limit window in seconds")

    # Celery
    CELERY_BROKER_URL: Optional[str] = Field(
        default=None,
        repr=False,
        description="Celery broker URL (defaults to REDIS_URL)",
    )
    CELERY_RESULT_BACKEND: Optional[str] = Field(
        default=None,
        repr=False,
        description="Celery result backend (defaults to REDIS_URL)",
    )

    # Scalability: disable sync file logging in production (many workers = contention on one file)
    SYNC_LOG_TO_FILE: bool = Field(
        default=False,
        description="If False, skip _log_to_file() in sync tasks (use logger only). Set to False in production with many Celery workers.",
    )

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        parsed = urlsplit(v)
        if (
            parsed.scheme != "postgresql+asyncpg"
            or not parsed.hostname
            or parsed.username is None
            or parsed.path in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DATABASE_URL must use asyncpg driver: postgresql+asyncpg://...")
        return v

    @field_validator("JWT_ALGORITHM")
    @classmethod
    def validate_jwt_algorithm(cls, value: str) -> str:
        if value != "RS256":
            raise ValueError("JWT_ALGORITHM must be RS256")
        return value

    @field_validator("CARDTRADER_API_BASE_URL")
    @classmethod
    def validate_cardtrader_base_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.cardtrader.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/api/v2"
        ):
            raise ValueError(
                "CARDTRADER_API_BASE_URL must be https://api.cardtrader.com/api/v2"
            )
        return value.rstrip("/")

    @field_validator("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND")
    @classmethod
    def validate_redis_network_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        raw = value.strip()
        parsed = urlsplit(raw)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Redis URL contains an invalid port") from exc
        database = parsed.path.removeprefix("/")
        if (
            parsed.scheme not in {"redis", "rediss"}
            or not parsed.hostname
            or (port is not None and not 1 <= port <= 65535)
            or parsed.query
            or parsed.fragment
            or (database and (not database.isdigit() or int(database) > 15))
        ):
            raise ValueError("Redis URL must be a valid redis:// or rediss:// URL")
        return raw

    @field_validator("PUBLIC_BASE_URL")
    @classmethod
    def validate_public_base_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("PUBLIC_BASE_URL must be an origin without path or credentials")
        return value.rstrip("/")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.AWS_SSM_ENABLED:
            self._load_secrets_from_ssm()
        self._validate_runtime_security()

    def _validate_runtime_security(self) -> None:
        """Validate secrets after optional SSM hydration."""
        from cryptography.fernet import Fernet

        secure_environment = self.ENVIRONMENT in {"staging", "production"}
        if self.JWT_REQUIRE_ISSUER_AUDIENCE is None:
            self.JWT_REQUIRE_ISSUER_AUDIENCE = secure_environment
        if self.JWT_REQUIRE_JTI is None:
            self.JWT_REQUIRE_JTI = secure_environment
        if secure_environment:
            self._check_legacy_jwt_rollout(
                strict=bool(
                    self.JWT_REQUIRE_ISSUER_AUDIENCE and self.JWT_REQUIRE_JTI
                )
            )
        if secure_environment and self.DEBUG:
            raise ValueError("DEBUG must be false in staging/production")
        if secure_environment and self.ENABLE_TEST_ENDPOINTS:
            raise ValueError("ENABLE_TEST_ENDPOINTS must be false in staging/production")
        trusted_hosts = [host.strip() for host in self.TRUSTED_HOSTS.split(",")]
        if not trusted_hosts or any(not host for host in trusted_hosts):
            raise ValueError("TRUSTED_HOSTS must contain only non-empty hosts")
        for host in trusted_hosts:
            if host == "*":
                if secure_environment:
                    raise ValueError(
                        "Wildcard TRUSTED_HOSTS is forbidden in staging/production"
                    )
                continue
            candidate = host.removeprefix("*.")
            if (
                len(host) > 253
                or not candidate
                or "://" in host
                or "/" in host
                or any(character.isspace() for character in host)
                or not re.fullmatch(r"[A-Za-z0-9.-]+", candidate)
            ):
                raise ValueError("Invalid TRUSTED_HOSTS entry")
        if secure_environment and not self.PUBLIC_BASE_URL.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must use HTTPS in staging/production")
        redis_urls = {
            "REDIS_URL": self.REDIS_URL,
            "CELERY_BROKER_URL": self.celery_broker_url,
            "CELERY_RESULT_BACKEND": self.celery_result_backend,
        }
        for label, redis_url in redis_urls.items():
            parsed_redis = urlsplit(redis_url)
            if secure_environment and parsed_redis.scheme == "redis" and (
                parsed_redis.hostname != "brx-sync-redis"
                or (parsed_redis.port or 6379) != 6379
            ):
                raise ValueError(
                    f"{label} plain Redis is allowed only on the isolated compose service"
                )
        origins = [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]
        if not origins:
            raise ValueError("ALLOWED_ORIGINS must contain at least one origin")
        for origin in origins:
            parsed_origin = urlsplit(origin)
            if (
                origin == "*"
                or parsed_origin.scheme
                not in ({"https"} if secure_environment else {"http", "https"})
                or not parsed_origin.hostname
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                if not (origin == "*" and not secure_environment):
                    raise ValueError("ALLOWED_ORIGINS contains an invalid origin")
        if self.REQUEST_MAX_BODY_BYTES < self.WEBHOOK_MAX_BODY_BYTES:
            raise ValueError("REQUEST_MAX_BODY_BYTES must cover WEBHOOK_MAX_BODY_BYTES")
        if secure_environment:
            pool_totals = {
                "DB_POOL_SIZE + DB_MAX_OVERFLOW": (
                    self.DB_POOL_SIZE + self.DB_MAX_OVERFLOW
                ),
                "DB_SYNC_POOL_SIZE + DB_SYNC_MAX_OVERFLOW": (
                    self.DB_SYNC_POOL_SIZE + self.DB_SYNC_MAX_OVERFLOW
                ),
                "DB_ISOLATED_POOL_SIZE + DB_ISOLATED_MAX_OVERFLOW": (
                    self.DB_ISOLATED_POOL_SIZE + self.DB_ISOLATED_MAX_OVERFLOW
                ),
                "MYSQL_POOL_SIZE + MYSQL_POOL_MAX_OVERFLOW": (
                    self.MYSQL_POOL_SIZE + self.MYSQL_POOL_MAX_OVERFLOW
                ),
            }
            if any(total > 10 for total in pool_totals.values()):
                raise ValueError(
                    "Per-process database pool capacity must not exceed 10 in "
                    "staging/production"
                )
        for ca_file in (
            self.DATABASE_SSL_CA_FILE,
            self.MYSQL_SSL_CA_FILE,
            self.REDIS_SSL_CA_FILE,
        ):
            if ca_file and not Path(ca_file).is_file():
                raise ValueError("Configured database CA bundle is not readable")
        if secure_environment:
            database_user = urlsplit(self.DATABASE_URL).username or ""
            if database_user.casefold() in {
                "postgres",
                "admin",
                "root",
                "brx_bd_admin",
            }:
                raise ValueError("Production PostgreSQL must use a dedicated least-privilege role")
            if self.MYSQL_USER.casefold() in {"root", "admin", "brx_bd_admin"}:
                raise ValueError("Production MySQL must use a dedicated read-only role")
        try:
            for cidr in self.INTERNAL_API_ALLOWED_CIDRS.split(","):
                if cidr.strip():
                    ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError as exc:
            raise ValueError("Invalid INTERNAL_API_ALLOWED_CIDRS") from exc
        callers: dict[str, dict[str, object]] | None = None
        if self.INTERNAL_CALLER_TOKENS is not None:
            raw_callers = self.INTERNAL_CALLER_TOKENS.get_secret_value()
            if len(raw_callers) > 64 * 1024:
                raise ValueError("INTERNAL_CALLER_TOKENS is too large")
            try:
                callers = json.loads(raw_callers)
            except json.JSONDecodeError as exc:
                raise ValueError("INTERNAL_CALLER_TOKENS must be valid JSON") from exc
            if not isinstance(callers, dict) or not 1 <= len(callers) <= 32:
                raise ValueError("INTERNAL_CALLER_TOKENS must contain 1..32 callers")
            for caller, record in callers.items():
                if not isinstance(caller, str) or not re.fullmatch(
                    r"[a-z0-9][a-z0-9_-]{0,63}", caller
                ):
                    raise ValueError("Invalid internal caller name")
                if not isinstance(record, dict):
                    raise ValueError("Invalid internal caller record")
                if set(record) != {"token", "scopes"}:
                    raise ValueError("Internal caller records contain unsupported fields")
                token = record.get("token")
                scopes = record.get("scopes")
                if not isinstance(token, str) or len(token) < (
                    32 if secure_environment else 8
                ):
                    raise ValueError("Internal caller tokens are too short")
                if (
                    not isinstance(scopes, list)
                    or not scopes
                    or len(scopes) > 16
                    or any(
                        not isinstance(scope, str)
                        or not re.fullmatch(r"[a-z][a-z0-9:_-]{0,63}", scope)
                        for scope in scopes
                    )
                ):
                    raise ValueError("Invalid internal caller scopes")
            tokens = [record["token"] for record in callers.values()]
            if len(set(tokens)) != len(tokens):
                raise ValueError("Internal caller tokens must be unique")
        legacy_internal_token = (
            self.INTERNAL_API_TOKEN.get_secret_value()
            if self.INTERNAL_API_TOKEN is not None
            else ""
        )
        if secure_environment and legacy_internal_token:
            raise ValueError(
                "Legacy INTERNAL_API_TOKEN is forbidden in staging/production"
            )
        if secure_environment and self.SERVICE_ROLE == "api":
            if callers is None or "auction" not in callers:
                raise ValueError(
                    "staging/production API requires an auction caller credential"
                )
            for caller, record in callers.items():
                scopes = set(record["scopes"])
                if caller == "auction":
                    valid = scopes == {"inventory:write"}
                else:
                    # Optional monitoring identities are read-only and cannot
                    # inherit Auction's inventory mutation authority.
                    valid = scopes == {"metrics:read"}
                if not valid:
                    raise ValueError(
                        "Internal callers must use the minimum service scope"
                    )
        if self.FERNET_KEY:
            try:
                Fernet(self.FERNET_KEY.encode("utf-8"))
                for previous in self.fernet_previous_keys:
                    Fernet(previous)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid Fernet key configuration") from exc
        elif secure_environment:
            raise ValueError("FERNET_KEY is required in staging/production")
        if secure_environment and not self.JWT_PUBLIC_KEY:
            raise ValueError("JWT_PUBLIC_KEY is required in staging/production")

    @property
    def postgres_connect_args(self) -> dict[str, object]:
        """Bound queries and verify PostgreSQL TLS in hardened environments."""
        args: dict[str, object] = {
            "timeout": 8,
            "command_timeout": self.DB_TRANSACTION_TIMEOUT,
            "server_settings": {
                "statement_timeout": str(self.DB_TRANSACTION_TIMEOUT * 1000),
                "idle_in_transaction_session_timeout": str(
                    self.DB_TRANSACTION_TIMEOUT * 1000
                ),
            },
        }
        if self.ENVIRONMENT in {"staging", "production"}:
            context = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH,
                cafile=self.DATABASE_SSL_CA_FILE,
            )
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            args["ssl"] = context
        return args

    @property
    def postgres_sync_connect_args(self) -> dict[str, object]:
        if self.ENVIRONMENT not in {"staging", "production"}:
            return {"connect_timeout": 8}
        args: dict[str, object] = {
            "connect_timeout": 8,
            "sslmode": "verify-full",
        }
        if self.DATABASE_SSL_CA_FILE:
            args["sslrootcert"] = self.DATABASE_SSL_CA_FILE
        return args

    @property
    def mysql_ssl_context(self) -> ssl.SSLContext | None:
        if self.ENVIRONMENT not in {"staging", "production"}:
            return None
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=self.MYSQL_SSL_CA_FILE,
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    @property
    def redis_tls_kwargs(self) -> dict[str, object]:
        if not self.REDIS_URL.startswith("rediss://"):
            return {}
        kwargs: dict[str, object] = {
            "ssl_cert_reqs": ssl.CERT_REQUIRED,
            "ssl_check_hostname": True,
            "ssl_min_version": ssl.TLSVersion.TLSv1_2,
        }
        if self.REDIS_SSL_CA_FILE:
            kwargs["ssl_ca_certs"] = self.REDIS_SSL_CA_FILE
        return kwargs

    def _legacy_jwt_rollout_expiry(self) -> datetime:
        raw = self.JWT_LEGACY_ROLLOUT_EXPIRES_AT.strip()
        try:
            expiry = datetime.fromisoformat(
                raw.removesuffix("Z") + ("+00:00" if raw.endswith("Z") else "")
            )
        except ValueError as exc:
            raise ValueError(
                "JWT_LEGACY_ROLLOUT_EXPIRES_AT must be an RFC3339 timestamp"
            ) from exc
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError("JWT_LEGACY_ROLLOUT_EXPIRES_AT must include a UTC offset")
        return expiry.astimezone(timezone.utc)

    def _check_legacy_jwt_rollout(self, *, strict: bool) -> None:
        configured = bool(
            self.JWT_LEGACY_ROLLOUT_ACK.strip()
            or self.JWT_LEGACY_ROLLOUT_EXPIRES_AT.strip()
        )
        if strict:
            if configured:
                raise ValueError(
                    "legacy JWT rollout acknowledgement must be unset in strict mode"
                )
            return
        if self.JWT_LEGACY_ROLLOUT_ACK != _JWT_LEGACY_ROLLOUT_ACK:
            raise ValueError("temporary legacy JWT acceptance requires explicit acknowledgement")
        now = datetime.now(timezone.utc)
        expiry = self._legacy_jwt_rollout_expiry()
        if expiry <= now or expiry > now + _JWT_LEGACY_ROLLOUT_MAX_WINDOW:
            raise ValueError("temporary legacy JWT acceptance must expire within two hours")

    @property
    def jwt_require_issuer_audience(self) -> bool:
        if self.JWT_REQUIRE_ISSUER_AUDIENCE:
            return True
        if self.ENVIRONMENT not in {"staging", "production"}:
            return False
        return datetime.now(timezone.utc) >= self._legacy_jwt_rollout_expiry()

    @property
    def jwt_require_jti(self) -> bool:
        if self.JWT_REQUIRE_JTI:
            return True
        if self.ENVIRONMENT not in {"staging", "production"}:
            return False
        return datetime.now(timezone.utc) >= self._legacy_jwt_rollout_expiry()

    def _load_secrets_from_ssm(self) -> None:
        """Load secrets from AWS SSM Parameter Store."""
        try:
            ssm_client = boto3.client("ssm", region_name=self.AWS_REGION)

            if self.FERNET_KEY_SSM_PATH and not self.FERNET_KEY:
                try:
                    response = ssm_client.get_parameter(
                        Name=self.FERNET_KEY_SSM_PATH, WithDecryption=True
                    )
                    self.FERNET_KEY = response["Parameter"]["Value"]
                except ssm_client.exceptions.ParameterNotFound:
                    pass

            if self.JWT_PUBLIC_KEY_SSM_PATH and not self.JWT_PUBLIC_KEY:
                try:
                    response = ssm_client.get_parameter(
                        Name=self.JWT_PUBLIC_KEY_SSM_PATH, WithDecryption=False
                    )
                    self.JWT_PUBLIC_KEY = response["Parameter"]["Value"]
                except ssm_client.exceptions.ParameterNotFound:
                    pass

        except Exception:
            if self.DEBUG:
                print("Warning: Could not load secrets from SSM; using environment variables.")

    @property
    def fernet_key_bytes(self) -> bytes:
        """Get Fernet key as bytes."""
        if not self.FERNET_KEY:
            raise ValueError("FERNET_KEY not configured")
        return self.FERNET_KEY.encode("utf-8")

    @property
    def fernet_previous_keys(self) -> tuple[bytes, ...]:
        return tuple(
            key.strip().encode("utf-8")
            for key in (self.FERNET_PREVIOUS_KEYS or "").split(",")
            if key.strip()
        )

    @property
    def test_endpoints_enabled(self) -> bool:
        """Test helpers are never exposed merely because a flag leaked to production."""
        return self.ENABLE_TEST_ENDPOINTS and self.ENVIRONMENT in {
            "development",
            "test",
        }

    def _format_pem_public_key(self, key_str: Optional[str]) -> str:
        """
        Normalize PEM format for RSA public key.
        Handles keys from AWS SSM (may be single line or multi-line).
        """
        if key_str is None:
            raise ValueError("JWT_PUBLIC_KEY is None")
        key_str = key_str.strip().replace("\r\n", "\n").replace("\r", "\n")
        if not key_str:
            raise ValueError("JWT_PUBLIC_KEY is empty")

        # Already in PEM format
        if "-----BEGIN" in key_str and "-----END" in key_str:
            return key_str

        # Single line from env/SSM: assume raw base64 body
        body = key_str.replace(" ", "").replace("\n", "")
        if not body:
            raise ValueError("JWT_PUBLIC_KEY has no key body")
        lines = [body[i : i + 64] for i in range(0, len(body), 64)]
        return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----"

    @property
    def jwt_public_key_pem(self) -> str:
        """Get JWT public key in PEM format."""
        if not self.JWT_PUBLIC_KEY:
            raise ValueError("JWT_PUBLIC_KEY not configured")
        pem = self._format_pem_public_key(self.JWT_PUBLIC_KEY)
        try:
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
            from cryptography.hazmat.primitives.serialization import load_pem_public_key

            key = load_pem_public_key(pem.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("JWT_PUBLIC_KEY is not a valid PEM public key") from exc
        if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
            raise ValueError("JWT_PUBLIC_KEY must be an RSA key of at least 2048 bits")
        return pem

    @property
    def celery_broker_url(self) -> str:
        """Get Celery broker URL, defaulting to REDIS_URL."""
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_result_backend(self) -> str:
        """Get Celery result backend, defaulting to REDIS_URL."""
        return self.CELERY_RESULT_BACKEND or self.REDIS_URL


@lru_cache()
def get_settings() -> Settings:
    """Cached settings; fails at first access if required env vars are missing."""
    return Settings()
