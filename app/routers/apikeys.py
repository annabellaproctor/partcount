from fastapi import APIRouter, Depends, HTTPException, Form, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from app.models.database import get_db
from app.models.models import APIKey, Profile

router = APIRouter(prefix="/api/keys", tags=["api_keys"])
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    api_key: str = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> APIKey:
    if not api_key:
        raise HTTPException(401, "X-API-Key header required")
    result = await db.execute(select(APIKey).where(APIKey.key == api_key, APIKey.active == True))
    key_obj = result.scalar_one_or_none()
    if not key_obj:
        raise HTTPException(403, "Invalid or inactive API key")
    key_obj.last_used = datetime.utcnow()
    return key_obj


@router.get("/")
async def list_keys(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(APIKey).order_by(APIKey.created_at.desc()))
    keys = result.scalars().all()
    return [{"id": k.id, "label": k.label, "active": k.active, "last_used": k.last_used, "created_at": k.created_at, "key_preview": k.key[:16] + "..."} for k in keys]


@router.post("/")
async def create_key(
    label: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(400, "No profile found — seed profile first")
    key = APIKey(label=label, profile_id=profile.id)
    db.add(key)
    await db.flush()
    return {"id": key.id, "key": key.key, "label": label}


@router.delete("/{key_id}")
async def revoke_key(key_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(404)
    key.active = False
    return {"revoked": True}
