from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import get_db
from app.services.lookup_engine import search as engine_search
from app.services import digikey, lcsc
from sqlalchemy import text
import json, uuid
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/lookup", tags=["lookup"])


@router.get("/search")
async def lookup_search(
    q: str = Query(..., min_length=2),
    source: str = Query("auto"),
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    return await engine_search(q, source=source, force=force, db=db)


@router.get("/part")
async def lookup_part(
    pn: str = Query(...),
    source: str = Query("digikey"),
    db: AsyncSession = Depends(get_db),
):
    cache_key = f"detail:{pn}"
    try:
        cached = await db.execute(
            text("SELECT result_json, fetched_at FROM component_lookups WHERE query = :q ORDER BY fetched_at DESC LIMIT 1"),
            {"q": cache_key}
        )
        row = cached.fetchone()
        if row and row.fetched_at:
            if datetime.utcnow() - row.fetched_at < timedelta(days=7):
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
