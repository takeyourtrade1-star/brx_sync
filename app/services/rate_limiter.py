"""
Distributed rate limiter using Redis Token Bucket algorithm.
Each user_id has a bucket of 200 tokens that refill every 10 seconds.
"""
import logging
import time
from typing import Optional, Tuple

from app.core.config import get_settings
from app.core.redis_client import get_redis_sync

settings = get_settings()
logger = logging.getLogger(__name__)


class RateLimiter:
    """Token Bucket rate limiter for CardTrader API calls."""

    def __init__(
        self,
        requests: int = None,
        window_seconds: int = None,
    ):
        self.requests = requests or settings.RATE_LIMIT_REQUESTS
        self.window_seconds = window_seconds or settings.RATE_LIMIT_WINDOW_SECONDS
        self.redis = get_redis_sync()

    def _get_key(self, user_id: str) -> str:
        """Get Redis key for user rate limit bucket."""
        return f"rate_limit:{user_id}"

    def check_and_consume(self, user_id: str) -> tuple[bool, Optional[float]]:
        """
        Check if request is allowed and consume a token.
        
        Returns:
            (allowed: bool, wait_seconds: Optional[float])
            - allowed: True if request can proceed, False if rate limited
            - wait_seconds: Seconds to wait before retry (None if allowed)
        """
        key = self._get_key(user_id)
        now = time.time()
        
        try:
            # Use Redis pipeline for atomic operations
            pipe = self.redis.pipeline()
            
            # Get current bucket state
            pipe.hgetall(key)
            pipe.execute()
            
            # Get or initialize bucket
            bucket_data = self.redis.hgetall(key)
            
            if not bucket_data:
                # Initialize new bucket
                tokens = self.requests - 1
                refill_time = now + self.window_seconds
                self.redis.hset(
                    key,
                    mapping={
                        "tokens": tokens,
                        "refill_time": refill_time,
                    }
                )
                self.redis.expire(key, int(self.window_seconds * 2))
                return True, None
            
            # Parse bucket data
            tokens = int(bucket_data.get("tokens", 0))
            refill_time = float(bucket_data.get("refill_time", now))
            
            # Check if bucket needs refill
            if now >= refill_time:
                # Refill bucket
                tokens = self.requests - 1
                refill_time = now + self.window_seconds
                self.redis.hset(
                    key,
                    mapping={
                        "tokens": tokens,
                        "refill_time": refill_time,
                    }
                )
                self.redis.expire(key, int(self.window_seconds * 2))
                return True, None
            
            # Check if tokens available
            if tokens > 0:
                # Consume token
                tokens -= 1
                self.redis.hset(key, "tokens", tokens)
                return True, None
            else:
                # Rate limited - calculate wait time
                wait_seconds = refill_time - now
                return False, wait_seconds
                
        except Exception as e:
            logger.error(f"Rate limiter error for user {user_id}: {e}")
            # Fail open - allow request if Redis fails
            return True, None

    def get_wait_time(self, user_id: str) -> float:
        """Get seconds to wait before next request is allowed."""
        key = self._get_key(user_id)
        bucket_data = self.redis.hgetall(key)
        
        if not bucket_data:
            return 0.0
        
        refill_time = float(bucket_data.get("refill_time", 0))
        now = time.time()
        
        if now >= refill_time:
            return 0.0
        
        return max(0.0, refill_time - now)

    def reset(self, user_id: str) -> None:
        """Reset rate limit bucket for user (for testing/admin)."""
        key = self._get_key(user_id)
        self.redis.delete(key)


# Global instance
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get or create global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter
