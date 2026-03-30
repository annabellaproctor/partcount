"""
Component lookup — DigiKey then LCSC fallback.
Cache in DB, TTL 24h.
Shift+Enter or force=true bypasses cache if last search was < 60s ago.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.models.database import get_db
from app.services import digikey, lcsc
import json, uuid
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/lookup", tags=["lookup"])
CACHE_TTL = timedelta(hours=24)
FORCE_WINDOW = timedelta(seconds=60)  # if last search < 60s ago and force=True, bypass cache


@router.get("/search")
async def lookup_search(
    q: str = Query(..., min_length=2),
    source: str = Query("auto"),
    force: bool = Query(False),  # Shift+Enter sets this
    db: AsyncSession = Depends(get_db),
):
    cache_key = f"{source}:{q.lower().strip()}"

    if not force:
        try:
            cached = await db.execute(
                text("SELECT result_json, fetched_at FROM component_lookups WHERE query = :q AND source = :s ORDER BY fetched_at DESC LIMIT 1"),
                {"q": cache_key, "s": source}
            )
            row = cached.fetchone()
            if row and row.fetched_at:
                age = datetime.utcnow() - row.fetched_at
                if age < CACHE_TTL:
                    return {"results": json.loads(row.result_json), "source": source, "cached": True}
        except Exception:
            pass
    else:
        # delete old cache for this query to force fresh fetch
        try:
            await db.execute(
                text("DELETE FROM component_lookups WHERE query = :q AND source = :s"),
                {"q": cache_key, "s": source}
            )
        except Exception:
            pass

    results = []
    actual_source = source

    if source in ("auto", "digikey"):
        results = await digikey.search(q, limit=10)
        if results:
            actual_source = "digikey"

    if not results and source in ("auto", "lcsc"):
        results = await lcsc.search(q, limit=10)
        if results:
            actual_source = "lcsc"

    if not results and source == "auto":
        actual_source = "none"

    if results:
        try:
            full_text = json.dumps(results)
            await db.execute(
                text("INSERT INTO component_lookups (id, query, source, result_json, full_text, fetched_at) "
                     "VALUES (:id, :q, :s, :r, :ft, :t) "
                     "ON CONFLICT DO NOTHING"),
                {"id": str(uuid.uuid4()), "q": cache_key, "s": actual_source,
                 "r": full_text, "ft": full_text, "t": datetime.utcnow()}
            )
        except Exception:
            pass

    return {"results": results, "source": actual_source, "cached": False}


@router.get("/part")
async def lookup_part(
    pn: str = Query(...),
    source: str = Query("digikey"),
    db: AsyncSession = Depends(get_db),
):
    cache_key = f"detail:{pn}"
    try:
        cached = await db.execute(
            text("SELECT result_json, fetched_at FROM component_lookups WHERE query = :q AND source = :s ORDER BY fetched_at DESC LIMIT 1"),
            {"q": cache_key, "s": source}
        )
        row = cached.fetchone()
        if row and row.fetched_at:
            if datetime.utcnow() - row.fetched_at < CACHE_TTL * 7:
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
                text("INSERT INTO component_lookups (id, query, source, result_json, full_text, fetched_at) VALUES (:id, :q, :s, :r, :ft, :t)"),
                {"id": str(uuid.uuid4()), "q": cache_key, "s": source,
                 "r": json.dumps(result), "ft": json.dumps(result), "t": datetime.utcnow()}
            )
        except Exception:
            pass

    return {"result": result, "cached": False}


@router.get("/debug")
async def debug_lookup(q: str = "esp32", source: str = "digikey"):
    if source == "lcsc":
        return await lcsc.debug_raw(q)
    return await digikey.debug_raw(q)


@router.delete("/cache")
async def clear_cache(db: AsyncSession = Depends(get_db)):
    await db.execute(text("DELETE FROM component_lookups"))
    return {"cleared": True}
