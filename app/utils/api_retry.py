"""
API retry utility with exponential backoff and cache invalidation.
Handles 429 rate limits, bad response caching, and provider-specific quirks.
"""
import asyncio
import logging
from typing import Optional, Callable, Any
from datetime import datetime, timedelta

log = logging.getLogger("api_retry")

class APIRetryConfig:
    """Configuration for API retry behavior"""
    max_retries: int = 3
    base_delay: float = 1.0  # seconds
    max_delay: float = 10.0  # seconds
    timeout: float = 30.0  # total timeout for all retries
    
    # Error codes that should invalidate cache
    cache_invalidation_codes = [400, 401, 403, 404, 429, 500, 502, 503, 504]


async def retry_with_backoff(
    func: Callable,
    *args,
    config: Optional[APIRetryConfig] = None,
    on_error: Optional[Callable] = None,
    **kwargs
) -> Any:
    """
    Execute function with exponential backoff on failure.
    
    Args:
        func: Async function to call
        config: Retry configuration (uses defaults if None)
        on_error: Optional callback(attempt, error, wait_time) called on each retry
        *args, **kwargs: Passed to func
    
    Returns:
        Result from func
        
    Raises:
        Last exception if all retries exhausted
    """
    if config is None:
        config = APIRetryConfig()
    
    start_time = datetime.utcnow()
    last_error = None
    
    for attempt in range(config.max_retries):
        try:
            # Check total timeout
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            if elapsed > config.timeout:
                log.warning(f"Total timeout ({config.timeout}s) exceeded")
                raise TimeoutError(f"API call timeout after {elapsed:.1f}s")
            
            # Call function
            result = await func(*args, **kwargs)
            
            # Success - return immediately
            if attempt > 0:
                log.info(f"API call succeeded on attempt {attempt + 1}")
            return result
            
        except Exception as e:
            last_error = e
            
            # Don't retry on last attempt
            if attempt >= config.max_retries - 1:
                break
            
            # Calculate backoff delay (exponential with jitter)
            delay = min(
                config.base_delay * (2 ** attempt),
                config.max_delay
            )
            
            # Add 10% jitter to avoid thundering herd
            import random
            delay *= (0.9 + random.random() * 0.2)
            
            log.warning(
                f"API call failed (attempt {attempt + 1}/{config.max_retries}): {str(e)[:100]}. "
                f"Retrying in {delay:.1f}s"
            )
            
            # Call error callback if provided
            if on_error:
                try:
                    await on_error(attempt, e, delay)
                except Exception as cb_error:
                    log.error(f"Error callback failed: {cb_error}")
            
            # Wait before retry
            await asyncio.sleep(delay)
    
    # All retries exhausted
    log.error(f"API call failed after {config.max_retries} attempts: {last_error}")
    raise last_error


def should_invalidate_cache(status_code: int) -> bool:
    """Check if HTTP status code should invalidate cached data"""
    return status_code in APIRetryConfig.cache_invalidation_codes


async def invalidate_bad_cache(
    cache_key: str,
    cache_store: Any,
    status_code: int
) -> None:
    """
    Invalidate cache entry if it contains error response.
    
    Args:
        cache_key: Key to invalidate
        cache_store: Cache object with delete() method
        status_code: HTTP status code from response
    """
    if should_invalidate_cache(status_code):
        try:
            await cache_store.delete(cache_key)
            log.info(f"Invalidated bad cache for key {cache_key[:50]}... (status {status_code})")
        except Exception as e:
            log.error(f"Failed to invalidate cache: {e}")


class RateLimitTracker:
    """Track API rate limit usage across providers"""
    
    def __init__(self):
        self._limits = {}  # {provider: {window_start, count, limit}}
        self._last_check = {}  # {provider: datetime}
    
    def should_check_usage(self, provider: str, check_interval_hours: int = 2) -> bool:
        """Check if we should query provider API for usage stats"""
        last = self._last_check.get(provider)
        if not last:
            return True
        
        elapsed = (datetime.utcnow() - last).total_seconds() / 3600
        return elapsed >= check_interval_hours
    
    def record_check(self, provider: str):
        """Record that we just checked usage"""
        self._last_check[provider] = datetime.utcnow()
    
    def update_limits(self, provider: str, used: int, limit: int):
        """Update rate limit tracking"""
        self._limits[provider] = {
            'window_start': datetime.utcnow(),
            'count': used,
            'limit': limit,
            'percentage': (used / limit * 100) if limit else 0
        }
    
    def get_status(self, provider: str) -> dict:
        """Get current rate limit status"""
        return self._limits.get(provider, {
            'count': 0,
            'limit': None,
            'percentage': 0
        })
    
    def is_approaching_limit(self, provider: str, threshold_pct: float = 80.0) -> bool:
        """Check if we're approaching rate limit"""
        status = self.get_status(provider)
        return status.get('percentage', 0) > threshold_pct


# Global rate limit tracker
rate_tracker = RateLimitTracker()
