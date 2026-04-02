from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.models.models import Component, ComponentType, Footprint, Kit, KitComponent, Manufacturer
from app.services.barcode_svc import next_barcode_id

import re


router = APIRouter(prefix="/api/kits", tags=["kits"])


class KitComponentCreate(BaseModel):
    component_id: str | None = None
    barcode_id: str | None = None
    mpn: str | None = None
    manufacturer_name: str | None = None
    digikey_pn: str | None = None
    lcsc_pn: str | None = None
    quantity: int = Field(default=1, ge=1)
    stock_quantity: int = Field(default=0, ge=0)
    notes: str | None = None

    # Optional inline creation fields when component_id is omitted
    name: str | None = None
    type_path: str | None = None
    value: str | None = None
    unit: str | None = None
    package: str | None = None
    tolerance: str | None = None
    voltage_rating: float | None = None
    current_rating: float | None = None
    power_rating: float | None = None
    type_data: dict | None = None


class KitCreate(BaseModel):
    name: str
    description: str | None = None
    notes: str | None = None
    image_path: str | None = None
    components: list[KitComponentCreate]


class KitPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    notes: str | None = None
    image_path: str | None = None
    clear_fields: list[str] = []
    replace_components: bool = False
    components: list[KitComponentCreate] | None = None


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


_MPN_STYLE_RE = re.compile(r"^[A-Za-z]{2,8}-[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def _extract_token_hints(item: KitComponentCreate) -> tuple[str | None, str | None, str | None, str | None]:
    token = (item.mpn or item.name or "").strip()
    if not token or not _MPN_STYLE_RE.match(token):
        return (None, None, item.value, item.unit)

    mpn = token
    name = item.name if item.name and item.name != token else token
    value = item.value
    unit = item.unit

    parts = token.split("-")
    if len(parts) >= 2 and not item.type_path:
        prefix = parts[0].upper()
        if prefix.startswith("CAP"):
            inferred = "passives/capacitor/ceramic"
        elif prefix.startswith("RES"):
            inferred = "passives/resistor/film"
        elif prefix.startswith("IND"):
            inferred = "passives/inductor/coil"
        else:
            inferred = None
        if inferred:
            item.type_path = inferred

    if len(parts) >= 2 and not value:
        value = parts[-1]
    if value and not unit:
        if "uF" in value or "nF" in value or "pF" in value or value.lower().endswith("f"):
            unit = "F"
        elif "ohm" in value.lower() or "ω" in value.lower() or value.lower().endswith("r"):
            unit = "Ohm"

    return (mpn, name, value, unit)


async def _resolve_manufacturer(db: AsyncSession, manufacturer_name: str | None) -> str | None:
    name = (manufacturer_name or "").strip()
    if not name:
        return None
    mfr = (await db.execute(select(Manufacturer).where(Manufacturer.name.ilike(f"%{name}%")).limit(1))).scalar_one_or_none()
    if not mfr:
        mfr = Manufacturer(name=name)
        db.add(mfr)
        await db.flush()
    return mfr.id


def _apply_component_defaults(comp: Component, item: KitComponentCreate, token_mpn: str | None, token_value: str | None, token_unit: str | None):
    comp.name = comp.name or item.name or token_mpn or "Component"
    comp.value = comp.value or item.value or token_value
    comp.unit = comp.unit or item.unit or token_unit
    comp.package = comp.package or item.package
    comp.tolerance = comp.tolerance or item.tolerance
    comp.voltage_rating = comp.voltage_rating if comp.voltage_rating is not None else item.voltage_rating
    comp.current_rating = comp.current_rating if comp.current_rating is not None else item.current_rating
    comp.power_rating = comp.power_rating if comp.power_rating is not None else item.power_rating
    comp.type_path = comp.type_path or item.type_path
    comp.type_data = comp.type_data or item.type_data
    comp.mpn = comp.mpn or item.mpn or token_mpn
    comp.digikey_pn = comp.digikey_pn or item.digikey_pn
    comp.lcsc_pn = comp.lcsc_pn or item.lcsc_pn


async def _apply_kit_stock_influence(db: AsyncSession, comp: Component, item: KitComponentCreate):
    qty = int(item.stock_quantity or 0)
    if qty <= 0:
        return
    mname = (item.manufacturer_name or "").strip() or "KitImport"
    fp = (await db.execute(
        select(Footprint).where(Footprint.component_id == comp.id, Footprint.manufacturer == mname).limit(1)
    )).scalar_one_or_none()
    if not fp:
        fp = Footprint(component_id=comp.id, manufacturer=mname, source="kit_import", quantity=qty)
        db.add(fp)
        return
    fp.quantity = int(fp.quantity or 0) + qty


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
        if item.manufacturer_name and not comp.manufacturer_id:
            comp.manufacturer_id = await _resolve_manufacturer(db, item.manufacturer_name)
        token_mpn, _, token_value, token_unit = _extract_token_hints(item)
        _apply_component_defaults(comp, item, token_mpn, token_value, token_unit)
        await _apply_kit_stock_influence(db, comp, item)
        return comp

    token_mpn, token_name, token_value, token_unit = _extract_token_hints(item)

    if item.barcode_id:
        comp = (await db.execute(select(Component).where(Component.barcode_id == item.barcode_id))).scalar_one_or_none()
        if comp:
            if item.manufacturer_name and not comp.manufacturer_id:
                comp.manufacturer_id = await _resolve_manufacturer(db, item.manufacturer_name)
            _apply_component_defaults(comp, item, token_mpn, token_value, token_unit)
            await _apply_kit_stock_influence(db, comp, item)
            return comp

    id_candidates = [x for x in [item.mpn, token_mpn, item.digikey_pn, item.lcsc_pn] if x]
    if id_candidates:
        for ident in id_candidates:
            comp = (await db.execute(
                select(Component).where(
                    (Component.mpn == ident) |
                    (Component.digikey_pn == ident) |
                    (Component.lcsc_pn == ident)
                ).limit(1)
            )).scalar_one_or_none()
            if comp:
                if item.manufacturer_name and not comp.manufacturer_id:
                    comp.manufacturer_id = await _resolve_manufacturer(db, item.manufacturer_name)
                _apply_component_defaults(comp, item, token_mpn, token_value, token_unit)
                await _apply_kit_stock_influence(db, comp, item)
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
        name=token_name or item.name,
        value=item.value or token_value,
        unit=item.unit or token_unit,
        package=item.package,
        tolerance=item.tolerance,
        voltage_rating=item.voltage_rating,
        current_rating=item.current_rating,
        power_rating=item.power_rating,
        type_id=type_id,
        type_path=item.type_path,
        type_data=item.type_data,
        mpn=item.mpn or token_mpn,
        digikey_pn=item.digikey_pn,
        lcsc_pn=item.lcsc_pn,
        manufacturer_id=await _resolve_manufacturer(db, item.manufacturer_name),
    )
    db.add(comp)
    await db.flush()
    await _apply_kit_stock_influence(db, comp, item)
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


@router.patch("/{kit_id}")
async def patch_kit(kit_id: str, req: KitPatch, db: AsyncSession = Depends(get_db)):
    kit = (await db.execute(
        select(Kit).where((Kit.id == kit_id) | (Kit.barcode_id == kit_id))
    )).scalar_one_or_none()
    if not kit:
        raise HTTPException(404, "Kit not found")

    payload = req.model_dump(exclude_unset=True)
    clear_fields = set(payload.pop("clear_fields", []))
    replace_components = payload.pop("replace_components", False)
    components = payload.pop("components", None)

    for field, value in payload.items():
        if hasattr(kit, field):
            setattr(kit, field, value)

    for field in clear_fields:
        if hasattr(kit, field):
            setattr(kit, field, None)

    if replace_components and components is not None:
        existing_links = (await db.execute(
            select(KitComponent).where(KitComponent.kit_id == kit.id)
        )).scalars().all()
        for link in existing_links:
            await db.delete(link)
        await db.flush()

        merged_rows: dict[str, dict] = {}
        for idx, item in enumerate(components):
            parsed = KitComponentCreate(**item) if isinstance(item, dict) else item
            comp = await _resolve_or_create_component(db, parsed)
            if comp.id in merged_rows:
                merged_rows[comp.id]["quantity"] += parsed.quantity
                continue
            merged_rows[comp.id] = {
                "component_id": comp.id,
                "quantity": parsed.quantity,
                "notes": parsed.notes,
                "position": idx,
            }
        for row in merged_rows.values():
            db.add(KitComponent(kit_id=kit.id, **row))

    return {
        "id": kit.id,
        "barcode_id": kit.barcode_id,
        "updated": True,
    }
