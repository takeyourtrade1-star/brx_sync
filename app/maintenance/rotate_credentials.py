"""Encrypt legacy webhook secrets and rotate credentials to the primary Fernet key."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from pathlib import Path
import socket
import ssl
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.inventory import UserSyncSettings

logger = logging.getLogger(__name__)


class RotationEncryptionManager:
    """Minimal Fernet boundary for the one-shot rotation job."""

    def __init__(self, primary_key: str, previous_keys: tuple[str, ...]) -> None:
        try:
            self._primary = Fernet(primary_key.encode("ascii"))
            keys = [
                self._primary,
                *(Fernet(key.encode("ascii")) for key in previous_keys),
            ]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Invalid credential-rotation Fernet key configuration"
            ) from exc
        self.fernet = MultiFernet(keys)

    def decrypt(self, ciphertext: str) -> str:
        return self.fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")

    def rotate(self, ciphertext: str) -> str:
        return self.fernet.rotate(ciphertext.encode("ascii")).decode("ascii")

    def rotate_at_rest_secret(self, stored: str) -> str:
        if stored.startswith("fernet:"):
            return "fernet:" + self.rotate(stored.removeprefix("fernet:"))
        return "fernet:" + self.fernet.encrypt(stored.encode("utf-8")).decode(
            "ascii"
        )


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required for credential rotation")
    return value


def _rotation_configuration() -> tuple[
    str, dict[str, object], RotationEncryptionManager
]:
    database_url = _required_environment("DATABASE_URL")
    parsed = urlsplit(database_url)
    if (
        parsed.scheme != "postgresql+asyncpg"
        or not parsed.hostname
        or not parsed.username
        or not parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(
            "Credential rotation requires a canonical asyncpg DATABASE_URL"
        )
    if parsed.username.casefold() in {"postgres", "admin", "root", "brx_bd_admin"}:
        raise RuntimeError("Credential rotation refuses an administrative database role")
    if not parsed.hostname.endswith(".rds.amazonaws.com"):
        raise RuntimeError("Credential rotation database must be an Amazon RDS endpoint")
    try:
        addresses = {
            ipaddress.ip_address(answer[4][0])
            for answer in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or 5432,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise RuntimeError("Credential rotation database host must resolve") from exc
    if not addresses or any(
        not address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        for address in addresses
    ):
        raise RuntimeError(
            "Credential rotation database must resolve only to private addresses"
        )

    ca_file = Path(_required_environment("DATABASE_SSL_CA_FILE"))
    if not ca_file.is_file():
        raise RuntimeError("Credential rotation CA bundle is not readable")
    tls = ssl.create_default_context(cafile=str(ca_file))
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.check_hostname = True
    tls.verify_mode = ssl.CERT_REQUIRED

    primary_key = _required_environment("FERNET_KEY")
    previous_keys = tuple(
        key.strip()
        for key in os.environ.get("FERNET_PREVIOUS_KEYS", "").split(",")
        if key.strip()
    )
    if len(set((primary_key, *previous_keys))) != 1 + len(previous_keys):
        raise RuntimeError("Credential-rotation Fernet keys must be unique")
    manager = RotationEncryptionManager(primary_key, previous_keys)
    connect_args: dict[str, object] = {
        "ssl": tls,
        "timeout": 10,
        "command_timeout": 30,
        "server_settings": {
            "statement_timeout": "30000",
            "lock_timeout": "5000",
        },
    }
    return database_url, connect_args, manager


def rotate_row_credentials(row: Any, manager: Any) -> None:
    """Validate before replacing a row, so corrupt ciphertext fails the deploy."""
    manager.decrypt(row.cardtrader_token_encrypted)
    row.cardtrader_token_encrypted = manager.rotate(row.cardtrader_token_encrypted)
    if row.webhook_secret is not None:
        row.webhook_secret = manager.rotate_at_rest_secret(row.webhook_secret)


async def rotate_all_credentials(batch_size: int = 100) -> int:
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    database_url, connect_args, manager = _rotation_configuration()
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    rotated = 0
    last_user_id = None

    try:
        while True:
            async with session_factory() as session:
                async with session.begin():
                    statement = (
                        select(UserSyncSettings)
                        .order_by(UserSyncSettings.user_id)
                        .limit(batch_size)
                    )
                    if last_user_id is not None:
                        statement = statement.where(
                            UserSyncSettings.user_id > last_user_id
                        )
                    rows = list(
                        (
                            await session.execute(statement.with_for_update())
                        ).scalars().all()
                    )
                    if not rows:
                        break
                    for row in rows:
                        rotate_row_credentials(row, manager)
                    last_user_id = rows[-1].user_id
                    rotated += len(rows)
    finally:
        await engine.dispose()

    logger.info("Credential encryption maintenance completed for %d rows", rotated)
    return rotated


def main() -> None:
    asyncio.run(rotate_all_credentials())


if __name__ == "__main__":
    main()
