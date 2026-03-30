from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.models.database import get_db
from app.models.models import Component, ComponentType, Footprint, BinAssignment
from app.services.barcode_svc import generate_code128_svg, generate_qr, autocrop_image, next_barcode_id
from app.services.influx import write_scan_event, write_stock_change
from app.services.ws_manager import manager
from app.schemas.type_hierarchy import flatten_type_paths, get_fields_for_type
from datetime import datetime
import os, shutil, uuid
import json
from sqlalchemy import or_

IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")
router = APIRouter(prefix="/api/components", tags=["components"])


@router.get("/")
async def list_components(db: AsyncSession = Depends(get_db), q: str = None, generic_only: bool = False):
    stmt = select(Component).order_by(Component.barcode_id)
    if q:
        like = f"%{q}%"
        from sqlalchemy import or_
        stmt = stmt.where(or_(
            Component.barcode_id.ilike(like),
            Component.name.ilike(like),
            Component.value.ilike(like),
            Component.package.ilike(like),
        ))
    if generic_only:
        stmt = stmt.where(Component.is_generic == True)
    result = await db.execute(stmt)
    comps = result.scalars().all()
    return [
        {
            "id": c.id,
            "barcode_id": c.barcode_id or "",
            "name": c.name or "",
            "value": c.value or "",
            "package": c.package or "",
            "is_generic": c.is_generic,
            "parent_id": c.parent_id,
        }
        for c in comps
    ]

@router.get("/types")
async def list_types(db: AsyncSession = Depends(get_db)):
    from app.models.models import ComponentType
    result = await db.execute(select(ComponentType).order_by(ComponentType.name))
    return [{"id": r.id, "name": r.name} for r in result.scalars().all()]


@router.get("/type-paths")
async def list_type_paths():
    return {"paths": flatten_type_paths()}


@router.get("/type-fields")
async def get_type_fields(type_path: str):
    return get_fields_for_type(type_path)



@router.get("/{barcode_id}")
async def get_component(barcode_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Component).where(Component.barcode_id == barcode_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, f"Component {barcode_id} not found")
    return comp


@router.post("/")
async def create_component(
    name: str = Form(...),
    value: str = Form(None),
    unit: str = Form(None),
    package: str = Form(None),
    voltage_rating: float = Form(None),
    tolerance: str = Form(None),
    type_id: str = Form(None),
    type_path: str = Form(None),
    type_data: str = Form(None),
    notes: str = Form(None),
    datasheet_url: str = Form(None),
    mpn: str = Form(None),
    digikey_pn: str = Form(None),
    lcsc_pn: str = Form(None),
    description: str = Form(None),
    manufacturer_name: str = Form(None),
    image_url: str = Form(None),
    is_generic: bool = Form(False),
    parent_id: str = Form(None),
    image: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
):
    # Resolve flat type_id for backwards compatibility.
    ctype = None
    if type_id:
        type_result = await db.execute(select(ComponentType).where(ComponentType.id == type_id))
        ctype = type_result.scalar_one_or_none()
    elif type_path:
        parts = [p for p in type_path.split("/") if p]
        if len(parts) >= 2:
            type_name = parts[1]
            type_result = await db.execute(select(ComponentType).where(ComponentType.name == type_name))
            ctype = type_result.scalar_one_or_none()
            if ctype:
                type_id = ctype.id

    if not ctype:
        raise HTTPException(404, "ComponentType not found")

    prefix = ctype.name[0].upper()
    existing = await db.execute(select(Component.barcode_id).where(Component.barcode_id.like(f"{prefix}%")))
    barcode_id = next_barcode_id(prefix, [r[0] for r in existing.fetchall()])

    image_path = None
    if image and image.filename:
        ext = os.path.splitext(image.filename)[1]
        fname = f"{barcode_id}{ext}"
        dest = f"{IMAGE_DIR}/components/{fname}"
        with open(dest, "wb") as f:
            shutil.copyfileobj(image.file, f)
        autocrop_image(dest)
        image_path = f"/images/components/{fname}"

    # resolve manufacturer
    mfr_id = None
    if manufacturer_name:
        from app.models.models import Manufacturer
        mr = await db.execute(select(Manufacturer).where(
            Manufacturer.name.ilike(f"%{manufacturer_name}%")
        ).limit(1))
        mfr = mr.scalar_one_or_none()
        if mfr:
            mfr_id = mfr.id

    # fetch image from URL if provided and no file uploaded
    if image_url and not image_path:
        try:
            import httpx as _hx
            from PIL import Image as _PIL
            fname = f"{barcode_id}.png"
            dest = f"{IMAGE_DIR}/components/{fname}"
            tmp = f"{IMAGE_DIR}/components/_tmp_{barcode_id}"
            async with _hx.AsyncClient(timeout=15, follow_redirects=True,
                                       headers={"User-Agent": "Mozilla/5.0"}) as client:
                r = await client.get(image_url)
                if r.status_code == 200:
                    with open(tmp, "wb") as f:
                        f.write(r.content)
                    try:
                        img = _PIL.open(tmp).convert("RGBA")
                        bbox = img.getbbox()
                        if bbox:
                            img = img.crop(bbox)
                        img.save(dest, "PNG")
                    except Exception:
                        import shutil as _sh
                        _sh.copy(tmp, dest)
                    finally:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                    image_path = f"/images/components/{fname}"
        except Exception as _e:
            import logging as _lg
            _lg.getLogger("components").warning(f"Auto image fetch failed: {_e}")

    parsed_type_data = None
    if type_data:
        try:
            parsed_type_data = json.loads(type_data)
        except json.JSONDecodeError:
            raise HTTPException(400, "type_data must be valid JSON")

    comp = Component(
        barcode_id=barcode_id,
        name=name,
        value=value,
        unit=unit,
        package=package,
        voltage_rating=voltage_rating,
        tolerance=tolerance,
        type_id=type_id,
        notes=notes,
        datasheet_url=datasheet_url,
        image_path=image_path,
        mpn=mpn,
        digikey_pn=digikey_pn,
        lcsc_pn=lcsc_pn,
        description=description,
        manufacturer_id=mfr_id,
        is_generic=bool(is_generic),
        parent_id=parent_id or None,
        type_path=type_path,
        type_data=parsed_type_data,
    )
    db.add(comp)
    await db.flush()

    await manager.broadcast("component_created", {"barcode_id": barcode_id, "name": name})
    return {"barcode_id": barcode_id, "id": comp.id}


@router.get("/{barcode_id}/barcode.svg")
async def get_barcode_svg(barcode_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Component).where(Component.barcode_id == barcode_id))
    if not result.scalar_one_or_none():
        raise HTTPException(404)
    from fastapi.responses import Response
    svg = generate_code128_svg(barcode_id)
    return Response(content=svg, media_type="image/svg+xml")


@router.post("/{barcode_id}/scan")
async def scan_component(barcode_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Component, BinAssignment)
        .join(BinAssignment, BinAssignment.component_id == Component.id, isouter=True)
        .where(Component.barcode_id == barcode_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(404, f"Unknown barcode: {barcode_id}")
    comp, bin_assign = row
    box_label = bin_assign.box.label if bin_assign and bin_assign.box else "unknown"
    cell_id = bin_assign.cell_id if bin_assign else "unknown"
    write_scan_event(barcode_id, comp.name, box_label, cell_id)
    payload = {
        "barcode_id": barcode_id,
        "name": comp.name,
        "box": box_label,
        "cell": cell_id,
        "value": comp.value or "",
        "package": comp.package or "",
        "image_path": comp.image_path or "",
    }
    await manager.broadcast("scan", payload)
    return payload


@router.patch("/{barcode_id}/stock")
async def update_stock(
    barcode_id: str,
    footprint_id: str = Form(...),
    delta: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    fp_result = await db.execute(select(Footprint).where(Footprint.id == footprint_id))
    fp = fp_result.scalar_one_or_none()
    if not fp:
        raise HTTPException(404, "Footprint not found")
    fp.quantity = max(0, fp.quantity + delta)
    comp_result = await db.execute(select(Component).where(Component.barcode_id == barcode_id))
    comp = comp_result.scalar_one_or_none()
    write_stock_change(barcode_id, comp.name if comp else barcode_id, delta, fp.quantity, footprint_id)
    await manager.broadcast("stock_change", {"barcode_id": barcode_id, "footprint_id": footprint_id, "quantity": fp.quantity, "delta": delta})
    return {"quantity": fp.quantity}


class StockUpdateRequest(BaseModel):
    delta: int
    footprint_id: Optional[str] = None


@router.post("/{component_id}/stock")
async def update_stock_by_id(
    component_id: str,
    req: StockUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Atomically increment or decrement stock for a component identified by UUID.
    If footprint_id is omitted, the first footprint for the component is used.
    Quantity is clamped to >= 0.
    """
    comp_result = await db.execute(select(Component).where(Component.id == component_id))
    comp = comp_result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    if req.footprint_id:
        fp_result = await db.execute(
            select(Footprint).where(
                Footprint.id == req.footprint_id,
                Footprint.component_id == component_id,
            )
        )
    else:
        fp_result = await db.execute(
            select(Footprint).where(Footprint.component_id == component_id).limit(1)
        )
    fp = fp_result.scalar_one_or_none()
    if not fp:
        raise HTTPException(404, "No footprint found for this component")

    fp.quantity = max(0, fp.quantity + req.delta)
    write_stock_change(comp.barcode_id, comp.name, req.delta, fp.quantity, fp.id)
    await manager.broadcast(
        "stock_change",
        {
            "barcode_id": comp.barcode_id,
            "component_id": component_id,
            "footprint_id": fp.id,
            "quantity": fp.quantity,
            "delta": req.delta,
        },
    )
    return {"quantity": fp.quantity, "footprint_id": fp.id}


@router.get("/{component_id}/generic-stock")
async def get_generic_stock(component_id: str, db: AsyncSession = Depends(get_db)):
    """
    Returns aggregated total stock for a generic component across all
    brand-specific children (and the generic record itself if it has footprints).
    Works for both generic definitions and brand-specific parts (walks up to parent).
    """
    comp_result = await db.execute(select(Component).where(Component.id == component_id))
    comp = comp_result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    # Resolve to the generic root
    if comp.is_generic:
        generic = comp
    elif comp.parent_id:
        root_result = await db.execute(select(Component).where(Component.id == comp.parent_id))
        generic = root_result.scalar_one_or_none()
        if not generic:
            raise HTTPException(404, "Parent generic component not found")
    else:
        # Not linked to a generic — return this component's own stock
        fp_result = await db.execute(
            select(func.coalesce(func.sum(Footprint.quantity), 0))
            .where(Footprint.component_id == component_id)
        )
        total = fp_result.scalar()
        return {
            "component_id": component_id,
            "is_generic": False,
            "total_stock": total,
            "breakdown": [],
        }

    # Collect IDs: the generic itself + all brand-specific children
    children_result = await db.execute(
        select(Component).where(Component.parent_id == generic.id)
    )
    children = children_result.scalars().all()
    all_ids = [generic.id] + [c.id for c in children]

    # Aggregate footprint quantities per component
    fp_agg = await db.execute(
        select(
            Footprint.component_id,
            func.sum(Footprint.quantity).label("total"),
        )
        .where(Footprint.component_id.in_(all_ids))
        .group_by(Footprint.component_id)
    )
    rows = fp_agg.all()

    qty_by_comp = {r.component_id: int(r.total or 0) for r in rows}
    total_stock = sum(qty_by_comp.values())

    breakdown = [
        {
            "component_id": generic.id,
            "name": generic.name,
            "barcode_id": generic.barcode_id,
            "quantity": qty_by_comp.get(generic.id, 0),
            "is_generic": True,
        }
    ] + [
        {
            "component_id": c.id,
            "name": c.name,
            "barcode_id": c.barcode_id,
            "quantity": qty_by_comp.get(c.id, 0),
            "is_generic": False,
        }
        for c in children
    ]

    return {
        "component_id": generic.id,
        "is_generic": True,
        "total_stock": total_stock,
        "breakdown": breakdown,
    }


@router.post("/{component_id}/touch")
async def touch_component(component_id: str, db: AsyncSession = Depends(get_db)):
    """
    Update updated_at to now, triggering a cache bust for supplier data.
    For generic components this also invalidates cached lookup data so
    DigiKey/LCSC pricing/availability is refreshed on next search.
    """
    comp_result = await db.execute(select(Component).where(Component.id == component_id))
    comp = comp_result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    comp.updated_at = datetime.utcnow()

    # If generic, bust the lookup cache for this component's value+package key
    # so that the next lookup re-fetches live pricing from DigiKey/LCSC.
    if comp.is_generic and (comp.value or comp.name):
        from sqlalchemy import text
        search_key = " ".join(filter(None, [comp.value, comp.package, comp.name])).lower()
        for prefix in ("digikey:", "lcsc:", "auto:"):
            try:
                await db.execute(
                    text("DELETE FROM component_lookups WHERE query = :q"),
                    {"q": f"{prefix}{search_key}"},
                )
            except Exception:
                pass

    return {"updated_at": comp.updated_at.isoformat(), "cache_busted": comp.is_generic}


class SetParentRequest(BaseModel):
    parent_id: Optional[str] = None


class ComponentPatchRequest(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    package: Optional[str] = None
    voltage_rating: Optional[float] = None
    tolerance: Optional[str] = None
    notes: Optional[str] = None
    datasheet_url: Optional[str] = None
    mpn: Optional[str] = None
    digikey_pn: Optional[str] = None
    lcsc_pn: Optional[str] = None
    description: Optional[str] = None
    type_path: Optional[str] = None
    type_data: Optional[dict] = None
    manufacturer_name: Optional[str] = None
    clear_fields: list[str] = []


class ComponentStubCreateRequest(BaseModel):
    name: str
    value: Optional[str] = None
    unit: Optional[str] = None
    package: Optional[str] = None
    supplier_name: Optional[str] = None
    source_note: Optional[str] = None


@router.patch("/{component_id}/parent")
async def set_generic_parent(
    component_id: str,
    req: SetParentRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Link a brand-specific component to a generic definition (or unlink).
    Setting parent_id=null detaches the component from any generic.
    """
    comp_result = await db.execute(select(Component).where(Component.id == component_id))
    comp = comp_result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    if req.parent_id:
        parent_result = await db.execute(select(Component).where(Component.id == req.parent_id))
        parent = parent_result.scalar_one_or_none()
        if not parent:
            raise HTTPException(404, "Parent component not found")
        if not parent.is_generic:
            raise HTTPException(400, "Parent component is not marked as generic")
        if req.parent_id == component_id:
            raise HTTPException(400, "A component cannot be its own parent")

    comp.parent_id = req.parent_id
    return {"component_id": component_id, "parent_id": comp.parent_id}


@router.patch("/{component_id}")
async def patch_component(
    component_id: str,
    req: ComponentPatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Patch component fields (supports clear_fields for AI-assisted data cleanup)."""
    comp = (await db.execute(
        select(Component).where(
            or_(Component.id == component_id, Component.barcode_id == component_id)
        )
    )).scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    payload = req.model_dump(exclude_unset=True)
    clear_fields = set(payload.pop("clear_fields", []))

    manufacturer_name = payload.pop("manufacturer_name", None)
    if manufacturer_name:
        from app.models.models import Manufacturer
        mfr = (await db.execute(
            select(Manufacturer).where(Manufacturer.name.ilike(f"%{manufacturer_name}%")).limit(1)
        )).scalar_one_or_none()
        if mfr:
            comp.manufacturer_id = mfr.id

    for field, value in payload.items():
        if hasattr(comp, field):
            setattr(comp, field, value)

    for field in clear_fields:
        if hasattr(comp, field):
            setattr(comp, field, None)

    comp.updated_at = datetime.utcnow()
    return {
        "id": comp.id,
        "barcode_id": comp.barcode_id,
        "updated": True,
    }


@router.post("/stub")
async def create_component_stub(
    req: ComponentStubCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create minimal component for unresolved order lines and mark as UNREVIEWED/CONFLICT."""
    name = (req.name or "").strip()
    if len(name) < 2:
        raise HTTPException(400, "Component name too short")

    # exact-ish match first to avoid duplicates.
    exact = (await db.execute(
        select(Component).where(Component.name.ilike(name)).limit(1)
    )).scalar_one_or_none()
    if exact:
        return {
            "id": exact.id,
            "barcode_id": exact.barcode_id,
            "created": False,
            "conflict": False,
            "matched_existing": True,
        }

    token = name.split()[0][:24]
    candidates = (await db.execute(
        select(Component).where(Component.name.ilike(f"%{token}%")).limit(6)
    )).scalars().all()

    # Pick a practical default type.
    ctype = (await db.execute(
        select(ComponentType).where(ComponentType.name == "module").limit(1)
    )).scalar_one_or_none()
    if not ctype:
        ctype = (await db.execute(select(ComponentType).limit(1))).scalar_one_or_none()
    if not ctype:
        raise HTTPException(400, "No component types available")

    prefix = ctype.name[0].upper()
    existing = (await db.execute(
        select(Component.barcode_id).where(Component.barcode_id.like(f"{prefix}%"))
    )).scalars().all()
    barcode_id = next_barcode_id(prefix, existing)

    notes = ["[UNREVIEWED]"]
    if candidates:
        notes.append("[CONFLICT]")
        compact = ", ".join(f"{c.barcode_id}:{c.name}" for c in candidates[:4])
        notes.append(f"Possible matches: {compact}")
    if req.source_note:
        notes.append(req.source_note[:240])
    if req.supplier_name:
        notes.append(f"Supplier hint: {req.supplier_name[:80]}")

    comp = Component(
        barcode_id=barcode_id,
        name=name,
        value=req.value,
        unit=req.unit,
        package=req.package,
        type_id=ctype.id,
        notes=" | ".join(notes),
        description="Auto-created from order parse. Review and complete fields.",
        type_path="modules/communication/wifi",
    )
    db.add(comp)
    await db.flush()

    await manager.broadcast("component_created", {"barcode_id": comp.barcode_id, "name": comp.name})
    return {
        "id": comp.id,
        "barcode_id": comp.barcode_id,
        "created": True,
        "conflict": bool(candidates),
        "conflict_candidates": [
            {"id": c.id, "barcode_id": c.barcode_id, "name": c.name}
            for c in candidates
        ],
    }
