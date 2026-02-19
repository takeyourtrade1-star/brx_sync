"""
CardTrader V2 API client with rate limiting and error handling.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx
from app.core.config import get_settings
from app.core.crypto import get_encryption_manager
from app.services.rate_limiter import get_rate_limiter
from app.services.adaptive_rate_limiter import get_adaptive_rate_limiter
from app.services.circuit_breaker import (
    get_circuit_breaker,
    CircuitBreakerOpenError,
    CircuitState,
)

settings = get_settings()
logger = logging.getLogger(__name__)


class CardTraderAPIError(Exception):
    """Base exception for CardTrader API errors."""
    pass


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
        self.token = token
        self.user_id = user_id
        self.base_url = settings.CARDTRADER_API_BASE_URL
        self.rate_limiter = get_rate_limiter()
        self.adaptive_rate_limiter = get_adaptive_rate_limiter()
        self.circuit_breaker = get_circuit_breaker()
        self.encryption_manager = get_encryption_manager()
        
        # HTTP client with longer timeout for bulk operations
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(180.0, connect=10.0),  # 180s for bulk export
            headers={
                "Authorization": f"Bearer {self.token}",
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

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        **kwargs
    ) -> Dict[str, Any]:
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
        # Check circuit breaker first
        state = self.circuit_breaker.get_state()
        if state == CircuitState.OPEN:
            if not self.circuit_breaker.should_attempt_reset():
                raise RateLimitError(
                    "CardTrader service temporarily unavailable. "
                    "Circuit breaker is OPEN. Please retry later."
                )
            # Attempt reset to HALF_OPEN
            self.circuit_breaker.set_state(CircuitState.HALF_OPEN)
            logger.info("Circuit breaker reset to HALF_OPEN, testing service recovery")
        
        url = f"{self.base_url}{endpoint}" if not endpoint.startswith("http") else endpoint
        
        # Check rate limit before request
        await self._wait_for_rate_limit()
        
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                response = await self.client.request(method, url, **kwargs)
                
                # Handle rate limit (429) with exponential backoff
                if response.status_code == 429:
                    retry_count += 1
                    retry_after = float(response.headers.get("Retry-After", 10))
                    # Add jitter and exponential backoff
                    import random
                    wait_time = retry_after + (retry_count * 2) + random.uniform(0, 1)
                    
                    logger.warning(
                        f"Rate limit 429 from CardTrader API (attempt {retry_count}/{max_retries}), "
                        f"waiting {wait_time:.2f} seconds before retry"
                    )
                    
                    # Record 429 for adaptive rate limiter
                    self.adaptive_rate_limiter.record_429_response(self.user_id)
                    
                    await asyncio.sleep(wait_time)
                    
                    # Update rate limiter state after waiting
                    await self._wait_for_rate_limit()
                    
                    if retry_count >= max_retries:
                        raise RateLimitError(
                            f"Rate limit exceeded after {max_retries} retries. "
                            f"Please wait and try again later."
                        )
                    continue  # Retry the request
                
                # Success - return response
                response.raise_for_status()
                result = response.json()
                
                # Record success for adaptive rate limiter
                self.adaptive_rate_limiter.record_success(self.user_id)
                
                return result
                
            except httpx.HTTPStatusError as e:
                # If it's still 429 after retries, raise RateLimitError
                if e.response.status_code == 429:
                    # Record 429 for adaptive rate limiter
                    self.adaptive_rate_limiter.record_429_response(self.user_id)
                    
                    if retry_count >= max_retries:
                        raise RateLimitError(
                            f"Rate limit exceeded after {max_retries} retries"
                        ) from e
                    # Continue to retry
                    retry_count += 1
                    retry_after = float(e.response.headers.get("Retry-After", 10))
                    import random
                    wait_time = retry_after + (retry_count * 2) + random.uniform(0, 1)
                    logger.warning(
                        f"Rate limit 429 error (attempt {retry_count}/{max_retries}), "
                        f"waiting {wait_time:.2f} seconds"
                    )
                    await asyncio.sleep(wait_time)
                    await self._wait_for_rate_limit()
                    continue
                # Other HTTP errors - record failure for circuit breaker
                error_type = "rate_limit" if e.response.status_code == 429 else "api_error"
                self.circuit_breaker.record_failure(error_type)
                error_msg = f"CardTrader API error {e.response.status_code}: {e.response.text}"
                logger.error(error_msg)
                raise CardTraderAPIError(error_msg) from e
            
            except httpx.RequestError as e:
                # Record failure for circuit breaker
                self.circuit_breaker.record_failure("network_error")
                error_msg = f"Request error: {str(e)}"
                logger.error(error_msg)
                raise CardTraderAPIError(error_msg) from e

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

    async def bulk_create_products(
        self, products: List[Dict[str, Any]]
    ) -> Dict[str, str]:
        """
        Create multiple products (asynchronous job).
        
        Args:
            products: List of product dictionaries with blueprint_id, price, quantity, etc.
            
        Returns:
            {"job": "uuid"} - Job UUID for status checking
        """
        return await self._make_request(
            "POST",
            "/products/bulk_create",
            json={"products": products}
        )

    async def bulk_update_products(
        self, products: List[Dict[str, Any]]
    ) -> Dict[str, str]:
        """
        Update multiple products (asynchronous job).
        
        Args:
            products: List of product dictionaries with id and fields to update
            
        Returns:
            {"job": "uuid"} - Job UUID for status checking
        """
        return await self._make_request(
            "POST",
            "/products/bulk_update",
            json={"products": products}
        )

    async def get_job_status(self, job_uuid: str) -> Dict[str, Any]:
        """
        Get status of an asynchronous job.
        
        Args:
            job_uuid: Job UUID from bulk_create/bulk_update
            
        Returns:
            Job status object with state, stats, results
        """
        return await self._make_request("GET", f"/jobs/{job_uuid}")

    async def get_expansions_export(self) -> List[Dict[str, Any]]:
        """Get list of expansions the user has products for."""
        return await self._make_request("GET", "/expansions/export")

    async def update_product(
        self,
        product_id: int,
        price: Optional[float] = None,
        quantity: Optional[int] = None,
        properties: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Update a single product (synchronous).
        
        Args:
            product_id: CardTrader product ID
            price: New price (optional)
            quantity: New quantity (optional)
            properties: Product properties (optional)
            
        Returns:
            Updated product resource
        """
        update_data = {"id": product_id}
        if price is not None:
            update_data["price"] = price
        if quantity is not None:
            update_data["quantity"] = quantity
        if properties is not None:
            update_data["properties"] = properties
        
        # Use bulk_update for single product (CardTrader supports it)
        result = await self.bulk_update_products([update_data])
        return result

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
        try:
            return await self._make_request("DELETE", f"/products/{product_id}")
        except CardTraderAPIError as e:
            # If product not found (404), it's already deleted - treat as success
            error_str = str(e).lower()
            if "404" in str(e) or "not_found" in error_str:
                logger.info(
                    f"Product {product_id} not found on CardTrader (already deleted). "
                    f"Treating as successful deletion."
                )
                return {
                    "status": "already_deleted",
                    "product_id": product_id,
                    "message": "Product was already deleted on CardTrader"
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
                    "message": "Product was already deleted on CardTrader"
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
        return await self._make_request(
            "POST",
            f"/products/{product_id}/increment",
            json={"delta_quantity": delta_quantity}
        )

    async def get_product_by_id(self, product_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a single product by ID from CardTrader inventory.
        
        Args:
            product_id: CardTrader product ID (as string)
            
        Returns:
            Product object if found, None otherwise
            
        Note:
            This method searches through the products export, which may be slow
            for large inventories. Consider caching results.
        """
        try:
            product_id_int = int(product_id)
        except (ValueError, TypeError):
            logger.warning(f"Invalid product_id format: {product_id}")
            return None
        
        # Get all products and search for the specific one
        # Note: This is not ideal for large inventories, but CardTrader API
        # doesn't provide a direct GET /products/:id endpoint
        products = await self.get_products_export()
        
        for product in products:
            if product.get("id") == product_id_int:
                return product
        
        return None

    async def check_product_availability(
        self, product_id: str
    ) -> Dict[str, Any]:
        """
        Check if a product is available (quantity > 0) on CardTrader.
        
        Args:
            product_id: CardTrader product ID (as string)
            
        Returns:
            Dict with:
            - available: bool - Whether product is available
            - quantity: int - Current quantity (0 if not found)
            - product: Optional[Dict] - Full product object if found
            - error: Optional[str] - Error message if check failed
        """
        try:
            product = await self.get_product_by_id(product_id)
            
            if product is None:
                return {
                    "available": False,
                    "quantity": 0,
                    "product": None,
                    "error": f"Product {product_id} not found in inventory",
                }
            
            quantity = product.get("quantity", 0)
            available = quantity > 0
            
            return {
                "available": available,
                "quantity": quantity,
                "product": product,
                "error": None,
            }
        except Exception as e:
            logger.error(f"Error checking product availability for {product_id}: {e}")
            return {
                "available": False,
                "quantity": 0,
                "product": None,
                "error": f"Error checking availability: {str(e)}",
            }

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
