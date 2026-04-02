"""
API usage statistics and rate limit monitoring.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta
from app.models.database import get_db
from app.models.models import APIUsage
from app.services.api_usage import get_usage_stats, RATE_LIMITS

router = APIRouter(prefix="/api/usage", tags=["usage"])

# Guard to avoid repeatedly marking the same quiet window as "checked".
_last_gemini_quiet_checked_for_call_at: datetime | None = None


@router.get("/stats")
async def usage_stats(days: int = 30, db: AsyncSession = Depends(get_db)):
    """Get API usage statistics for the last N days."""
    global _last_gemini_quiet_checked_for_call_at
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

    # Gemini delayed refresh metadata:
    # Google-side usage can lag; only mark ready after 11 minutes of AI inactivity,
    # and only once per last call window.
    gem_row = await db.execute(
        select(APIUsage.timestamp)
        .where(APIUsage.api_name == "gemini")
        .order_by(APIUsage.timestamp.desc())
        .limit(1)
    )
    last_call = gem_row.scalar_one_or_none()
    now = datetime.utcnow()
    refresh_after = timedelta(minutes=11)

    refresh_ready = False
    next_refresh_at = None
    if last_call:
      next_refresh_at = last_call + refresh_after
      quiet_elapsed = now >= next_refresh_at
      already_checked = _last_gemini_quiet_checked_for_call_at == last_call
      if quiet_elapsed and not already_checked:
          refresh_ready = True
          _last_gemini_quiet_checked_for_call_at = last_call

    gem = stats.get("gemini", {
        "name": "Gemini AI",
        "total_calls": 0,
        "successful_calls": 0,
        "avg_response_ms": None,
        "limit": None,
        "unit": "unlimited",
        "percentage_used": None,
    })
    gem["last_call_at"] = last_call.isoformat() if last_call else None
    gem["next_refresh_at"] = next_refresh_at.isoformat() if next_refresh_at else None
    gem["refresh_ready"] = refresh_ready
    gem["quiet_window_minutes"] = 11
    stats["gemini"] = gem
    
    return stats
