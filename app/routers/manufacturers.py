from fastapi import APIRouter, Depends, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.database import get_db
from app.models.models import Manufacturer
import uuid

router = APIRouter(prefix="/api/manufacturers", tags=["manufacturers"])


@router.get("/")
async def list_manufacturers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Manufacturer).order_by(Manufacturer.name))
    return [{"id": r.id, "name": r.name, "aliases": r.aliases} for r in result.scalars().all()]


@router.post("/")
async def create_manufacturer(
    name: str = Form(...),
    aliases: str = Form(None),
    url: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    m = Manufacturer(id=str(uuid.uuid4()), name=name, aliases=aliases, url=url)
    db.add(m)
    await db.flush()
    return {"id": m.id, "name": name}
