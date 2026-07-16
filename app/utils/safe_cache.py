"""
Safe caching utility that never stores error responses.
Auto-invalidates on HTTP error codes, exceptions, and bad data.
"""
import hashlib
import json
import logging
from typing import Any, Optional, Callable
from datetime import datetime, timedelta

log = logging.getLogger("safe_cache")

# HTTP status codes that should NEVER be cached
NEVER_CACHE_CODES = {
    400, 401, 403, 404,  # Client errors
    429,                  # Rate limit
    500, 502, 503, 504,  # Server errors
}

# Error response patterns in text that should never be cached
ERROR_RESPONSE_PATTERNS = [
    "i'm sorry",
    "i cannot",
    "i can't",
    "something went wrong",
    "an error occurred",
    "unable to",
    "failed to",
    "error:",
    "exception:",
]


def _contains_error_pattern(value: Any) -> bool:
    """Check if value contains error response patterns"""
    if isinstance(value, str):
        value_lower = value.lower()
        return any(pattern in value_lower for pattern in ERROR_RESPONSE_PATTERNS)
    
    if isinstance(value, dict):
        # Check all string values recursively
        for v in value.values():
            if _contains_error_pattern(v):
                return True
    
    if isinstance(value, list):
        for item in value:
            if _contains_error_pattern(item):
                return True
    
    return False


class SafeCache:
    """
    In-memory cache that automatically invalidates bad responses.
    Prevents caching of errors, rate limits, or malformed data.
    """
    
    def __init__(self, default_ttl: int = 3600):
        """
        Args:
            default_ttl: Default time-to-live in seconds (default 1 hour)
        """
        self._store = {}  # {key: {value, expires_at, status_code}}
        self._default_ttl = default_ttl
    
    def _make_key(self, *parts: Any) -> str:
        """Create cache key from parts"""
        key_str = ":".join(str(p) for p in parts)
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, *key_parts: Any) -> Optional[Any]:
        """
        Get cached value if valid.
        
        Returns:
            Cached value or None if expired/missing
        """
        key = self._make_key(*key_parts)
        
        if key not in self._store:
            return None
        
        entry = self._store[key]
        
        # Check expiration
        if datetime.utcnow() > entry['expires_at']:
            log.debug(f"Cache expired: {key[:16]}...")
            del self._store[key]
            return None
        
        # Check if it's an error response that should have been cleaned
        if entry.get('status_code') in NEVER_CACHE_CODES:
            log.warning(f"Found cached error response (code {entry['status_code']}), invalidating")
            del self._store[key]
            return None
        
        log.debug(f"Cache hit: {key[:16]}...")
        return entry['value']
    
    def set(
        self, 
        *key_parts: Any, 
        value: Any, 
        ttl: Optional[int] = None,
        status_code: Optional[int] = None
    ) -> bool:
        """
        Store value in cache if it's not an error response.
        
        Args:
            key_parts: Parts to construct cache key
            value: Value to cache
            ttl: Time-to-live in seconds (None = use default)
            status_code: HTTP status code if this is from API response
        
        Returns:
            True if cached, False if rejected (error code)
        """
        # CRITICAL: Never cache error responses
        if status_code and status_code in NEVER_CACHE_CODES:
            log.warning(f"Refusing to cache error response (code {status_code})")
            return False
        
        # Never cache None or empty values
        if value is None:
            log.debug("Refusing to cache None value")
            return False
        
        # Never cache error-like data
        if isinstance(value, dict):
            if 'error' in value or 'Error' in value:
                log.warning("Refusing to cache error dict")
                return False
        
        # CRITICAL: Check for error response patterns in content
        if _contains_error_pattern(value):
            log.warning(f"Refusing to cache value containing error pattern")
            return False
        
        key = self._make_key(*key_parts)
        ttl = ttl if ttl is not None else self._default_ttl
        
        self._store[key] = {
            'value': value,
            'expires_at': datetime.utcnow() + timedelta(seconds=ttl),
            'status_code': status_code,
            'cached_at': datetime.utcnow(),
        }
        
        log.debug(f"Cache set: {key[:16]}... (ttl={ttl}s)")
        return True
    
    def delete(self, *key_parts: Any) -> bool:
        """
        Delete cache entry.
        
        Returns:
            True if deleted, False if not found
        """
        key = self._make_key(*key_parts)
        
        if key in self._store:
            log.debug(f"Cache invalidated: {key[:16]}...")
            del self._store[key]
            return True
        
        return False
    
    def invalidate_pattern(self, pattern: str) -> int:
        """
        Invalidate all keys matching pattern.
        
        Args:
            pattern: String that key must contain
        
        Returns:
            Number of keys deleted
        """
        to_delete = [
            k for k in self._store.keys()
            if pattern in k
        ]
        
        for k in to_delete:
            del self._store[k]
        
        if to_delete:
            log.info(f"Invalidated {len(to_delete)} keys matching '{pattern}'")
        
        return len(to_delete)
    
    def clear(self) -> int:
        """
        Clear entire cache.
        
        Returns:
            Number of keys deleted
        """
        count = len(self._store)
        self._store.clear()
        log.info(f"Cache cleared ({count} entries)")
        return count
    
    def cleanup_expired(self) -> int:
        """
        Remove expired entries.
        
        Returns:
            Number of entries removed
        """
        now = datetime.utcnow()
        expired = [
            k for k, v in self._store.items()
            if now > v['expires_at']
        ]
        
        for k in expired:
            del self._store[k]
        
        if expired:
            log.debug(f"Cleaned up {len(expired)} expired entries")
        
        return len(expired)
    
    def stats(self) -> dict:
        """Get cache statistics"""
        return {
            'total_entries': len(self._store),
            'oldest_entry': min(
                (v['cached_at'] for v in self._store.values()),
                default=None
            ),
            'newest_entry': max(
                (v['cached_at'] for v in self._store.values()),
                default=None
            ),
        }


# Global cache instance
cache = SafeCache(default_ttl=3600)


async def cached_api_call(
    func: Callable,
    *key_parts: Any,
    ttl: Optional[int] = None,
    force_refresh: bool = False,
    **kwargs
) -> Any:
    """
    Wrapper for API calls with safe caching.
    
    Args:
        func: Async function to call
        key_parts: Cache key components
        ttl: Time-to-live for cache
        force_refresh: Skip cache and fetch fresh
        **kwargs: Passed to func
    
    Returns:
        Result from func (cached or fresh)
    """
    # Check cache unless forced refresh
    if not force_refresh:
        cached = cache.get(*key_parts)
        if cached is not None:
            return cached
    
    # Call function
    try:
        result = await func(**kwargs)
        
        # Extract status code if present
        status_code = None
        if isinstance(result, dict):
            status_code = result.get('status_code') or result.get('statusCode')
        
        # Cache if successful
        cache.set(*key_parts, value=result, ttl=ttl, status_code=status_code)
        
        return result
        
    except Exception as e:
        log.error(f"API call failed: {e}")
        # Make sure we don't have stale cached errors
        cache.delete(*key_parts)
        raise
