"""
Unit tests for rate limiter service.
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.rate_limiter import RateLimiter


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis_mock = MagicMock()
    redis_mock.hgetall.return_value = {}
    redis_mock.hset.return_value = True
    redis_mock.expire.return_value = True
    redis_mock.delete.return_value = True
    redis_mock.eval.return_value = [1, 0]
    return redis_mock


@patch("app.services.rate_limiter.get_redis_sync")
def test_rate_limiter_initial_consume(mock_get_redis, mock_redis):
    """Test initial token consumption."""
    mock_get_redis.return_value = mock_redis
    
    limiter = RateLimiter(requests=200, window_seconds=10)
    allowed, wait_seconds = limiter.check_and_consume("user123")
    
    assert allowed is True
    assert wait_seconds is None
    assert mock_redis.eval.called


@patch("app.services.rate_limiter.get_redis_sync")
def test_rate_limiter_rate_limited(mock_get_redis, mock_redis):
    """Test rate limiting when tokens exhausted."""
    mock_get_redis.return_value = mock_redis
    
    mock_redis.eval.return_value = [0, 5]
    
    limiter = RateLimiter(requests=200, window_seconds=10)
    allowed, wait_seconds = limiter.check_and_consume("user123")
    
    assert allowed is False
    assert wait_seconds is not None
    assert wait_seconds > 0


@patch("app.services.rate_limiter.get_redis_sync")
def test_rate_limiter_reset(mock_get_redis, mock_redis):
    """Test rate limiter reset."""
    mock_get_redis.return_value = mock_redis
    
    limiter = RateLimiter()
    limiter.reset("user123")
    
    assert mock_redis.delete.called


@patch("app.services.rate_limiter.get_redis_sync")
def test_rate_limiter_fails_closed_when_redis_is_unavailable(mock_get_redis, mock_redis):
    mock_redis.eval.side_effect = ConnectionError("redis unavailable")
    mock_get_redis.return_value = mock_redis

    allowed, wait_seconds = RateLimiter(requests=200, window_seconds=10).check_and_consume(
        "user123"
    )

    assert allowed is False
    assert wait_seconds == 10
