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
_TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local max_tokens = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local bucket = redis.call('HMGET', key, 'tokens', 'refill_time')
local tokens = tonumber(bucket[1])
local refill_time = tonumber(bucket[2])
if not tokens or not refill_time or now >= refill_time then
  tokens = max_tokens
  refill_time = now + window_seconds
end
if tokens > 0 then
  tokens = tokens - 1
  redis.call('HSET', key, 'tokens', tokens, 'refill_time', refill_time)
  redis.call('EXPIRE', key, math.ceil(window_seconds * 2))
  return {1, 0}
end
return {0, math.max(0, refill_time - now)}
"""


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
            allowed, wait_seconds = self.redis.eval(
                _TOKEN_BUCKET_SCRIPT,
                1,
                key,
                self.requests,
                self.window_seconds,
                now,
            )
            return bool(allowed), float(wait_seconds) if wait_seconds else None
                
        except Exception as e:
            logger.error("CardTrader rate limiter unavailable")
            return False, float(self.window_seconds)

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
