"""Publish a canonical catalog document and acknowledge the actual Search task."""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.services.catalog_importer import CatalogImportNeedsReview, CatalogImportTransientError


class MeilisearchCatalogPublisher:
    def __init__(
        self, url: str, api_key: str, index: str = "cards", *,
        transport: httpx.AsyncBaseTransport | None = None,
        task_timeout_seconds: float = 120,
    ) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9_-]+", index) or not api_key
        ):
            raise ValueError("Invalid catalog Search configuration")
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.index = index
        self.transport = transport
        self.task_timeout_seconds = task_timeout_seconds

    async def _json(self, client: httpx.AsyncClient, method: str, path: str, **kwargs):
        response = await client.request(method, path, **kwargs)
        if response.status_code == 404 and method == "GET" and "/documents/" in path:
            return None
        if not response.is_success:
            raise CatalogImportTransientError(
                "search_http_error", f"Search HTTP {response.status_code}"
            )
        if len(response.content) > 2 * 1024 * 1024:
            raise CatalogImportTransientError("search_response_too_large", "Search response exceeds limit")
        result = response.json()
        if not isinstance(result, dict):
            raise CatalogImportTransientError("search_invalid_response", "Search returned an invalid object")
        return result

    async def _wait(self, client: httpx.AsyncClient, result: dict[str, Any]) -> None:
        task_id = result.get("taskUid")
        if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id < 0:
            raise CatalogImportTransientError("search_missing_task", "Search did not acknowledge a task")
        deadline = time.monotonic() + self.task_timeout_seconds
        while time.monotonic() < deadline:
            task = await self._json(client, "GET", f"/tasks/{task_id}")
            if task.get("status") == "succeeded":
                return
            if task.get("status") in {"failed", "canceled"}:
                raise CatalogImportTransientError("search_task_failed", "Search indexing task failed")
            await asyncio.sleep(0.5)
        raise CatalogImportTransientError("search_task_timeout", "Search indexing task is still unconfirmed")

    async def publish(self, document: Mapping[str, Any]) -> None:
        document_id = document.get("id")
        blueprint_id = document.get("cardtrader_id")
        if (
            not isinstance(document_id, str) or not re.fullmatch(r"mtg_[1-9][0-9]*", document_id)
            or not isinstance(blueprint_id, int) or isinstance(blueprint_id, bool) or blueprint_id <= 0
            or document.get("game_slug") != "mtg" or document.get("category_id") != 1
        ):
            raise CatalogImportNeedsReview("invalid_search_identity", "Canonical Magic identity is required")
        base = f"/indexes/{self.index}"
        async with httpx.AsyncClient(
            base_url=self.url, transport=self.transport, trust_env=False,
            follow_redirects=False, timeout=httpx.Timeout(15, connect=5),
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        ) as client:
            existing = await self._json(client, "GET", f"{base}/documents/{document_id}")
            if existing is not None and existing.get("cardtrader_id") != blueprint_id:
                raise CatalogImportNeedsReview("search_identity_collision", "Document belongs to another blueprint")
            found = await self._json(client, "POST", f"{base}/search", json={
                "filter": f"cardtrader_id = {blueprint_id}", "limit": 100,
            })
            hits = found.get("hits")
            if not isinstance(hits, list) or len(hits) >= 100:
                raise CatalogImportNeedsReview("search_ambiguous_duplicates", "Cannot prove blueprint index coverage")
            legacy_id = f"mtg_{blueprint_id}"
            aliases = []
            for hit in hits:
                if (
                    not isinstance(hit, dict) or hit.get("cardtrader_id") != blueprint_id
                    or hit.get("game_slug") != "mtg" or hit.get("category_id") != 1
                    or hit.get("id") not in {document_id, legacy_id}
                ):
                    raise CatalogImportNeedsReview("search_ambiguous_duplicates", "Unexpected blueprint identity in index")
                if hit["id"] != document_id:
                    aliases.append(hit)
            # PUT preserves prices/localized fields already present on canonical documents.
            payload = {key: value for key, value in document.items() if value is not None}
            # The importer knows the provider name, while Search can already
            # contain translations. Merge those lists instead of replacing
            # them with the importer's English-only projection on a retry.
            previous = existing or (aliases[0] if aliases else {})
            for key, limit in (("keywords_localized", 32), ("search_tokens", 256)):
                old_values = previous.get(key)
                new_values = payload.get(key)
                if isinstance(old_values, list):
                    candidates = old_values + (new_values if isinstance(new_values, list) else [])
                    payload[key] = list(dict.fromkeys(
                        value[:1000] for value in candidates if isinstance(value, str) and value
                    ))[:limit]
            if not existing and aliases:
                for key in ("market_price", "foil_price"):
                    value = aliases[0].get(key)
                    if value is not None and key not in payload:
                        payload[key] = value
            accepted = await self._json(client, "PUT", f"{base}/documents", json=[payload])
            await self._wait(client, accepted)
            # Legacy IDs may still be referenced by saved links and favorites.
            # Keep them; this worker never needs document deletion privileges.


def build_catalog_index_publisher_from_settings() -> MeilisearchCatalogPublisher:
    settings = get_settings()
    if (
        settings.SERVICE_ROLE != "worker" or not settings.CATALOG_SEARCH_PUBLISH_ENABLED
        or not settings.CATALOG_MEILISEARCH_URL or not settings.CATALOG_MEILISEARCH_API_KEY
    ):
        raise CatalogImportNeedsReview("search_publisher_disabled", "Catalog Search publisher is not configured")
    return MeilisearchCatalogPublisher(
        settings.CATALOG_MEILISEARCH_URL,
        settings.CATALOG_MEILISEARCH_API_KEY.get_secret_value(),
        settings.CATALOG_MEILISEARCH_INDEX,
    )
