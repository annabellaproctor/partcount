"""
API usage statistics and rate limit monitoring.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import get_db
from app.services.api_usage import get_usage_stats, RATE_LIMITS

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/stats")
async def usage_stats(days: int = 30, db: AsyncSession = Depends(get_db)):
    """Get API usage statistics for the last N days."""
    stats = await get_usage_stats(db, days=days)
    
    # Include all configured APIs even if no usage yet
    for api_name, info in RATE_LIMITS.items():
        if api_name not in stats:
            stats[api_name] = {
                "name": info["name"],
                "total_calls": 0,
                "successful_calls": 0,
                "avg_response_ms": None,
                "limit": info["limit"],
                "unit": info["unit"],
                "percentage_used": 0 if info["limit"] else None,
            }
    
    return stats
