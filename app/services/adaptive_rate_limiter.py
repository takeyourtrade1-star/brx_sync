"""
Adaptive Rate Limiter - Enterprise-grade rate limiting with dynamic adjustment.
Monitors CardTrader 429 responses and automatically adjusts rate limits per user.
"""
import logging
import time
from typing import Optional, Tuple

from app.core.config import get_settings
from app.core.redis_client import get_redis_sync

settings = get_settings()
logger = logging.getLogger(__name__)


class AdaptiveRateLimiter:
    """
    Advanced rate limiter that adapts to CardTrader API behavior.
    
    Features:
    - Token Bucket algorithm (distributed via Redis)
    - Adaptive limit adjustment based on 429 responses
    - Per-user isolation
    - Burst handling
    - Statistics tracking
    """
    
    def __init__(
        self,
        base_requests: int = None,
        window_seconds: int = None,
    ):
        self.base_requests = base_requests or settings.RATE_LIMIT_REQUESTS
        self.window_seconds = window_seconds or settings.RATE_LIMIT_WINDOW_SECONDS
        self.redis = get_redis_sync()
        
        # Adaptive parameters
        self.min_factor = 0.5  # Minimum 50% of base limit
        self.max_factor = 1.5  # Maximum 150% of base limit
        self.reduction_factor = 0.9  # Reduce by 10% on 429
        self.increase_factor = 1.01  # Increase by 1% on success
        self.stats_window = 3600  # 1 hour for statistics
    
    def _get_key(self, user_id: str, suffix: str = "") -> str:
        """Get Redis key for user rate limit bucket."""
        base = f"rate_limit:{user_id}"
        return f"{base}:{suffix}" if suffix else base
    
    def check_and_consume(
        self, 
        user_id: str, 
        tokens: int = 1
    ) -> Tuple[bool, Optional[float]]:
        """
        Check if request is allowed and consume tokens.
        
        Args:
            user_id: User identifier
            tokens: Number of tokens to consume (default: 1)
            
        Returns:
            (allowed: bool, wait_seconds: Optional[float])
        """
        # Get adaptive factor for this user
        adaptive_factor = self._get_adaptive_factor(user_id)
        effective_limit = int(self.base_requests * adaptive_factor)
        
        key = self._get_key(user_id)
        now = time.time()
        
        try:
            # Use Lua script for atomic operations
            lua_script = """
            local key = KEYS[1]
            local max_tokens = tonumber(ARGV[1])
            local window_seconds = tonumber(ARGV[2])
            local now = tonumber(ARGV[3])
            local tokens_to_consume = tonumber(ARGV[4])
            
            local bucket = redis.call('HMGET', key, 'tokens', 'refill_time')
            local current_tokens = tonumber(bucket[1])
            local refill_time = tonumber(bucket[2])
            
            -- Initialize if not exists
            if not current_tokens then
                current_tokens = max_tokens
                refill_time = now + window_seconds
            end
            
            -- Refill if needed
            if now >= refill_time then
                current_tokens = max_tokens
                refill_time = now + window_seconds
            end
            
            -- Check if enough tokens
            if current_tokens >= tokens_to_consume then
                current_tokens = current_tokens - tokens_to_consume
                redis.call('HMSET', key, 'tokens', current_tokens, 'refill_time', refill_time)
                redis.call('EXPIRE', key, window_seconds * 2)
                return {1, 0, current_tokens}  -- allowed, wait_seconds, remaining_tokens
            else
                local wait_seconds = math.max(0, refill_time - now)
                return {0, wait_seconds, current_tokens}  -- not allowed, wait_seconds, remaining_tokens
            end
            """
            
            result = self.redis.eval(
                lua_script,
                1,  # num keys
                key,
                effective_limit,
                self.window_seconds,
                now,
                tokens
            )
            
            allowed = bool(result[0])
            wait_seconds = float(result[1]) if result[1] > 0 else None
            remaining_tokens = int(result[2])
            
            # Log if approaching limit
            if remaining_tokens < effective_limit * 0.1:
                logger.debug(
                    f"User {user_id} approaching rate limit: "
                    f"{remaining_tokens}/{effective_limit} tokens remaining"
                )
            
            return allowed, wait_seconds
            
        except Exception as e:
            logger.error(f"Rate limiter error for user {user_id}: {e}")
            # Fail open - allow request if Redis fails
            return True, None
    
    def record_429_response(self, user_id: str) -> None:
        """
        Record a 429 response and adjust rate limit downward.
        
        Args:
            user_id: User identifier
        """
        stats_key = self._get_key(user_id, "stats")
        
        # Increment 429 counter
        self.redis.incr(f"{stats_key}:429_count")
        self.redis.expire(f"{stats_key}:429_count", self.stats_window)
        
        # Record timestamp
        self.redis.lpush(f"{stats_key}:429_timestamps", time.time())
        self.redis.ltrim(f"{stats_key}:429_timestamps", 0, 100)  # Keep last 100
        self.redis.expire(f"{stats_key}:429_timestamps", self.stats_window)
        
        # Reduce adaptive factor
        current_factor = self._get_adaptive_factor(user_id)
        new_factor = max(self.min_factor, current_factor * self.reduction_factor)
        self._set_adaptive_factor(user_id, new_factor)
        
        logger.warning(
            f"User {user_id} received 429, reducing rate limit factor "
            f"from {current_factor:.2f} to {new_factor:.2f}"
        )
    
    def record_success(self, user_id: str) -> None:
        """
        Record a successful request and gradually increase rate limit.
        
        Args:
            user_id: User identifier
        """
        stats_key = self._get_key(user_id, "stats")
        
        # Increment success counter
        self.redis.incr(f"{stats_key}:success_count")
        self.redis.expire(f"{stats_key}:success_count", self.stats_window)
        
        # Gradually increase adaptive factor (only if no recent 429s)
        recent_429s = self._get_recent_429_count(user_id, window=300)  # Last 5 minutes
        if recent_429s == 0:
            current_factor = self._get_adaptive_factor(user_id)
            if current_factor < 1.0:  # Only increase if below base
                new_factor = min(self.max_factor, current_factor * self.increase_factor)
                self._set_adaptive_factor(user_id, new_factor)
    
    def _get_adaptive_factor(self, user_id: str) -> float:
        """Get current adaptive factor for user."""
        factor_key = self._get_key(user_id, "factor")
        factor = self.redis.get(factor_key)
        
        if factor is None:
            return 1.0  # Default: 100% of base limit
        
        return float(factor)
    
    def _set_adaptive_factor(self, user_id: str, factor: float) -> None:
        """Set adaptive factor for user."""
        factor_key = self._get_key(user_id, "factor")
        self.redis.setex(factor_key, self.stats_window, factor)
    
    def _get_recent_429_count(self, user_id: str, window: int = 300) -> int:
        """Get number of 429s in the last N seconds."""
        stats_key = self._get_key(user_id, "stats")
        timestamps_key = f"{stats_key}:429_timestamps"
        
        now = time.time()
        cutoff = now - window
        
        # Get all timestamps
        timestamps = self.redis.lrange(timestamps_key, 0, -1)
        
        # Count recent ones
        recent_count = sum(1 for ts in timestamps if float(ts) > cutoff)
        
        return recent_count
    
    def get_statistics(self, user_id: str) -> dict:
        """
        Get rate limiting statistics for user.
        
        Returns:
            Dict with statistics
        """
        stats_key = self._get_key(user_id, "stats")
        factor = self._get_adaptive_factor(user_id)
        effective_limit = int(self.base_requests * factor)
        
        stats = {
            "user_id": user_id,
            "base_limit": self.base_requests,
            "adaptive_factor": factor,
            "effective_limit": effective_limit,
            "429_count_1h": int(self.redis.get(f"{stats_key}:429_count") or 0),
            "success_count_1h": int(self.redis.get(f"{stats_key}:success_count") or 0),
            "recent_429s_5m": self._get_recent_429_count(user_id, window=300),
        }
        
        return stats
    
    def reset_user(self, user_id: str) -> None:
        """Reset rate limit state for user (admin/testing)."""
        pattern = f"rate_limit:{user_id}:*"
        keys = self.redis.keys(pattern)
        if keys:
            self.redis.delete(*keys)
        logger.info(f"Reset rate limit state for user {user_id}")


# Global instance
_adaptive_rate_limiter: Optional[AdaptiveRateLimiter] = None


def get_adaptive_rate_limiter() -> AdaptiveRateLimiter:
    """Get or create global adaptive rate limiter instance."""
    global _adaptive_rate_limiter
    if _adaptive_rate_limiter is None:
        _adaptive_rate_limiter = AdaptiveRateLimiter()
    return _adaptive_rate_limiter
