from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.models.database import get_db
from app.models.models import Component, ComponentType, Footprint, BinAssignment
from app.services.barcode_svc import generate_code128_svg, generate_qr, autocrop_image, next_barcode_id
from app.services.influx import write_scan_event, write_stock_change
from app.services.ws_manager import manager
import os, shutil, uuid

IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")
router = APIRouter(prefix="/api/components", tags=["components"])


@router.get("/")
async def list_components(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Component).order_by(Component.barcode_id))
    return result.scalars().all()


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
    type_id: str = Form(...),
    notes: str = Form(None),
    datasheet_url: str = Form(None),
    mpn: str = Form(None),
    digikey_pn: str = Form(None),
    lcsc_pn: str = Form(None),
    description: str = Form(None),
    manufacturer_name: str = Form(None),
    image_url: str = Form(None),
    image: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
):
    # generate barcode ID based on type prefix
    type_result = await db.execute(select(ComponentType).where(ComponentType.id == type_id))
    ctype = type_result.scalar_one_or_none()
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
            import httpx, shutil
            ext = ".jpg"
            fname = f"{barcode_id}{ext}"
            dest = f"{IMAGE_DIR}/components/{fname}"
            async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                r = await client.get(image_url, headers={"User-Agent": "Mozilla/5.0"})
                with open(dest, "wb") as f:
                    f.write(r.content)
            autocrop_image(dest)
            image_path = f"/images/components/{fname}"
        except Exception:
            pass

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
    await manager.broadcast("scan", {"barcode_id": barcode_id, "name": comp.name, "box": box_label, "cell": cell_id})
    return {"barcode_id": barcode_id, "name": comp.name, "box": box_label, "cell": cell_id}


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


@router.get("/types")
async def list_types(db: AsyncSession = Depends(get_db)):
    from app.models.models import ComponentType
    result = await db.execute(select(ComponentType).order_by(ComponentType.name))
    return [{"id": r.id, "name": r.name} for r in result.scalars().all()]
