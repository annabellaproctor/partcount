"""
Component lookup endpoint — searches DigiKey then LCSC as fallback.
Results are cached in component_lookups table.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from app.models.database import get_db
from app.services import digikey, lcsc
import json, uuid
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/lookup", tags=["lookup"])


@router.get("/debug")
async def debug_lookup(q: str = "esp32"):
    from app.services.digikey import debug_raw
    return await debug_raw(q)

CACHE_TTL_HOURS = 24


@router.get("/search")
async def lookup_search(
    q: str = Query(..., min_length=2),
    source: str = Query("auto"),  # auto | digikey | lcsc
    db: AsyncSession = Depends(get_db),
):
    cache_key = f"{source}:{q.lower().strip()}"

    # check cache
    try:
        cached = await db.execute(
            text("SELECT result_json, fetched_at FROM component_lookups WHERE query = :q AND source = :s ORDER BY fetched_at DESC LIMIT 1"),
            {"q": cache_key, "s": source}
        )
        row = cached.fetchone()
        if row:
            age = datetime.utcnow() - row.fetched_at
            if age < timedelta(hours=CACHE_TTL_HOURS):
                return {"results": json.loads(row.result_json), "source": source, "cached": True}
    except Exception:
        pass

    results = []

    if source in ("auto", "digikey"):
        results = await digikey.search(q, limit=10)

    if not results and source in ("auto", "lcsc"):
        results = await lcsc.search(q, limit=10)
        if results:
            source = "lcsc"

    if not results and source == "auto":
        source = "none"

    # cache result
    if results:
        try:
            await db.execute(
                text("INSERT INTO component_lookups (id, query, source, result_json, fetched_at) VALUES (:id, :q, :s, :r, :t)"),
                {"id": str(uuid.uuid4()), "q": cache_key, "s": source, "r": json.dumps(results), "t": datetime.utcnow()}
            )
        except Exception:
            pass

    return {"results": results, "source": source, "cached": False}


@router.get("/part")
async def lookup_part(
    pn: str = Query(...),
    source: str = Query("digikey"),
    db: AsyncSession = Depends(get_db),
):
    """Get full details for a specific part number"""
    cache_key = f"detail:{pn}"
    try:
        cached = await db.execute(
            text("SELECT result_json, fetched_at FROM component_lookups WHERE query = :q AND source = :s ORDER BY fetched_at DESC LIMIT 1"),
            {"q": cache_key, "s": source}
        )
        row = cached.fetchone()
        if row:
            age = datetime.utcnow() - row.fetched_at
            if age < timedelta(hours=CACHE_TTL_HOURS * 7):
                return {"result": json.loads(row.result_json), "cached": True}
    except Exception:
        pass

    result = None
    if source == "digikey":
        result = await digikey.get_part(pn)
    elif source == "lcsc":
        result = await lcsc.get_part(pn)

    if result:
        try:
            await db.execute(
                text("INSERT INTO component_lookups (id, query, source, result_json, fetched_at) VALUES (:id, :q, :s, :r, :t)"),
                {"id": str(uuid.uuid4()), "q": cache_key, "s": source, "r": json.dumps(result), "t": datetime.utcnow()}
            )
        except Exception:
            pass

    return {"result": result, "cached": False}
