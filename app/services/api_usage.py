"""
API usage tracking and rate limit enforcement.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.models.models import APIUsage
from datetime import datetime, timedelta
import logging

log = logging.getLogger("api_usage")

# Rate limits (per month unless specified)
RATE_LIMITS = {
    "digikey": {"limit": None, "name": "DigiKey API", "unit": "unlimited"},
    "mouser": {"limit": None, "name": "Mouser API", "unit": "unlimited"},
    "brave": {"limit": 1000, "name": "Brave Search", "unit": "month"},
    "tavily": {"limit": 1000, "name": "Tavily AI Search", "unit": "month"},
    "serpapi": {"limit": 100, "name": "SerpAPI (Google)", "unit": "month"},
    "gemini": {"limit": None, "name": "Gemini AI", "unit": "unlimited"},
}


async def track_api_call(
    db: AsyncSession,
    api_name: str,
    endpoint: str = None,
    success: bool = True,
    error_message: str = None,
    response_time_ms: int = None,
):
    """Log an API call for usage tracking."""
    try:
        usage = APIUsage(
            api_name=api_name,
            endpoint=endpoint,
            success=success,
            error_message=error_message,
            response_time_ms=response_time_ms,
        )
        db.add(usage)
        await db.commit()
    except Exception as e:
        log.error(f"Failed to track API usage: {e}")


async def get_usage_stats(db: AsyncSession, api_name: str = None, days: int = 30) -> dict:
    """Get usage statistics for API(s)."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    query = select(
        APIUsage.api_name,
        func.count(APIUsage.id).label("total_calls"),
        func.sum(func.cast(APIUsage.success, Integer)).label("successful_calls"),
        func.avg(APIUsage.response_time_ms).label("avg_response_ms"),
    ).where(APIUsage.timestamp >= cutoff)
    
    if api_name:
        query = query.where(APIUsage.api_name == api_name)
    
    query = query.group_by(APIUsage.api_name)
    
    result = await db.execute(query)
    rows = result.all()
    
    stats = {}
    for row in rows:
        limit_info = RATE_LIMITS.get(row.api_name, {"limit": None, "name": row.api_name, "unit": "unknown"})
        stats[row.api_name] = {
            "name": limit_info["name"],
            "total_calls": row.total_calls,
            "successful_calls": row.successful_calls or 0,
            "avg_response_ms": int(row.avg_response_ms) if row.avg_response_ms else None,
            "limit": limit_info["limit"],
            "unit": limit_info["unit"],
            "percentage_used": (row.total_calls / limit_info["limit"] * 100) if limit_info["limit"] else None,
        }
    
    return stats


async def check_rate_limit(db: AsyncSession, api_name: str) -> tuple[bool, str]:
    """
    Check if API call is within rate limits.
    Returns (allowed: bool, reason: str)
    """
    limit_info = RATE_LIMITS.get(api_name)
    if not limit_info or not limit_info["limit"]:
        return True, ""
    
    # Count calls this month
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    result = await db.execute(
        select(func.count(APIUsage.id))
        .where(APIUsage.api_name == api_name)
        .where(APIUsage.timestamp >= month_start)
    )
    count = result.scalar() or 0
    
    if count >= limit_info["limit"]:
        return False, f"{limit_info['name']} monthly limit ({limit_info['limit']}) exceeded"
    
    return True, ""
