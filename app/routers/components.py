from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from typing import Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.models.database import get_db
from app.models.models import Component, ComponentType, Footprint, BinAssignment, InventoryEvent
from app.services.barcode_svc import generate_code128_svg, generate_qr, autocrop_image, next_barcode_id
from app.services.short_title import generate_short_title
from app.services.influx import write_scan_event, write_stock_change
from app.services.ws_manager import manager
from app.schemas.type_hierarchy import TYPE_HIERARCHY, flatten_type_paths, get_fields_for_type
from datetime import datetime
import os, shutil, uuid
import json
import re
import csv
import io
from sqlalchemy import or_

IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")
router = APIRouter(prefix="/api/components", tags=["components"])


CSV_FIELDS = [
    "barcode_id", "name", "short_title", "short_title_manual",
    "value", "unit", "package", "tolerance", "voltage_rating",
    "mpn", "digikey_pn", "lcsc_pn", "datasheet_url", "description", "notes",
    "type_path", "type_data", "sticker_tag_no", "search_alias",
]


def _normalize_delimiter(delim: str | None) -> str:
    d = (delim or "auto").strip().lower()
    if d in {";", "semicolon", "semi"}:
        return ";"
    if d in {"tab", "\\t", "t"}:
        return "\t"
    if d in {",", "comma", "csv"}:
        return ","
    return "auto"


def _read_csv_rows(raw: str, delimiter: str) -> list[dict]:
    text_data = raw.lstrip("\ufeff")
    if delimiter == "auto":
        sniffer = csv.Sniffer()
        sample = text_data[:4096]
        try:
            dialect = sniffer.sniff(sample, delimiters=[",", ";", "\t"])
            reader = csv.DictReader(io.StringIO(text_data), dialect=dialect)
        except Exception:
            reader = csv.DictReader(io.StringIO(text_data), delimiter=",")
    else:
        reader = csv.DictReader(io.StringIO(text_data), delimiter=delimiter)
    return [dict(r) for r in reader]


def _csv_bool(v: Any) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _compact_title_for(comp: Component, payload: dict | None = None) -> str:
    p = payload or {}
    return generate_short_title(
        name=(p.get("name") if payload else comp.name),
        value=(p.get("value") if payload else comp.value),
        unit=(p.get("unit") if payload else comp.unit),
        package=(p.get("package") if payload else comp.package),
        type_path=(p.get("type_path") if payload else comp.type_path),
    )


def _append_search_alias(comp: Component, alias: str | None):
    alias = (alias or "").strip()
    if not alias:
        return
    existing = [x.strip() for x in (comp.search_alias or "").split("|") if x.strip()]
    lowered = {x.lower() for x in existing}
    if alias.lower() in lowered:
        return
    existing.append(alias)
    # Keep alias history bounded.
    comp.search_alias = " | ".join(existing[:20])


def _apply_component_title(comp: Component, *, preferred: str | None = None, manual: bool = False):
    old_name = (comp.name or "").strip()
    title = (preferred or "").strip() or generate_short_title(
        name=comp.name,
        value=comp.value,
        unit=comp.unit,
        package=comp.package,
        type_path=comp.type_path,
    )
    if old_name and old_name.lower() != title.lower():
        _append_search_alias(comp, old_name)
    comp.name = title
    comp.short_title = title
    comp.short_title_manual = bool(manual)


def _is_empty_value(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def _render_name_template(template: str, comp: Component, index_value: int) -> str:
    values = {
        "index": index_value,
        "seq": index_value,
        "barcode_id": comp.barcode_id or "",
        "name": comp.name or "",
        "value": comp.value or "",
        "unit": comp.unit or "",
        "package": comp.package or "",
        "mpn": comp.mpn or "",
        "type_path": comp.type_path or "",
        "image_path": comp.image_path or "",
    }
    if isinstance(comp.type_data, dict):
        for k, v in comp.type_data.items():
            if k not in values:
                values[k] = "" if v is None else str(v)

    def repl(m):
        key = m.group(1)
        fmt = m.group(2)
        val = values.get(key, "")
        if fmt:
            try:
                return ("{" + (":" + fmt) + "}").format(val)
            except Exception:
                return str(val)
        return str(val)

    rendered = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([^}]+))?\}", repl, template)
    return re.sub(r"\s+", " ", rendered).strip()


def _auto_component_name(comp: Component) -> str:
    t = (comp.type_path or "").lower()
    td = comp.type_data if isinstance(comp.type_data, dict) else {}
    value = (comp.value or "").strip()
    unit = (comp.unit or "").strip()
    package = (comp.package or "").strip()

    if "resistor" in t:
        parts = ["Resistor"]
        if value:
            parts.append(f"{value}{unit or 'Ω'}")
        if comp.tolerance:
            parts.append(comp.tolerance)
        if package:
            parts.append(package)
        return " ".join(parts)

    if "capacitor" in t:
        parts = ["Capacitor"]
        if value:
            parts.append(f"{value}{unit or 'F'}")
        if comp.voltage_rating:
            parts.append(f"{comp.voltage_rating:g}V")
        if package:
            parts.append(package)
        return " ".join(parts)

    if "development-board" in t or "mcu-module" in t:
        fam = td.get("mcu_family") or td.get("variant") or comp.mpn or "Module"
        parts = [str(fam)]
        if td.get("clock_speed_mhz"):
            parts.append(f"{td['clock_speed_mhz']}MHz")
        if td.get("ram_size_kb"):
            parts.append(f"{td['ram_size_kb']}KB")
        return " ".join(str(x) for x in parts if x)

    # Generic fallback keeps existing name if it already looks meaningful.
    if comp.name and len(comp.name.strip()) >= 4:
        return comp.name.strip()

    bits = [comp.mpn, comp.value, unit, package]
    return " ".join([b for b in bits if b]) or "Component"


def _e12_values_between_ohms(low: float, high: float) -> list[float]:
    series = [10, 12, 15, 18, 22, 27, 33, 39, 47, 56, 68, 82]
    out = []
    if low <= 0 or high <= 0 or high < low:
        return out

    decade = 1.0
    while decade <= high * 10:
        for base in series:
            v = (base / 10.0) * decade
            if low <= v <= high:
                out.append(v)
        decade *= 10
    # de-dup + sort
    return sorted(set(round(v, 6) for v in out))


def _ohm_to_label(v: float) -> str:
    if v >= 1_000_000:
        m = v / 1_000_000
        return f"{m:g}M"
    if v >= 1000:
        k = v / 1000
        return f"{k:g}k"
    return f"{v:g}"


@router.get("/")
async def list_components(db: AsyncSession = Depends(get_db), q: str = None, generic_only: bool = False):
    stmt = select(Component).order_by(Component.barcode_id)
    if q:
        like = f"%{q}%"
        from sqlalchemy import or_
        stmt = stmt.where(or_(
            Component.barcode_id.ilike(like),
            Component.name.ilike(like),
            Component.search_alias.ilike(like),
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
            "short_title": c.short_title or "",
            "search_alias": c.search_alias or "",
            "value": c.value or "",
            "unit": c.unit or "",
            "package": c.package or "",
            "image_path": c.image_path or "",
            "image_query": c.image_query or "",
            "sticker_tag_no": c.sticker_tag_no,
            "type_path": c.type_path or "",
            "is_generic": c.is_generic,
            "parent_id": c.parent_id,
        }
        for c in comps
    ]


@router.get("/export-all", response_class=PlainTextResponse)
async def export_all_components(
    delimiter: str = "semicolon",
    db: AsyncSession = Depends(get_db),
):
    d = _normalize_delimiter(delimiter)
    sep = ";" if d == "auto" else d
    comps = (await db.execute(select(Component).order_by(Component.barcode_id))).scalars().all()

    out = io.StringIO()
    fields = CSV_FIELDS + ["title_compact", "name_human"]
    writer = csv.DictWriter(out, fieldnames=fields, delimiter=sep, lineterminator="\n")
    writer.writeheader()

    for c in comps:
        row = {
            "barcode_id": c.barcode_id or "",
            "name": c.name or "",
            "short_title": c.short_title or "",
            "short_title_manual": "1" if c.short_title_manual else "0",
            "value": c.value or "",
            "unit": c.unit or "",
            "package": c.package or "",
            "tolerance": c.tolerance or "",
            "voltage_rating": "" if c.voltage_rating is None else f"{c.voltage_rating:g}",
            "mpn": c.mpn or "",
            "digikey_pn": c.digikey_pn or "",
            "lcsc_pn": c.lcsc_pn or "",
            "datasheet_url": c.datasheet_url or "",
            "description": c.description or "",
            "notes": c.notes or "",
            "type_path": c.type_path or "",
            "type_data": json.dumps(c.type_data) if isinstance(c.type_data, dict) else "",
            "sticker_tag_no": "" if c.sticker_tag_no is None else str(c.sticker_tag_no),
            "search_alias": c.search_alias or "",
            "title_compact": _compact_title_for(c),
            "name_human": c.name or "",
        }
        writer.writerow(row)

    return out.getvalue()


@router.post("/import-new")
async def import_components_new(
    file: UploadFile = File(...),
    delimiter: str = Form("auto"),
    db: AsyncSession = Depends(get_db),
):
    raw = (await file.read()).decode("utf-8", errors="replace")
    rows = _read_csv_rows(raw, _normalize_delimiter(delimiter))
    if not rows:
        raise HTTPException(400, "No rows found")

    created = 0
    skipped = 0
    errors = []
    for idx, r in enumerate(rows, start=2):
        try:
            bid = (r.get("barcode_id") or "").strip()
            if not bid:
                skipped += 1
                continue
            exists = (await db.execute(select(Component).where(Component.barcode_id == bid))).scalar_one_or_none()
            if exists:
                skipped += 1
                continue

            payload = {k: (r.get(k) or "").strip() for k in CSV_FIELDS}
            comp = Component(
                barcode_id=bid,
                name=(payload.get("name") or "Component"),
                short_title=(payload.get("short_title") or None),
                short_title_manual=_csv_bool(payload.get("short_title_manual")),
                value=(payload.get("value") or None),
                unit=(payload.get("unit") or None),
                package=(payload.get("package") or None),
                tolerance=(payload.get("tolerance") or None),
                voltage_rating=(float(payload["voltage_rating"]) if payload.get("voltage_rating") else None),
                mpn=(payload.get("mpn") or None),
                digikey_pn=(payload.get("digikey_pn") or None),
                lcsc_pn=(payload.get("lcsc_pn") or None),
                datasheet_url=(payload.get("datasheet_url") or None),
                description=(payload.get("description") or None),
                notes=(payload.get("notes") or None),
                type_path=(payload.get("type_path") or None),
                sticker_tag_no=(int(payload["sticker_tag_no"]) if payload.get("sticker_tag_no") else None),
                search_alias=(payload.get("search_alias") or None),
            )
            if payload.get("type_data"):
                try:
                    comp.type_data = json.loads(payload["type_data"])
                except Exception:
                    pass
            if not comp.short_title:
                comp.short_title = _compact_title_for(comp, payload)
            db.add(comp)
            created += 1
        except Exception as e:
            errors.append({"line": idx, "error": str(e)[:160]})

    await db.flush()
    return {"created": created, "skipped": skipped, "errors": errors}


@router.post("/import-modifications")
async def import_components_modifications(
    file: UploadFile = File(...),
    delimiter: str = Form("auto"),
    db: AsyncSession = Depends(get_db),
):
    raw = (await file.read()).decode("utf-8", errors="replace")
    rows = _read_csv_rows(raw, _normalize_delimiter(delimiter))
    if not rows:
        raise HTTPException(400, "No rows found")

    updated = 0
    skipped = 0
    errors = []
    for idx, r in enumerate(rows, start=2):
        try:
            bid = (r.get("barcode_id") or "").strip()
            if not bid:
                skipped += 1
                continue
            comp = (await db.execute(select(Component).where(Component.barcode_id == bid))).scalar_one_or_none()
            if not comp:
                skipped += 1
                continue

            changed = False
            for f in CSV_FIELDS:
                if f not in r:
                    continue
                val = (r.get(f) or "").strip()
                if f in {"barcode_id"}:
                    continue
                if f == "short_title_manual":
                    comp.short_title_manual = _csv_bool(val)
                    changed = True
                    continue
                if f == "voltage_rating":
                    comp.voltage_rating = float(val) if val else None
                    changed = True
                    continue
                if f == "sticker_tag_no":
                    comp.sticker_tag_no = int(val) if val else None
                    changed = True
                    continue
                if f == "type_data":
                    if val:
                        try:
                            comp.type_data = json.loads(val)
                        except Exception:
                            pass
                    else:
                        comp.type_data = None
                    changed = True
                    continue
                if hasattr(comp, f):
                    setattr(comp, f, val or None)
                    changed = True

            compact = (r.get("title_compact") or "").strip()
            human = (r.get("name_human") or "").strip()
            if compact:
                comp.short_title = compact
                comp.short_title_manual = True
                changed = True
            if human:
                comp.name = human
                changed = True
            if changed:
                updated += 1
        except Exception as e:
            errors.append({"line": idx, "error": str(e)[:160]})

    await db.flush()
    return {"updated": updated, "skipped": skipped, "errors": errors}

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


@router.get("/type-hierarchy")
async def get_type_hierarchy():
    return {"hierarchy": TYPE_HIERARCHY}



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
    short_title: str = Form(None),
    short_title_manual: bool = Form(False),
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
        short_title=(short_title.strip() if short_title else None),
        short_title_manual=bool(short_title_manual and short_title),
        manufacturer_id=mfr_id,
        is_generic=bool(is_generic),
        parent_id=parent_id or None,
        type_path=type_path,
        type_data=parsed_type_data,
    )
    _apply_component_title(
        comp,
        preferred=(short_title.strip() if short_title else None),
        manual=bool(short_title_manual and short_title),
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


@router.delete("/{barcode_id}/image")
async def delete_component_image(barcode_id: str, db: AsyncSession = Depends(get_db)):
    """Delete/reset component image. Sets image_path to None for future re-upload."""
    result = await db.execute(select(Component).where(Component.barcode_id == barcode_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")
    
    # Delete physical file if it exists
    if comp.image_path:
        try:
            file_path = os.path.join("/app", comp.image_path.lstrip("/"))
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            log.warning(f"Failed to delete image file for {barcode_id}: {e}")
    
    # Clear image_path from database
    comp.image_path = None
    await db.commit()
    
    await manager.broadcast("component_updated", {"barcode_id": barcode_id, "field": "image_path", "value": None})
    return {"deleted": True, "barcode_id": barcode_id}


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
    fp.quantity = int(fp.quantity or 0) + int(delta)
    comp_result = await db.execute(select(Component).where(Component.barcode_id == barcode_id))
    comp = comp_result.scalar_one_or_none()
    write_stock_change(barcode_id, comp.name if comp else barcode_id, delta, fp.quantity, footprint_id)
    await manager.broadcast("stock_change", {"barcode_id": barcode_id, "footprint_id": footprint_id, "quantity": fp.quantity, "delta": delta})
    return {"quantity": fp.quantity}


class StockUpdateRequest(BaseModel):
    delta: int
    footprint_id: Optional[str] = None


class InventoryActionRequest(BaseModel):
    action: str
    quantity: int = 0
    footprint_id: Optional[str] = None
    notes: Optional[str] = None
    reference_id: Optional[str] = None


def _effective_quantity(fp: Footprint) -> int:
    return int(fp.quantity or 0) + int(fp.sigma_adjustment or 0)


@router.post("/{component_id}/stock")
async def update_stock_by_id(
    component_id: str,
    req: StockUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Atomically increment or decrement stock for a component identified by UUID.
    If footprint_id is omitted, the first footprint for the component is used.
    Quantity updates allow negatives to support audit and correction workflows.
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

    fp.quantity = int(fp.quantity or 0) + int(req.delta)
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
            select(func.coalesce(func.sum(Footprint.quantity + func.coalesce(Footprint.sigma_adjustment, 0)), 0))
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
            func.sum(Footprint.quantity + func.coalesce(Footprint.sigma_adjustment, 0)).label("total"),
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


@router.get("/{component_id}/inventory-events")
async def list_inventory_events(
    component_id: str,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    comp = (await db.execute(select(Component).where(Component.id == component_id))).scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    take = max(1, min(limit, 500))
    rows = (await db.execute(
        select(InventoryEvent)
        .where(InventoryEvent.component_id == component_id)
        .order_by(InventoryEvent.created_at.desc())
        .limit(take)
    )).scalars().all()

    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "component_id": e.component_id,
            "footprint_id": e.footprint_id,
            "quantity_input": int(e.quantity_input or 0),
            "quantity_change": int(e.quantity_change or 0),
            "sigma_change": int(e.sigma_change or 0),
            "resulting_raw_quantity": int(e.resulting_raw_quantity or 0),
            "resulting_effective_quantity": int(e.resulting_effective_quantity or 0),
            "reference_id": e.reference_id,
            "notes": e.notes or "",
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in rows
    ]


@router.post("/{component_id}/inventory-action")
async def inventory_action(
    component_id: str,
    req: InventoryActionRequest,
    db: AsyncSession = Depends(get_db),
):
    comp = (await db.execute(select(Component).where(Component.id == component_id))).scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    action = (req.action or "").strip().lower()
    if action not in {"take", "put", "restock", "order", "calibrate"}:
        raise HTTPException(400, "Invalid action")

    qty = int(req.quantity or 0)
    if action in {"take", "put", "restock", "order"} and qty <= 0:
        raise HTTPException(400, "quantity must be > 0 for this action")

    if req.footprint_id:
        fp = (await db.execute(
            select(Footprint).where(
                Footprint.id == req.footprint_id,
                Footprint.component_id == component_id,
            )
        )).scalar_one_or_none()
    else:
        fp = (await db.execute(select(Footprint).where(Footprint.component_id == component_id).limit(1))).scalar_one_or_none()

    # Allow ledger-first workflows even when there is no baseline stock record.
    if not fp:
        fp = Footprint(
            id=str(uuid.uuid4()),
            component_id=component_id,
            manufacturer="Unspecified",
            source="Manual Ledger",
            quantity=0,
            sigma_adjustment=0,
        )
        db.add(fp)
        await db.flush()

    raw_before = int(fp.quantity or 0)
    sigma_before = int(fp.sigma_adjustment or 0)
    effective_before = _effective_quantity(fp)

    quantity_change = 0
    sigma_change = 0

    if action == "take":
        fp.quantity = raw_before - qty
        quantity_change = int(fp.quantity or 0) - raw_before
    elif action in {"put", "restock"}:
        fp.quantity = raw_before + qty
        quantity_change = qty
    elif action == "calibrate":
        sigma_change = qty - effective_before
        fp.sigma_adjustment = sigma_before + sigma_change
    else:
        # order is ledger-only; receiving can be recorded as restock later.
        quantity_change = 0

    raw_after = int(fp.quantity or 0)
    sigma_after = int(fp.sigma_adjustment or 0)
    effective_after = _effective_quantity(fp)

    ev = InventoryEvent(
        id=str(uuid.uuid4()),
        component_id=component_id,
        footprint_id=fp.id,
        event_type=action,
        quantity_input=qty,
        quantity_change=quantity_change,
        sigma_change=sigma_change,
        resulting_raw_quantity=raw_after,
        resulting_effective_quantity=effective_after,
        reference_id=(req.reference_id or None),
        notes=(req.notes or None),
    )
    db.add(ev)

    if action in {"take", "put", "restock", "calibrate"}:
        write_stock_change(comp.barcode_id, comp.name, quantity_change, effective_after, fp.id)
        await manager.broadcast(
            "stock_change",
            {
                "barcode_id": comp.barcode_id,
                "component_id": component_id,
                "footprint_id": fp.id,
                "action": action,
                "raw_quantity": raw_after,
                "sigma_adjustment": sigma_after,
                "quantity": effective_after,
                "delta": quantity_change,
                "sigma_delta": sigma_change,
            },
        )

    return {
        "ok": True,
        "event_id": ev.id,
        "component_id": component_id,
        "footprint_id": fp.id,
        "action": action,
        "raw_before": raw_before,
        "sigma_before": sigma_before,
        "effective_before": effective_before,
        "raw_quantity": raw_after,
        "sigma_adjustment": sigma_after,
        "effective_quantity": effective_after,
        "quantity_change": quantity_change,
        "sigma_change": sigma_change,
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
    short_title: Optional[str] = None
    short_title_manual: Optional[bool] = None
    type_path: Optional[str] = None
    type_data: Optional[dict] = None
    sticker_tag_no: Optional[int] = None
    manufacturer_name: Optional[str] = None
    clear_fields: list[str] = []


class ComponentStubCreateRequest(BaseModel):
    name: str
    value: Optional[str] = None
    unit: Optional[str] = None
    package: Optional[str] = None
    supplier_name: Optional[str] = None
    source_note: Optional[str] = None


class BulkDeleteRequest(BaseModel):
    component_ids: list[str]


class BulkMergeRequest(BaseModel):
    component_ids: list[str]
    target_id: Optional[str] = None


class BulkRenameRequest(BaseModel):
    component_ids: list[str]
    mode: str = "set"  # set | prefix | suffix | replace
    value: Optional[str] = None
    find: Optional[str] = None
    replace: Optional[str] = None


class BulkAutoRenameRequest(BaseModel):
    component_ids: list[str]


class BulkAIModifyRequest(BaseModel):
    component_ids: list[str]
    text: str
    action: str = "repair"


class BulkAIRenameRequest(BaseModel):
    component_ids: list[str]
    rules: str
    model: Optional[str] = None


class BulkTemplateRenameRequest(BaseModel):
    component_ids: list[str]
    template: str
    start: int = 1
    step: int = 1


class BulkCopyFieldsRequest(BaseModel):
    source_id: str
    target_ids: list[str]
    fields: list[str]
    only_if_empty: bool = True


class CollectionCommandRequest(BaseModel):
    command: str


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

    short_title_value = payload.pop("short_title", None) if "short_title" in payload else None
    short_title_manual_flag = payload.pop("short_title_manual", None) if "short_title_manual" in payload else None

    for field, value in payload.items():
        if hasattr(comp, field):
            setattr(comp, field, value)

    for field in clear_fields:
        if isinstance(field, str) and field.startswith("type_data."):
            key = field.split(".", 1)[1].strip()
            if key and isinstance(comp.type_data, dict) and key in comp.type_data:
                td = dict(comp.type_data)
                td.pop(key, None)
                comp.type_data = td
            continue
        if hasattr(comp, field):
            setattr(comp, field, None)

    if short_title_value is not None:
        title = short_title_value.strip()
        if title:
            _apply_component_title(comp, preferred=title, manual=True)
        else:
            comp.short_title_manual = False

    if short_title_manual_flag is not None and short_title_value is None:
        comp.short_title_manual = bool(short_title_manual_flag)

    title_relevant_fields = {
        "name", "value", "unit", "package", "type_path", "description"
    }
    changed_title_inputs = bool(title_relevant_fields.intersection(payload.keys()))
    if not comp.short_title_manual and (changed_title_inputs or not comp.short_title or short_title_manual_flag is False):
        _apply_component_title(comp, manual=False)

    comp.updated_at = datetime.utcnow()
    return {
        "id": comp.id,
        "barcode_id": comp.barcode_id,
        "updated": True,
    }


@router.post("/bulk-delete")
async def bulk_delete_components(req: BulkDeleteRequest, db: AsyncSession = Depends(get_db)):
    ids = list(dict.fromkeys([x for x in req.component_ids if x]))
    if not ids:
        raise HTTPException(400, "No component ids provided")

    comps = (await db.execute(select(Component).where(Component.id.in_(ids)))).scalars().all()
    if not comps:
        return {"deleted": 0}

    # Clear parent links before delete.
    children = (await db.execute(select(Component).where(Component.parent_id.in_(ids)))).scalars().all()
    for c in children:
        c.parent_id = None

    # Remove join-table references first.
    from app.models.models import BOMItem, ComponentSupplier, PurchaseOrderItem, KitComponent
    for cid in ids:
        links = (await db.execute(select(KitComponent).where(KitComponent.component_id == cid))).scalars().all()
        for lk in links:
            await db.delete(lk)

        bom = (await db.execute(select(BOMItem).where(BOMItem.component_id == cid))).scalars().all()
        for r in bom:
            r.component_id = None

        suppliers_rows = (await db.execute(select(ComponentSupplier).where(ComponentSupplier.component_id == cid))).scalars().all()
        for r in suppliers_rows:
            await db.delete(r)

        po_rows = (await db.execute(select(PurchaseOrderItem).where(PurchaseOrderItem.component_id == cid))).scalars().all()
        for r in po_rows:
            r.component_id = None

        bins = (await db.execute(select(BinAssignment).where(BinAssignment.component_id == cid))).scalars().all()
        for r in bins:
            await db.delete(r)

        fps = (await db.execute(select(Footprint).where(Footprint.component_id == cid))).scalars().all()
        for r in fps:
            await db.delete(r)

    for comp in comps:
        await db.delete(comp)

    return {"deleted": len(comps)}


@router.post("/bulk-merge")
async def bulk_merge_components(req: BulkMergeRequest, db: AsyncSession = Depends(get_db)):
    ids = list(dict.fromkeys([x for x in req.component_ids if x]))
    if len(ids) < 2:
        raise HTTPException(400, "Select at least 2 components")

    target_id = req.target_id or ids[0]
    if target_id not in ids:
        ids.insert(0, target_id)

    target = (await db.execute(select(Component).where(Component.id == target_id))).scalar_one_or_none()
    if not target:
        raise HTTPException(404, "Target component not found")

    from app.models.models import BOMItem, ComponentSupplier, PurchaseOrderItem, KitComponent
    merged = 0
    for source_id in ids:
        if source_id == target_id:
            continue
        source = (await db.execute(select(Component).where(Component.id == source_id))).scalar_one_or_none()
        if not source:
            continue

        # Fill empty target fields from source.
        for field in ["value", "unit", "package", "voltage_rating", "tolerance", "notes", "datasheet_url", "mpn", "digikey_pn", "lcsc_pn", "description", "type_path"]:
            if getattr(target, field) in (None, "") and getattr(source, field) not in (None, ""):
                setattr(target, field, getattr(source, field))

        if not target.short_title_manual and not target.short_title and source.short_title:
            target.short_title = source.short_title

        # Re-parent children
        kids = (await db.execute(select(Component).where(Component.parent_id == source_id))).scalars().all()
        for child in kids:
            child.parent_id = target_id

        # Merge kit rows, respecting unique(kit_id, component_id)
        src_links = (await db.execute(select(KitComponent).where(KitComponent.component_id == source_id))).scalars().all()
        for lk in src_links:
            tgt_link = (await db.execute(
                select(KitComponent).where(KitComponent.kit_id == lk.kit_id, KitComponent.component_id == target_id)
            )).scalar_one_or_none()
            if tgt_link:
                tgt_link.quantity = (tgt_link.quantity or 0) + (lk.quantity or 0)
                await db.delete(lk)
            else:
                lk.component_id = target_id

        for row in (await db.execute(select(BOMItem).where(BOMItem.component_id == source_id))).scalars().all():
            row.component_id = target_id
        for row in (await db.execute(select(ComponentSupplier).where(ComponentSupplier.component_id == source_id))).scalars().all():
            row.component_id = target_id
        for row in (await db.execute(select(PurchaseOrderItem).where(PurchaseOrderItem.component_id == source_id))).scalars().all():
            row.component_id = target_id
        for row in (await db.execute(select(BinAssignment).where(BinAssignment.component_id == source_id))).scalars().all():
            row.component_id = target_id
        for row in (await db.execute(select(Footprint).where(Footprint.component_id == source_id))).scalars().all():
            row.component_id = target_id

        await db.delete(source)
        merged += 1

    return {"target_id": target_id, "merged": merged}


@router.post("/bulk-rename")
async def bulk_rename_components(req: BulkRenameRequest, db: AsyncSession = Depends(get_db)):
    ids = list(dict.fromkeys([x for x in req.component_ids if x]))
    if not ids:
        raise HTTPException(400, "No component ids provided")
    comps = (await db.execute(select(Component).where(Component.id.in_(ids)))).scalars().all()

    updated = 0
    for comp in comps:
        before = comp.name or ""
        after = before
        if req.mode == "set":
            after = (req.value or "").strip() or before
        elif req.mode == "prefix":
            after = ((req.value or "") + before).strip()
        elif req.mode == "suffix":
            after = (before + (req.value or "")).strip()
        elif req.mode == "replace":
            after = before.replace(req.find or "", req.replace or "")

        if after and after != before:
            _append_search_alias(comp, before)
            comp.name = after
            if not comp.short_title_manual:
                _apply_component_title(comp, preferred=after, manual=False)
            updated += 1

    return {"updated": updated}


@router.post("/bulk-auto-rename")
async def bulk_auto_rename_components(req: BulkAutoRenameRequest, db: AsyncSession = Depends(get_db)):
    ids = list(dict.fromkeys([x for x in req.component_ids if x]))
    if not ids:
        raise HTTPException(400, "No component ids provided")
    comps = (await db.execute(select(Component).where(Component.id.in_(ids)))).scalars().all()

    updated = 0
    for comp in comps:
        new_name = _auto_component_name(comp)
        if new_name and new_name != comp.name:
            _append_search_alias(comp, comp.name)
            comp.name = new_name
            updated += 1
        if not comp.short_title_manual:
            _apply_component_title(comp, preferred=comp.name, manual=False)

    return {"updated": updated}


@router.post("/bulk-ai-modify")
async def bulk_ai_modify_components(req: BulkAIModifyRequest, db: AsyncSession = Depends(get_db)):
    ids = list(dict.fromkeys([x for x in req.component_ids if x]))
    if not ids:
        raise HTTPException(400, "No component ids provided")
    if not req.text or len(req.text.strip()) < 5:
        raise HTTPException(400, "Text too short")

    from app.routers.ai_parse import enrich_record, EnrichRequest

    changed = 0
    failed = []
    for cid in ids:
        comp = (await db.execute(select(Component).where(Component.id == cid))).scalar_one_or_none()
        if not comp:
            failed.append({"id": cid, "error": "not found"})
            continue

        existing = {
            "name": comp.name,
            "barcode_id": comp.barcode_id,
            "value": comp.value,
            "unit": comp.unit,
            "package": comp.package,
            "voltage_rating": comp.voltage_rating,
            "tolerance": comp.tolerance,
            "description": comp.description,
            "notes": comp.notes,
            "mpn": comp.mpn,
            "digikey_pn": comp.digikey_pn,
            "lcsc_pn": comp.lcsc_pn,
            "datasheet_url": comp.datasheet_url,
            "type_path": comp.type_path,
            "type_data": comp.type_data if isinstance(comp.type_data, dict) else {},
        }

        try:
            ai = await enrich_record(EnrichRequest(
                mode="component",
                action=req.action,
                text=req.text,
                existing_data=existing,
            ))
            patch_fields = ai.get("patch_fields") or {}
            clear_fields = ai.get("remove_fields") or []
            for field, val in patch_fields.items():
                if hasattr(comp, field):
                    if field == "name" and comp.name and val and str(val).strip() and comp.name != str(val).strip():
                        _append_search_alias(comp, comp.name)
                    setattr(comp, field, val)
            for field in clear_fields:
                if isinstance(field, str) and field.startswith("type_data."):
                    key = field.split(".", 1)[1].strip()
                    if key and isinstance(comp.type_data, dict) and key in comp.type_data:
                        td = dict(comp.type_data)
                        td.pop(key, None)
                        comp.type_data = td
                    continue
                if hasattr(comp, field):
                    setattr(comp, field, None)
            if not comp.short_title_manual:
                _apply_component_title(comp, preferred=comp.name, manual=False)
            comp.updated_at = datetime.utcnow()
            changed += 1
        except Exception as e:
            failed.append({"id": cid, "error": str(e)[:160]})

    return {"updated": changed, "failed": failed}


@router.post("/bulk-ai-rename")
async def bulk_ai_rename_components(req: BulkAIRenameRequest, db: AsyncSession = Depends(get_db)):
    ids = list(dict.fromkeys([x for x in req.component_ids if x]))
    if not ids:
        raise HTTPException(400, "No component ids provided")
    if not req.rules or len(req.rules.strip()) < 4:
        raise HTTPException(400, "Rules text too short")

    from app.routers.ai_parse import generate_component_title_with_rules

    comps = (await db.execute(select(Component).where(Component.id.in_(ids)))).scalars().all()
    updated = 0
    failed = []

    for comp in comps:
        try:
            ai = await generate_component_title_with_rules(
                {
                    "name": comp.name,
                    "value": comp.value,
                    "unit": comp.unit,
                    "package": comp.package,
                    "mpn": comp.mpn,
                    "type_path": comp.type_path,
                    "type_data": comp.type_data if isinstance(comp.type_data, dict) else {},
                },
                req.rules,
                req.model,
            )
            proposed = ai.get("name")
            if proposed and proposed.strip() and proposed.strip() != comp.name:
                _append_search_alias(comp, comp.name)
                comp.name = proposed.strip()
                if not comp.short_title_manual:
                    _apply_component_title(comp, preferred=comp.name, manual=False)
                updated += 1
        except Exception as e:
            failed.append({"id": comp.id, "error": str(e)[:160]})

    return {"updated": updated, "failed": failed}


@router.post("/bulk-template-rename")
async def bulk_template_rename_components(req: BulkTemplateRenameRequest, db: AsyncSession = Depends(get_db)):
    ids = list(dict.fromkeys([x for x in req.component_ids if x]))
    if not ids:
        raise HTTPException(400, "No component ids provided")
    template = (req.template or "").strip()
    if len(template) < 2:
        raise HTTPException(400, "Template is too short")

    comps = (await db.execute(select(Component).where(Component.id.in_(ids)).order_by(Component.barcode_id))).scalars().all()
    updated = 0
    index = int(req.start)
    step = int(req.step) if int(req.step) != 0 else 1

    for comp in comps:
        before = comp.name or ""
        after = _render_name_template(template, comp, index)
        index += step
        if after and after != before:
            _append_search_alias(comp, before)
            comp.name = after
            if not comp.short_title_manual:
                _apply_component_title(comp, preferred=after, manual=False)
            updated += 1

    return {"updated": updated}


@router.post("/bulk-copy-fields")
async def bulk_copy_fields(req: BulkCopyFieldsRequest, db: AsyncSession = Depends(get_db)):
    allowed_fields = {
        "image_path", "image_query", "sticker_tag_no", "datasheet_url", "package", "type_path", "type_data", "mpn",
        "description", "notes", "value", "unit", "tolerance", "voltage_rating",
        "digikey_pn", "lcsc_pn",
    }
    fields = [f for f in req.fields if f in allowed_fields]
    if not fields:
        raise HTTPException(400, "No allowed fields selected")

    source = (await db.execute(select(Component).where(Component.id == req.source_id))).scalar_one_or_none()
    if not source:
        raise HTTPException(404, "Source component not found")

    target_ids = list(dict.fromkeys([x for x in req.target_ids if x and x != req.source_id]))
    if not target_ids:
        raise HTTPException(400, "No target components provided")

    targets = (await db.execute(select(Component).where(Component.id.in_(target_ids)))).scalars().all()
    copied = 0
    touched = 0
    for target in targets:
        target_changed = False
        for field in fields:
            src_val = getattr(source, field, None)
            if _is_empty_value(src_val):
                continue
            dst_val = getattr(target, field, None)
            if req.only_if_empty and not _is_empty_value(dst_val):
                continue
            setattr(target, field, src_val)
            copied += 1
            target_changed = True
        if target_changed:
            touched += 1
            if not target.short_title_manual:
                _apply_component_title(target, manual=False)

    return {"targets_updated": touched, "fields_copied": copied}


@router.post("/collection-command")
async def create_collection_from_command(req: CollectionCommandRequest, db: AsyncSession = Depends(get_db)):
    cmd = (req.command or "").strip()
    if len(cmd) < 10:
        raise HTTPException(400, "Command too short")

    lc = cmd.lower()
    if "resistor" not in lc:
        raise HTTPException(400, "Only resistor collection commands are currently supported")

    m = re.search(r"from\s*([0-9.]+)\s*(?:ohm|Ω|r)?\s*to\s*([0-9.]+)\s*(?:ohm|Ω|r|k|m)?", lc)
    if not m:
        raise HTTPException(400, "Could not parse resistor range (try: from 10ohm to 100ohm)")
    lo = float(m.group(1))
    hi = float(m.group(2))
    if hi < lo:
        lo, hi = hi, lo

    tol_match = re.search(r"([0-9.]+\s*%)", lc)
    tolerance = tol_match.group(1).replace(" ", "") if tol_match else None
    power_match = re.search(r"([0-9]+\s*/\s*[0-9]+|[0-9.]+)\s*w", lc)
    power = power_match.group(1).replace(" ", "") + "W" if power_match else None
    material = "Carbon Film" if "carbon film" in lc else ("Metal Film" if "metal film" in lc else None)

    ctype = (await db.execute(select(ComponentType).where(ComponentType.name == "resistor").limit(1))).scalar_one_or_none()
    if not ctype:
        ctype = (await db.execute(select(ComponentType).limit(1))).scalar_one_or_none()
    if not ctype:
        raise HTTPException(400, "No component types available")

    prefix = ctype.name[0].upper()
    existing_barcodes = (await db.execute(select(Component.barcode_id).where(Component.barcode_id.like(f"{prefix}%")))).scalars().all()
    pool = set(existing_barcodes)

    created = []
    values = _e12_values_between_ohms(lo, hi)
    for v in values:
        val_label = _ohm_to_label(v)
        exists = (await db.execute(
            select(Component).where(
                Component.type_id == ctype.id,
                Component.value == val_label,
                Component.unit == "Ω",
            ).limit(1)
        )).scalar_one_or_none()
        if exists:
            continue

        barcode = next_barcode_id(prefix, list(pool))
        pool.add(barcode)
        base_name = "Resistor"
        if material:
            base_name = f"{material} Resistor"
        name = f"{base_name} {val_label}Ω"

        notes_parts = ["[AUTO-COLLECTION]", f"Generated from command: {cmd[:180]}"]
        if power:
            notes_parts.append(f"power={power}")

        comp = Component(
            barcode_id=barcode,
            name=name,
            value=val_label,
            unit="Ω",
            tolerance=tolerance,
            type_id=ctype.id,
            type_path="passives/resistor/film" if material else "passives/resistor",
            notes=" | ".join(notes_parts),
        )
        _apply_component_title(comp, manual=False)
        if power:
            comp.type_data = {"power_rating": power}

        db.add(comp)
        created.append({"id": comp.id, "barcode_id": barcode, "name": name})

    return {"created": len(created), "items": created, "range_values": len(values)}


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
    _apply_component_title(comp, manual=False)
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
