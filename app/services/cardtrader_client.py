"""
CardTrader V2 API client with rate limiting and error handling.
"""

import asyncio
import json
import logging
import math
import random
import uuid
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import get_settings
from app.services.adaptive_rate_limiter import get_adaptive_rate_limiter
from app.services.circuit_breaker import (
    CircuitState,
    get_circuit_breaker,
)
from app.services.rate_limiter import get_rate_limiter

settings = get_settings()
logger = logging.getLogger(__name__)


class CardTraderAPIError(Exception):
    """Base exception for CardTrader API errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        outcome_unknown: bool = False,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown
        self.retryable = retryable


class RateLimitError(CardTraderAPIError):
    """Rate limit exceeded (429)."""

    pass


class CardTraderClient:
    """Client for CardTrader V2 API with rate limiting."""

    def __init__(self, token: str, user_id: str):
        """
        Initialize CardTrader client.

        Args:
            token: CardTrader API token (decrypted)
            user_id: User ID for rate limiting
        """
        if not isinstance(token, str) or not token.strip() or len(token) > 4096:
            raise ValueError("Invalid CardTrader credential")
        self.token = token
        self.user_id = user_id
        self.base_url = settings.CARDTRADER_API_BASE_URL
        self.rate_limiter = get_rate_limiter()
        self.adaptive_rate_limiter = get_adaptive_rate_limiter()
        self.circuit_breaker = get_circuit_breaker(self.user_id)

        # HTTP client with longer timeout for bulk operations
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(180.0, connect=10.0),  # 180s for bulk export
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
            },
        )

    async def _wait_for_rate_limit(self) -> None:
        """Wait if rate limit is exceeded (using adaptive rate limiter)."""
        allowed, wait_seconds = self.adaptive_rate_limiter.check_and_consume(self.user_id)

        if not allowed and wait_seconds:
            logger.warning(
                f"Rate limit exceeded for user {self.user_id}, "
                f"waiting {wait_seconds:.2f} seconds"
            )
            await asyncio.sleep(wait_seconds)
            # Try once more after waiting
            allowed, wait_seconds = self.adaptive_rate_limiter.check_and_consume(self.user_id)
            if not allowed:
                raise RateLimitError(
                    f"Rate limit still exceeded after waiting. "
                    f"Please retry in {wait_seconds:.2f} seconds"
                )

    @staticmethod
    async def _read_bounded_response(response: httpx.Response) -> bytearray:
        """Read a streamed response without ever buffering beyond the hard cap."""
        max_bytes = settings.CARDTRADER_MAX_RESPONSE_BYTES
        content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise CardTraderAPIError(
                "CardTrader returned an unsupported Content-Encoding"
            )
        declared_size = response.headers.get("Content-Length")
        if declared_size is not None:
            if (
                len(declared_size) > 20
                or not declared_size.isascii()
                or not declared_size.isdecimal()
            ):
                raise CardTraderAPIError(
                    "CardTrader returned an invalid Content-Length header"
                )
            if int(declared_size) > max_bytes:
                raise CardTraderAPIError(
                    "CardTrader response exceeded the configured limit"
                )

        body = bytearray()
        # Read raw transfer-decoded bytes. Combined with Accept-Encoding: identity
        # this prevents an upstream compression bomb from allocating a decoded
        # chunk before the application can enforce its limit.
        async for chunk in response.aiter_raw():
            if len(body) + len(chunk) > max_bytes:
                raise CardTraderAPIError(
                    "CardTrader response exceeded the configured limit"
                )
            body.extend(chunk)
        return body

    async def _make_request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """
        Make HTTP request with rate limiting and error handling.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE)
            endpoint: API endpoint (without base URL)
            **kwargs: Additional arguments for httpx request

        Returns:
            Response JSON data

        Raises:
            RateLimitError: If rate limit is exceeded
            CardTraderAPIError: For other API errors
        """
        method = method.upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "DELETE"}:
            raise CardTraderAPIError("Unsupported CardTrader HTTP method")
        if (
            not isinstance(endpoint, str)
            or not endpoint.startswith("/")
            or endpoint.startswith("//")
            or "\\" in endpoint
            or "\x00" in endpoint
        ):
            raise CardTraderAPIError("Invalid CardTrader endpoint")
        forbidden_options = {"auth", "cookies", "follow_redirects", "headers"}
        if forbidden_options.intersection(kwargs):
            raise CardTraderAPIError("Unsafe CardTrader request option")
        safe_to_retry = method in {"GET", "HEAD", "OPTIONS"}
        max_attempts = 3

        # The breaker is scoped to this CardTrader account.
        state = self.circuit_breaker.get_state()
        if state == CircuitState.OPEN:
            if not self.circuit_breaker.should_attempt_reset():
                raise RateLimitError(
                    "CardTrader service temporarily unavailable. "
                    "Circuit breaker is OPEN. Please retry later.",
                    retryable=True,
                )
            self.circuit_breaker.set_state(CircuitState.HALF_OPEN)
            logger.info("Circuit breaker reset to HALF_OPEN, testing service recovery")

        for attempt in range(1, max_attempts + 1):
            await self._wait_for_rate_limit()
            try:
                request = self.client.build_request(method, endpoint, **kwargs)
                response = await self.client.send(request, stream=True)
                try:
                    response_body = await self._read_bounded_response(response)
                finally:
                    await response.aclose()

                if response.status_code == 429:
                    self.adaptive_rate_limiter.record_429_response(self.user_id)
                    if attempt >= max_attempts:
                        raise RateLimitError(
                            f"Rate limit exceeded after {max_attempts} attempts",
                            status_code=429,
                            retryable=True,
                        )
                    try:
                        retry_after = float(response.headers.get("Retry-After", 10))
                        if not math.isfinite(retry_after):
                            raise ValueError("non-finite Retry-After")
                        retry_after = min(
                            settings.CARDTRADER_MAX_RETRY_AFTER_SECONDS,
                            max(0.0, retry_after),
                        )
                    except (TypeError, ValueError):
                        retry_after = min(
                            10.0,
                            settings.CARDTRADER_MAX_RETRY_AFTER_SECONDS,
                        )
                    wait_time = retry_after + (attempt * 2) + random.uniform(0, 1)
                    logger.warning(
                        "Rate limit 429 from CardTrader API "
                        "(attempt %s/%s), waiting %.2f seconds",
                        attempt,
                        max_attempts,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue

                if response.status_code >= 400:
                    status_code = response.status_code
                    server_failure = status_code >= 500
                    if server_failure:
                        self.circuit_breaker.record_failure("server_error")
                        if safe_to_retry and attempt < max_attempts:
                            await asyncio.sleep((2 ** (attempt - 1)) + random.uniform(0, 0.5))
                            continue
                    error_msg = f"CardTrader API returned HTTP {status_code}"
                    logger.error("CardTrader API returned HTTP %s", status_code)
                    raise CardTraderAPIError(
                        error_msg,
                        status_code=status_code,
                        outcome_unknown=server_failure and not safe_to_retry,
                        retryable=server_failure,
                    )

                try:
                    result = {} if response.status_code == 204 else json.loads(response_body)
                except (UnicodeDecodeError, ValueError) as exc:
                    self.circuit_breaker.record_failure("invalid_response")
                    if safe_to_retry and attempt < max_attempts:
                        await asyncio.sleep((2 ** (attempt - 1)) + random.uniform(0, 0.5))
                        continue
                    error_msg = "CardTrader returned a successful but invalid JSON response"
                    logger.error(error_msg)
                    raise CardTraderAPIError(
                        error_msg,
                        status_code=response.status_code,
                        outcome_unknown=not safe_to_retry,
                        retryable=safe_to_retry,
                    ) from exc

                # These stores are telemetry only: their implementations fail
                # open and cannot turn a remote success into a local failure.
                try:
                    self.adaptive_rate_limiter.record_success(self.user_id)
                except Exception as exc:
                    logger.warning(
                        "Adaptive telemetry failed after CardTrader success (%s)",
                        type(exc).__name__,
                    )
                try:
                    self.circuit_breaker.record_success()
                except Exception as exc:
                    logger.warning(
                        "Circuit telemetry failed after CardTrader success (%s)",
                        type(exc).__name__,
                    )
                return result

            except (RateLimitError, CardTraderAPIError):
                raise
            except httpx.RequestError as exc:
                self.circuit_breaker.record_failure("network_error")
                if safe_to_retry and attempt < max_attempts:
                    await asyncio.sleep((2 ** (attempt - 1)) + random.uniform(0, 0.5))
                    continue
                error_msg = "CardTrader network request failed"
                logger.error(error_msg)
                raise CardTraderAPIError(
                    error_msg,
                    outcome_unknown=not safe_to_retry,
                    retryable=True,
                ) from exc

        raise CardTraderAPIError("CardTrader request attempts exhausted", retryable=True)

    async def get_info(self) -> Dict[str, Any]:
        """Get app info and shared_secret from /info endpoint."""
        return await self._make_request("GET", "/info")

    async def get_products_export(
        self,
        blueprint_id: Optional[int] = None,
        expansion_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Export all products from CardTrader inventory.

        Args:
            blueprint_id: Optional filter by blueprint_id
            expansion_id: Optional filter by expansion_id

        Returns:
            List of product objects

        Note:
            This endpoint may take 120-180 seconds for large collections.
        """
        params = {}
        if blueprint_id:
            params["blueprint_id"] = blueprint_id
        if expansion_id:
            params["expansion_id"] = expansion_id

        return await self._make_request("GET", "/products/export", params=params)

    async def get_product(self, product_id: int) -> Optional[Dict[str, Any]]:
        """Read one product for a last-moment stale-stock guard."""

        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise ValueError("Invalid CardTrader product id")

        try:
            result = await self._make_request("GET", f"/products/{product_id}")
        except CardTraderAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        resource = result.get("resource") if isinstance(result, dict) else None
        return resource if isinstance(resource, dict) else result

    async def bulk_update_products(self, products: List[Dict[str, Any]]) -> Dict[str, str]:
        """
        Update multiple products (asynchronous job).

        Args:
            products: List of product dictionaries with id and fields to update

        Returns:
            {"job": "uuid"} - Job UUID for status checking
        """
        if not isinstance(products, list) or not 1 <= len(products) <= 1000:
            raise ValueError("Invalid CardTrader bulk update size")
        return await self._make_request(
            "POST", "/products/bulk_update", json={"products": products}
        )

    async def create_product(self, product: Dict[str, Any]) -> Dict[str, Any]:
        """Create one product synchronously, with a bounded strict payload."""
        if not isinstance(product, dict):
            raise ValueError("Invalid CardTrader create payload")
        allowed = {
            "blueprint_id",
            "price",
            "quantity",
            "description",
            "error_mode",
            "user_data_field",
            "properties",
            "graded",
        }
        if set(product) - allowed:
            raise ValueError("Unsupported CardTrader create field")
        blueprint_id = product.get("blueprint_id")
        quantity = product.get("quantity")
        price = product.get("price")
        if (
            not isinstance(blueprint_id, int)
            or isinstance(blueprint_id, bool)
            or blueprint_id <= 0
            or not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or not 1 <= quantity <= 1_000_000
            or not isinstance(price, (int, float))
            or isinstance(price, bool)
            or not 0 < float(price) <= 1_000_000
        ):
            raise ValueError("Invalid CardTrader create identity or stock")
        return await self._make_request("POST", "/products", json=product)

    async def get_job_status(self, job_uuid: str) -> Dict[str, Any]:
        """
        Get status of an asynchronous job.

        Args:
            job_uuid: Job UUID from bulk_create/bulk_update

        Returns:
            Job status object with state, stats, results
        """
        try:
            safe_job_id = str(uuid.UUID(str(job_uuid)))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid CardTrader job id") from exc
        return await self._make_request("GET", f"/jobs/{safe_job_id}")

    async def get_expansions_export(self) -> List[Dict[str, Any]]:
        """Get list of expansions the user has products for."""
        return await self._make_request("GET", "/expansions/export")

    async def delete_product(self, product_id: int) -> Dict[str, Any]:
        """
        Delete a product from CardTrader.

        Args:
            product_id: CardTrader product ID

        Returns:
            Deletion result. If product is already deleted (404), returns success status.

        Raises:
            CardTraderAPIError: If deletion fails (except 404 which is treated as success)
        """
        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise ValueError("Invalid CardTrader product id")
        try:
            return await self._make_request("DELETE", f"/products/{product_id}")
        except CardTraderAPIError as e:
            # If product not found (404), it's already deleted - treat as success
            if e.status_code == 404:
                logger.info(
                    f"Product {product_id} not found on CardTrader (already deleted). "
                    f"Treating as successful deletion."
                )
                return {
                    "status": "already_deleted",
                    "product_id": product_id,
                    "message": "Product was already deleted on CardTrader",
                }
            # Re-raise other errors
            raise
        except httpx.HTTPStatusError as e:
            # Handle 404 directly from HTTPStatusError
            if e.response.status_code == 404:
                logger.info(
                    f"Product {product_id} not found on CardTrader (404). "
                    f"Treating as successful deletion."
                )
                return {
                    "status": "already_deleted",
                    "product_id": product_id,
                    "message": "Product was already deleted on CardTrader",
                }
            # Re-raise other HTTP errors (they will be caught by _make_request)
            raise

    async def increment_product_quantity(
        self,
        product_id: int,
        delta_quantity: int,
    ) -> Dict[str, Any]:
        """
        Increment or decrement product quantity.

        Args:
            product_id: CardTrader product ID
            delta_quantity: Quantity change (positive or negative)

        Returns:
            Updated product resource

        Note:
            If resulting quantity is 0 or less, the product will be deleted.
        """
        if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0:
            raise ValueError("Invalid CardTrader product id")
        if (
            not isinstance(delta_quantity, int)
            or isinstance(delta_quantity, bool)
            or delta_quantity == 0
            or abs(delta_quantity) > 1_000_000
        ):
            raise ValueError("Invalid CardTrader quantity delta")
        return await self._make_request(
            "POST", f"/products/{product_id}/increment", json={"delta_quantity": delta_quantity}
        )

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
