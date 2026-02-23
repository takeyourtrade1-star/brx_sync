"""
Configuration settings for BRX Sync Microservice.
12-Factor config: no hardcoded secrets, fail fast on missing critical env.
"""
from functools import lru_cache
from typing import Optional

import boto3
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # App (non-sensitive, safe defaults)
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "BRX Sync"
    APP_NAME: str = "brx-sync"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = Field(default=False, description="Debug mode")
    ENVIRONMENT: str = Field(default="production", description="Environment name")
    ALLOWED_ORIGINS: str = Field(
        default="*",
        description="Comma-separated list of allowed CORS origins (use '*' for all in dev only)"
    )

    # PostgreSQL — required, no defaults (fail fast)
    DATABASE_URL: str = Field(
        ...,
        description="PostgreSQL connection string (asyncpg format)",
        examples=["postgresql+asyncpg://user:pass@host:5432/dbname"],
    )
    DB_POOL_SIZE: int = Field(default=50, description="Database connection pool size (increased for production scalability)")
    DB_MAX_OVERFLOW: int = Field(default=100, description="Max overflow connections (increased for production scalability)")
    DB_TRANSACTION_TIMEOUT: int = Field(
        default=30, description="Database transaction timeout in seconds"
    )
    
    # MySQL Connection Pool
    MYSQL_POOL_SIZE: int = Field(default=20, description="MySQL connection pool size (increased for bulk sync scalability)")
    MYSQL_POOL_MAX_OVERFLOW: int = Field(default=20, description="MySQL connection pool max overflow (increased for bulk sync scalability)")

    # MySQL — required for blueprint mapping (read-only)
    MYSQL_HOST: str = Field(..., description="MySQL host")
    MYSQL_PORT: int = Field(default=3306, description="MySQL port")
    MYSQL_USER: str = Field(..., description="MySQL user")
    MYSQL_PASSWORD: SecretStr = Field(..., description="MySQL password")
    MYSQL_DATABASE: str = Field(..., description="MySQL database name")

    # Redis — required for rate limiting and Celery
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for rate limiting and Celery broker",
    )

    # Fernet Encryption — required for token encryption
    FERNET_KEY_SSM_PATH: Optional[str] = Field(
        default="/prod/ebartex/fernet_key",
        description="SSM path for Fernet encryption key (32-byte base64)",
    )
    FERNET_KEY: Optional[str] = Field(
        default=None, description="Fernet key (base64) - fallback to env"
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

    # AWS Integration (optional)
    AWS_REGION: str = Field(default="eu-south-1", description="AWS region for SSM")
    AWS_SSM_ENABLED: bool = Field(
        default=True, description="Enable AWS SSM Parameter Store"
    )
    AWS_SSM_PREFIX: str = Field(
        default="/prod/ebartex", description="SSM parameter prefix"
    )

    # CardTrader API
    CARDTRADER_API_BASE_URL: str = Field(
        default="https://api.cardtrader.com/api/v2",
        description="CardTrader V2 API base URL",
    )

    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = Field(
        default=200, description="Rate limit requests per window"
    )
    RATE_LIMIT_WINDOW_SECONDS: int = Field(
        default=10, description="Rate limit window in seconds"
    )

    # Celery
    CELERY_BROKER_URL: Optional[str] = Field(
        default=None, description="Celery broker URL (defaults to REDIS_URL)"
    )
    CELERY_RESULT_BACKEND: Optional[str] = Field(
        default=None, description="Celery result backend (defaults to REDIS_URL)"
    )

    # Scalability: disable sync file logging in production (many workers = contention on one file)
    SYNC_LOG_TO_FILE: bool = Field(
        default=True,
        description="If False, skip _log_to_file() in sync tasks (use logger only). Set to False in production with many Celery workers.",
    )

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "DATABASE_URL must use asyncpg driver: postgresql+asyncpg://..."
            )
        return v

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if self.AWS_SSM_ENABLED:
            self._load_secrets_from_ssm()

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

        except Exception as e:
            if self.DEBUG:
                print(f"Warning: Could not load from SSM: {e}. Using environment variables.")

    @property
    def fernet_key_bytes(self) -> bytes:
        """Get Fernet key as bytes."""
        if not self.FERNET_KEY:
            raise ValueError("FERNET_KEY not configured")
        return self.FERNET_KEY.encode("utf-8")

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
        return self._format_pem_public_key(self.JWT_PUBLIC_KEY)

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
