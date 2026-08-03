"""
Circuit Breaker Pattern for CardTrader API.
Prevents cascading failures when external service is down or overloaded.
"""

import logging
import time
from enum import Enum
from typing import Any, Callable, Dict

from app.core.redis_client import get_redis_sync

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "CLOSED"  # Normal operation
    OPEN = "OPEN"  # Failing, reject requests
    HALF_OPEN = "HALF_OPEN"  # Testing if service recovered


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is OPEN."""

    pass


class CardTraderCircuitBreaker:
    """
    Circuit Breaker for CardTrader API.

    Prevents making requests when:
    - Service is down
    - Service is overloaded (many 429s)
    - Error rate exceeds threshold

    Automatically recovers when service is healthy again.
    """

    def __init__(
        self,
        user_id: str,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout: int = 60,
        half_open_timeout: int = 30,
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures to open circuit
            success_threshold: Number of successes to close circuit (from HALF_OPEN)
            timeout: Seconds to wait before attempting HALF_OPEN
            half_open_timeout: Seconds to wait in HALF_OPEN before opening again
        """
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.timeout = timeout
        self.half_open_timeout = half_open_timeout
        self.redis = get_redis_sync()
        self.user_id = str(user_id)
        self.circuit_key = f"circuit_breaker:cardtrader:{self.user_id}"

    def get_state(self) -> CircuitState:
        """Get current circuit state."""
        try:
            state_str = self.redis.get(f"{self.circuit_key}:state")
        except Exception as exc:
            logger.warning(
                "Circuit breaker state unavailable for user %s: %s",
                self.user_id,
                exc,
            )
            return CircuitState.CLOSED

        if state_str is None:
            return CircuitState.CLOSED
        try:
            state_str = state_str.decode() if isinstance(state_str, bytes) else state_str
            return CircuitState(state_str)
        except (UnicodeDecodeError, ValueError):
            logger.warning(
                "Invalid circuit breaker state for user %s; failing open",
                self.user_id,
            )
            return CircuitState.CLOSED

    def set_state(self, state: CircuitState) -> None:
        """Set circuit state."""
        try:
            self.redis.setex(
                f"{self.circuit_key}:state",
                self.timeout * 2,
                state.value,
            )
            logger.info(
                "Circuit breaker state for user %s changed to %s",
                self.user_id,
                state.value,
            )
        except Exception as exc:
            logger.warning(
                "Unable to persist circuit breaker state for user %s: %s",
                self.user_id,
                exc,
            )

    def record_failure(self, error_type: str = "generic") -> None:
        """Record a failure and potentially open circuit."""
        failures_key = f"{self.circuit_key}:failures"
        try:
            failures = self.redis.incr(failures_key)
            self.redis.expire(failures_key, self.timeout)
            self.redis.lpush(f"{self.circuit_key}:failure_timestamps", time.time())
            self.redis.ltrim(f"{self.circuit_key}:failure_timestamps", 0, 100)
            self.redis.expire(
                f"{self.circuit_key}:failure_timestamps",
                self.timeout,
            )
            self.redis.hincrby(f"{self.circuit_key}:error_types", error_type, 1)
            self.redis.expire(f"{self.circuit_key}:error_types", self.timeout)
            logger.warning(
                "Circuit breaker user=%s failure=%s/%s type=%s",
                self.user_id,
                failures,
                self.failure_threshold,
                error_type,
            )
            if failures >= self.failure_threshold:
                self.set_state(CircuitState.OPEN)
                self.redis.setex(
                    f"{self.circuit_key}:opened_at",
                    self.timeout,
                    time.time(),
                )
                logger.error(
                    "Circuit breaker OPENED for user %s after %s failures",
                    self.user_id,
                    failures,
                )
        except Exception as exc:
            logger.warning(
                "Unable to record circuit failure for user %s: %s",
                self.user_id,
                exc,
            )

    def record_success(self) -> None:
        """Record a success and potentially close circuit."""
        try:
            state = self.get_state()
            self.redis.delete(f"{self.circuit_key}:failures")
            if state == CircuitState.HALF_OPEN:
                successes_key = f"{self.circuit_key}:successes"
                successes = self.redis.incr(successes_key)
                self.redis.expire(successes_key, self.half_open_timeout)
                if successes >= self.success_threshold:
                    self.set_state(CircuitState.CLOSED)
                    self.redis.delete(successes_key)
                    self.redis.delete(f"{self.circuit_key}:opened_at")
                    logger.info(
                        "Circuit breaker CLOSED for user %s",
                        self.user_id,
                    )
            elif state == CircuitState.CLOSED:
                self.redis.delete(f"{self.circuit_key}:successes")
        except Exception as exc:
            logger.warning(
                "Unable to record circuit success for user %s: %s",
                self.user_id,
                exc,
            )

    def should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset from OPEN to HALF_OPEN."""
        if self.get_state() != CircuitState.OPEN:
            return False

        try:
            opened_at = self.redis.get(f"{self.circuit_key}:opened_at")
        except Exception as exc:
            logger.warning(
                "Circuit reset timestamp unavailable for user %s: %s",
                self.user_id,
                exc,
            )
            return True
        if opened_at is None:
            return True

        try:
            return time.time() - float(opened_at) >= self.timeout
        except (TypeError, ValueError):
            return True

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Function to execute
            *args, **kwargs: Function arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is OPEN
        """
        state = self.get_state()

        # Check if we should attempt reset
        if state == CircuitState.OPEN:
            if self.should_attempt_reset():
                logger.info("Attempting circuit breaker reset to HALF_OPEN")
                self.set_state(CircuitState.HALF_OPEN)
                # Reset success counter
                self.redis.delete(f"{self.circuit_key}:successes")
            else:
                raise CircuitBreakerOpenError(
                    f"Circuit breaker is OPEN. "
                    f"Service unavailable. Retry in {self.timeout} seconds."
                )

        # Execute function
        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            # Determine error type
            error_type = (
                "rate_limit" if "429" in str(e) or "rate limit" in str(e).lower() else "generic"
            )
            self.record_failure(error_type)
            raise

    def get_statistics(self) -> dict:
        """Get circuit breaker statistics."""
        state = self.get_state()
        try:
            failures = int(self.redis.get(f"{self.circuit_key}:failures") or 0)
            successes = int(self.redis.get(f"{self.circuit_key}:successes") or 0)
            opened_at = self.redis.get(f"{self.circuit_key}:opened_at")
            opened_at_ts = float(opened_at) if opened_at else None
            raw_error_types = self.redis.hgetall(f"{self.circuit_key}:error_types")
            error_types = {
                k.decode() if isinstance(k, bytes) else k: int(v)
                for k, v in raw_error_types.items()
            }
        except Exception:
            failures = 0
            successes = 0
            opened_at_ts = None
            error_types = {}

        return {
            "state": state.value,
            "failures": failures,
            "successes": successes,
            "failure_threshold": self.failure_threshold,
            "success_threshold": self.success_threshold,
            "opened_at": opened_at_ts,
            "time_since_open": time.time() - opened_at_ts if opened_at_ts else None,
            "error_types": error_types,
        }

    def reset(self) -> None:
        """Reset circuit breaker to CLOSED state (admin/testing)."""
        try:
            keys = self.redis.keys(f"{self.circuit_key}:*")
            if keys:
                self.redis.delete(*keys)
            self.set_state(CircuitState.CLOSED)
            logger.info(
                "Circuit breaker manually reset for user %s",
                self.user_id,
            )
        except Exception as exc:
            logger.warning(
                "Unable to reset circuit breaker for user %s: %s",
                self.user_id,
                exc,
            )


# One breaker per CardTrader account. A bad token or failing account must never
# stop mutations for every other seller.
_circuit_breakers: Dict[str, CardTraderCircuitBreaker] = {}


def get_circuit_breaker(user_id: str) -> CardTraderCircuitBreaker:
    """Get or create the circuit breaker for one CardTrader account."""
    key = str(user_id)
    breaker = _circuit_breakers.get(key)
    if breaker is None:
        breaker = CardTraderCircuitBreaker(key)
        _circuit_breakers[key] = breaker
    return breaker
