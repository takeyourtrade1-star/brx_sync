"""
Circuit Breaker Pattern for CardTrader API.
Prevents cascading failures when external service is down or overloaded.
"""
import logging
import time
from enum import Enum
from typing import Callable, Any, Optional

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
        self.circuit_key = "circuit_breaker:cardtrader"
    
    def get_state(self) -> CircuitState:
        """Get current circuit state."""
        state_str = self.redis.get(f"{self.circuit_key}:state")
        
        if state_str is None:
            return CircuitState.CLOSED
        
        state_str = state_str.decode() if isinstance(state_str, bytes) else state_str
        return CircuitState(state_str)
    
    def set_state(self, state: CircuitState) -> None:
        """Set circuit state."""
        self.redis.setex(
            f"{self.circuit_key}:state",
            self.timeout * 2,  # Expire after 2x timeout
            state.value
        )
        logger.info(f"Circuit breaker state changed to {state.value}")
    
    def record_failure(self, error_type: str = "generic") -> None:
        """Record a failure and potentially open circuit."""
        failures_key = f"{self.circuit_key}:failures"
        
        # Increment failure counter
        failures = self.redis.incr(failures_key)
        self.redis.expire(failures_key, self.timeout)
        
        # Record failure timestamp
        self.redis.lpush(f"{self.circuit_key}:failure_timestamps", time.time())
        self.redis.ltrim(f"{self.circuit_key}:failure_timestamps", 0, 100)
        self.redis.expire(f"{self.circuit_key}:failure_timestamps", self.timeout)
        
        # Record error type
        self.redis.hincrby(f"{self.circuit_key}:error_types", error_type, 1)
        self.redis.expire(f"{self.circuit_key}:error_types", self.timeout)
        
        logger.warning(
            f"Circuit breaker: failure recorded ({failures}/{self.failure_threshold}), "
            f"error_type={error_type}"
        )
        
        # Open circuit if threshold reached
        if failures >= self.failure_threshold:
            self.set_state(CircuitState.OPEN)
            self.redis.setex(
                f"{self.circuit_key}:opened_at",
                self.timeout,
                time.time()
            )
            logger.error(
                f"Circuit breaker OPENED after {failures} failures. "
                f"Will attempt recovery in {self.timeout} seconds."
            )
    
    def record_success(self) -> None:
        """Record a success and potentially close circuit."""
        # Clear failure counter
        self.redis.delete(f"{self.circuit_key}:failures")
        
        state = self.get_state()
        
        if state == CircuitState.HALF_OPEN:
            # Increment success counter
            successes_key = f"{self.circuit_key}:successes"
            successes = self.redis.incr(successes_key)
            self.redis.expire(successes_key, self.half_open_timeout)
            
            logger.info(
                f"Circuit breaker HALF_OPEN: success recorded "
                f"({successes}/{self.success_threshold})"
            )
            
            # Close circuit if threshold reached
            if successes >= self.success_threshold:
                self.set_state(CircuitState.CLOSED)
                self.redis.delete(successes_key)
                logger.info("Circuit breaker CLOSED - service recovered")
        elif state == CircuitState.CLOSED:
            # Reset any lingering failure counts
            self.redis.delete(f"{self.circuit_key}:failures")
    
    def should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset from OPEN to HALF_OPEN."""
        if self.get_state() != CircuitState.OPEN:
            return False
        
        opened_at_key = f"{self.circuit_key}:opened_at"
        opened_at = self.redis.get(opened_at_key)
        
        if opened_at is None:
            # No record of when it opened, allow reset
            return True
        
        opened_at = float(opened_at)
        elapsed = time.time() - opened_at
        
        return elapsed >= self.timeout
    
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
            error_type = "rate_limit" if "429" in str(e) or "rate limit" in str(e).lower() else "generic"
            self.record_failure(error_type)
            raise
    
    def get_statistics(self) -> dict:
        """Get circuit breaker statistics."""
        state = self.get_state()
        failures = int(self.redis.get(f"{self.circuit_key}:failures") or 0)
        successes = int(self.redis.get(f"{self.circuit_key}:successes") or 0)
        
        opened_at = self.redis.get(f"{self.circuit_key}:opened_at")
        opened_at_ts = float(opened_at) if opened_at else None
        
        error_types = self.redis.hgetall(f"{self.circuit_key}:error_types")
        error_types = {
            k.decode() if isinstance(k, bytes) else k: int(v)
            for k, v in error_types.items()
        }
        
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
        keys = self.redis.keys(f"{self.circuit_key}:*")
        if keys:
            self.redis.delete(*keys)
        self.set_state(CircuitState.CLOSED)
        logger.info("Circuit breaker manually reset to CLOSED")


# Global instance
_circuit_breaker: Optional[CardTraderCircuitBreaker] = None


def get_circuit_breaker() -> CardTraderCircuitBreaker:
    """Get or create global circuit breaker instance."""
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = CardTraderCircuitBreaker()
    return _circuit_breaker
