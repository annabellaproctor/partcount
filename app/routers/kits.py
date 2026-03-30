from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.models.models import Component, ComponentType, Kit, KitComponent
from app.services.barcode_svc import next_barcode_id

import re


router = APIRouter(prefix="/api/kits", tags=["kits"])


class KitComponentCreate(BaseModel):
    component_id: str | None = None
    quantity: int = Field(default=1, ge=1)
    notes: str | None = None

    # Optional inline creation fields when component_id is omitted
    name: str | None = None
    type_path: str | None = None
    value: str | None = None
    unit: str | None = None
    type_data: dict | None = None


class KitCreate(BaseModel):
    name: str
    description: str | None = None
    notes: str | None = None
    image_path: str | None = None
    components: list[KitComponentCreate]


def _component_type_from_path(type_path: str | None) -> str | None:
    if not type_path:
        return None
    parts = [p for p in type_path.split("/") if p]
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else None


def _prefix_for_type_name(type_name: str | None) -> str:
    mapping = {
        "resistor": "R",
        "capacitor": "C",
        "diode": "D",
        "transistor": "T",
        "mosfet": "Q",
        "ic": "U",
        "inductor": "L",
        "connector": "J",
        "relay": "K",
        "led": "L",
        "module": "M",
        "crystal": "Y",
        "fuse": "F",
        "switch": "S",
        "sensor": "S",
    }
    return mapping.get((type_name or "").lower(), "X")


async def _next_kit_barcode(db: AsyncSession) -> str:
    rows = (await db.execute(select(Kit.barcode_id))).scalars().all()
    max_num = 0
    for bid in rows:
        m = re.match(r"^K(\d+)$", bid or "")
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"K{max_num + 1:03d}"


async def _resolve_or_create_component(db: AsyncSession, item: KitComponentCreate) -> Component:
    if item.component_id:
        comp = (await db.execute(select(Component).where(Component.id == item.component_id))).scalar_one_or_none()
        if not comp:
            raise HTTPException(404, f"Component not found: {item.component_id}")
        return comp

    if not item.name:
        raise HTTPException(400, "Each kit component needs component_id or name")

    inferred_type_name = _component_type_from_path(item.type_path)
    type_id = None
    if inferred_type_name:
        ctype = (await db.execute(
            select(ComponentType).where(ComponentType.name == inferred_type_name)
        )).scalar_one_or_none()
        if ctype:
            type_id = ctype.id

    prefix = _prefix_for_type_name(inferred_type_name)
    existing = (await db.execute(
        select(Component.barcode_id).where(Component.barcode_id.like(f"{prefix}%"))
    )).scalars().all()
    barcode_id = next_barcode_id(prefix, existing)

    comp = Component(
        barcode_id=barcode_id,
        name=item.name,
        value=item.value,
        unit=item.unit,
        type_id=type_id,
        type_path=item.type_path,
        type_data=item.type_data,
    )
    db.add(comp)
    await db.flush()
    return comp


@router.post("/")
async def create_kit(kit_data: KitCreate, db: AsyncSession = Depends(get_db)):
    if not kit_data.components:
        raise HTTPException(400, "A kit must include at least one component")

    barcode_id = await _next_kit_barcode(db)
    kit = Kit(
        barcode_id=barcode_id,
        name=kit_data.name,
        description=kit_data.description,
        notes=kit_data.notes,
        image_path=kit_data.image_path,
    )
    db.add(kit)
    await db.flush()

    # Merge duplicate component IDs in a single payload so UNIQUE(kit_id, component_id) is respected.
    merged_rows: dict[str, dict] = {}
    for idx, item in enumerate(kit_data.components):
        comp = await _resolve_or_create_component(db, item)
        if comp.id in merged_rows:
            merged_rows[comp.id]["quantity"] += item.quantity
            continue
        merged_rows[comp.id] = {
            "component_id": comp.id,
            "quantity": item.quantity,
            "notes": item.notes,
            "position": idx,
        }

    for row in merged_rows.values():
        db.add(KitComponent(kit_id=kit.id, **row))

    await db.flush()

    return {
        "id": kit.id,
        "barcode_id": kit.barcode_id,
        "name": kit.name,
        "description": kit.description,
        "component_count": len(merged_rows),
    }


@router.get("/")
async def list_kits(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Kit).order_by(Kit.barcode_id))).scalars().all()
    return [
        {
            "id": k.id,
            "barcode_id": k.barcode_id,
            "name": k.name,
            "description": k.description,
            "image_path": k.image_path,
        }
        for k in rows
    ]


@router.get("/{kit_id}")
async def get_kit(kit_id: str, db: AsyncSession = Depends(get_db)):
    kit = (await db.execute(select(Kit).where(Kit.id == kit_id))).scalar_one_or_none()
    if not kit:
        raise HTTPException(404, "Kit not found")

    rows = (await db.execute(
        select(KitComponent, Component)
        .join(Component, Component.id == KitComponent.component_id)
        .where(KitComponent.kit_id == kit_id)
        .order_by(KitComponent.position)
    )).all()

    components = [
        {
            "id": comp.id,
            "barcode_id": comp.barcode_id,
            "name": comp.name,
            "value": comp.value,
            "unit": comp.unit,
            "type_path": comp.type_path,
            "quantity": link.quantity,
            "notes": link.notes,
        }
        for link, comp in rows
    ]

    return {
        "id": kit.id,
        "barcode_id": kit.barcode_id,
        "name": kit.name,
        "description": kit.description,
        "image_path": kit.image_path,
        "notes": kit.notes,
        "components": components,
    }


@router.delete("/{kit_id}")
async def delete_kit(kit_id: str, db: AsyncSession = Depends(get_db)):
    kit = (await db.execute(select(Kit).where(Kit.id == kit_id))).scalar_one_or_none()
    if not kit:
        raise HTTPException(404, "Kit not found")

    await db.delete(kit)
    return {"deleted": True}
