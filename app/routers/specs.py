"""
/api/specs/ — read/write API for the new ParametricSpec / ManufacturerPart / StockLot schema.

These endpoints are ADDITIVE: the legacy /api/components/ routes are untouched.

Endpoint map
────────────
GET    /api/specs/                              list all specs
GET    /api/specs/taxonomy                      full taxonomy tree with inherited schemas
GET    /api/specs/search                        parametric filter search
GET    /api/specs/{barcode_id}                  spec detail with all parts + lots + locations
POST   /api/specs/                              create spec (auto-generates name from taxonomy label)
PATCH  /api/specs/{barcode_id}                  update spec fields / add MPN
POST   /api/specs/{barcode_id}/inventory-action adjust stock
POST   /api/specs/{barcode_id}/manufacturer-parts add MPN to existing spec
DELETE /api/specs/{barcode_id}                  hard delete (blocks if stock > 0)
"""

import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_new_db
from app.models.new_models import (
    CellAssignment,
    ComponentTaxonomy,
    Container,
    Fixture,
    Manufacturer2,
    ManufacturerPart,
    ParametricSpec,
    StockLot,
    Zone,
)
from app.services.barcode_svc import next_barcode_id
from app.services.taxonomy_svc import (
    collect_inherited_schema,
    render_label,
    _make_node_list,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/specs", tags=["specs"])


def _uid() -> str:
    return str(uuid.uuid4())


# ────────────────────────────────────────────────────────────────────────────
# Taxonomy helpers (cached per-request — small table, fast)
# ────────────────────────────────────────────────────────────────────────────

async def _load_taxonomy_nodes(db: AsyncSession) -> list:
    return (await db.execute(
        select(ComponentTaxonomy.path, ComponentTaxonomy.spec_schema, ComponentTaxonomy.label_format)
        .order_by(ComponentTaxonomy.path)
    )).all()


async def _get_taxonomy_row(db: AsyncSession, taxonomy_id: str):
    return (await db.execute(
        select(ComponentTaxonomy).where(ComponentTaxonomy.id == taxonomy_id)
    )).scalar_one_or_none()


def _auto_name(taxonomy_node, specs: Optional[dict], all_nodes) -> Optional[str]:
    """
    Generate name from label_format + specs. Returns None if no format defined.
    """
    if not taxonomy_node or not taxonomy_node.label_format:
        return None
    inherited = collect_inherited_schema(taxonomy_node.path, _make_node_list(all_nodes))
    return render_label(taxonomy_node.label_format, specs or {}, inherited)


# ────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────────

async def _get_spec_or_404(db: AsyncSession, barcode_id: str) -> ParametricSpec:
    spec = (await db.execute(
        select(ParametricSpec).where(ParametricSpec.barcode_id == barcode_id)
    )).scalar_one_or_none()
    if spec is None:
        raise HTTPException(404, f"Spec '{barcode_id}' not found")
    return spec


async def _spec_total_stock(db: AsyncSession, spec_id: str) -> int:
    row = await db.execute(
        select(func.coalesce(func.sum(StockLot.effective_quantity), 0))
        .join(ManufacturerPart, ManufacturerPart.id == StockLot.manufacturer_part_id)
        .where(ManufacturerPart.parametric_spec_id == spec_id)
    )
    return int(row.scalar())


async def _get_or_create_manufacturer(db: AsyncSession, name: str) -> Manufacturer2:
    name = (name or "Unknown").strip() or "Unknown"
    mfr = (await db.execute(
        select(Manufacturer2).where(Manufacturer2.name == name)
    )).scalar_one_or_none()
    if mfr is None:
        mfr = Manufacturer2(id=_uid(), name=name)
        db.add(mfr)
        await db.flush()
    return mfr


async def _resolve_taxonomy(db: AsyncSession, type_path: Optional[str]) -> Optional[ComponentTaxonomy]:
    if not type_path:
        return None
    row = (await db.execute(
        select(ComponentTaxonomy).where(ComponentTaxonomy.path == type_path)
    )).scalar_one_or_none()
    return row


def _spec_summary(spec: ParametricSpec, type_path: Optional[str], stock: int,
                  mpn=None, manufacturer=None, mfr_parts_count: int = 0) -> dict:
    s = spec.specs or {}
    return {
        "id": spec.id,
        "barcode_id": spec.barcode_id or "",
        "name": spec.name or "",
        "specs": s,
        # Convenience top-level fields for search/display
        "value": s.get("value") or s.get("resistance") or s.get("capacitance") or s.get("inductance") or "",
        "unit": s.get("unit", ""),
        "package": s.get("package", ""),
        "tolerance": s.get("tolerance", ""),
        "voltage_rating": s.get("voltage_rating"),
        "image_path": spec.image_path or "",
        "search_alias": spec.search_alias or "",
        "type_path": type_path or "",
        "mpn": mpn,
        "manufacturer": manufacturer,
        "stock": stock,
        "manufacturer_parts_count": mfr_parts_count,
    }


# ────────────────────────────────────────────────────────────────────────────
# GET /api/specs/  — list
# ────────────────────────────────────────────────────────────────────────────

@router.get("/")
async def list_specs(
    q: Optional[str] = None,
    db: AsyncSession = Depends(get_new_db),
):
    stmt = (
        select(
            ParametricSpec.id,
            ParametricSpec.barcode_id,
            ParametricSpec.name,
            ParametricSpec.specs,
            ParametricSpec.image_path,
            ParametricSpec.search_alias,
            ParametricSpec.component_type_id,
            func.min(ManufacturerPart.mpn).label("mpn"),
            func.min(Manufacturer2.name).label("manufacturer"),
            func.coalesce(func.sum(StockLot.effective_quantity), 0).label("stock"),
            func.count(func.distinct(ManufacturerPart.id)).label("manufacturer_parts_count"),
            ComponentTaxonomy.path.label("type_path"),
        )
        .outerjoin(ManufacturerPart, ManufacturerPart.parametric_spec_id == ParametricSpec.id)
        .outerjoin(Manufacturer2, Manufacturer2.id == ManufacturerPart.manufacturer_id)
        .outerjoin(StockLot, StockLot.manufacturer_part_id == ManufacturerPart.id)
        .outerjoin(ComponentTaxonomy, ComponentTaxonomy.id == ParametricSpec.component_type_id)
        .group_by(
            ParametricSpec.id,
            ParametricSpec.barcode_id,
            ParametricSpec.name,
            ParametricSpec.specs,
            ParametricSpec.image_path,
            ParametricSpec.search_alias,
            ParametricSpec.component_type_id,
            ComponentTaxonomy.path,
        )
        .order_by(ParametricSpec.name)
    )

    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            ParametricSpec.barcode_id.ilike(like)
            | ParametricSpec.name.ilike(like)
            | ParametricSpec.search_alias.ilike(like)
            | ParametricSpec.specs.cast(text("text")).ilike(like)
        )

    rows = (await db.execute(stmt)).all()

    return [
        {
            "id": r.id,
            "barcode_id": r.barcode_id or "",
            "name": r.name or "",
            "specs": r.specs or {},
            # Convenience aliases for backward compat with templates/JS
            "value": (r.specs or {}).get("value") or (r.specs or {}).get("resistance") or (r.specs or {}).get("capacitance") or "",
            "unit": (r.specs or {}).get("unit", ""),
            "package": (r.specs or {}).get("package", ""),
            "tolerance": (r.specs or {}).get("tolerance", ""),
            "voltage_rating": (r.specs or {}).get("voltage_rating"),
            "image_path": r.image_path or "",
            "search_alias": r.search_alias or "",
            "type_path": r.type_path or "",
            "mpn": r.mpn,
            "manufacturer": r.manufacturer,
            "stock": int(r.stock),
            "manufacturer_parts_count": int(r.manufacturer_parts_count),
        }
        for r in rows
    ]


# ────────────────────────────────────────────────────────────────────────────
# GET /api/specs/field-definitions  — shared field metadata for UI
# ────────────────────────────────────────────────────────────────────────────

@router.get("/field-definitions")
async def list_field_definitions(db: AsyncSession = Depends(get_new_db)):
    """Return all field_definitions rows for UI to build pickers."""
    rows = (await db.execute(
        text("SELECT * FROM field_definitions ORDER BY field_name")
    )).mappings().all()
    return [dict(r) for r in rows]


# ────────────────────────────────────────────────────────────────────────────
# POST /api/specs/preview-label  — server-side label preview
# ────────────────────────────────────────────────────────────────────────────

class PreviewLabelRequest(BaseModel):
    type_path: str
    specs: dict


@router.post("/preview-label")
async def preview_label(body: PreviewLabelRequest, db: AsyncSession = Depends(get_new_db)):
    """Render the label for a given type_path + specs without creating anything."""
    tax = await _resolve_taxonomy(db, body.type_path)
    if not tax:
        raise HTTPException(400, f"Unknown type_path: {body.type_path}")
    all_nodes = await _load_taxonomy_nodes(db)
    inherited = collect_inherited_schema(tax.path, _make_node_list(all_nodes))
    label = render_label(tax.label_format, body.specs, inherited)
    return {"label": label, "label_format": tax.label_format}


# ────────────────────────────────────────────────────────────────────────────
# GET /api/specs/taxonomy  — must precede /{barcode_id}
# ────────────────────────────────────────────────────────────────────────────

@router.get("/taxonomy")
async def list_taxonomy(db: AsyncSession = Depends(get_new_db)):
    """
    Full taxonomy tree. Each node includes:
    - its own spec_schema fields
    - inherited_schema: merged fields from all ancestors (what the UI should render)
    - label_format: template string for auto-generating spec names
    """
    rows = (await db.execute(
        select(ComponentTaxonomy.path, ComponentTaxonomy.name,
               ComponentTaxonomy.spec_schema, ComponentTaxonomy.label_format)
        .order_by(ComponentTaxonomy.path)
    )).all()

    node_list = _make_node_list(rows)

    return [
        {
            "path": r.path,
            "name": r.name,
            "depth": r.path.count("/"),
            "spec_schema": r.spec_schema or {},
            "label_format": r.label_format,
            "inherited_schema": collect_inherited_schema(r.path, node_list),
        }
        for r in rows
    ]


# ────────────────────────────────────────────────────────────────────────────
# GET /api/specs/search  — parametric filter
# ────────────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_specs(
    type_path: Optional[str] = None,
    package: Optional[str] = None,
    manufacturer: Optional[str] = None,
    in_stock_only: bool = False,
    db: AsyncSession = Depends(get_new_db),
):
    stmt = (
        select(
            ParametricSpec,
            func.coalesce(func.sum(StockLot.effective_quantity), 0).label("total_stock"),
            ComponentTaxonomy.path.label("type_path"),
        )
        .outerjoin(ManufacturerPart, ManufacturerPart.parametric_spec_id == ParametricSpec.id)
        .outerjoin(Manufacturer2, Manufacturer2.id == ManufacturerPart.manufacturer_id)
        .outerjoin(StockLot, StockLot.manufacturer_part_id == ManufacturerPart.id)
        .outerjoin(ComponentTaxonomy, ComponentTaxonomy.id == ParametricSpec.component_type_id)
    )

    if type_path:
        stmt = stmt.where(ComponentTaxonomy.path.like(f"{type_path}%"))

    if package:
        stmt = stmt.where(ParametricSpec.specs["package"].astext == package)

    if manufacturer:
        stmt = stmt.where(Manufacturer2.name.ilike(f"%{manufacturer}%"))

    stmt = stmt.group_by(ParametricSpec.id, ComponentTaxonomy.path).order_by(ParametricSpec.name)

    rows = (await db.execute(stmt)).all()

    results = []
    for spec, total_stock, type_path_val in rows:
        if in_stock_only and int(total_stock) <= 0:
            continue
        s = spec.specs or {}
        results.append({
            "barcode_id": spec.barcode_id or "",
            "name": spec.name or "",
            "specs": s,
            "type_path": type_path_val or "",
            "total_stock": int(total_stock),
        })

    return results


# ────────────────────────────────────────────────────────────────────────────
# GET /api/specs/{barcode_id}  — detail
# ────────────────────────────────────────────────────────────────────────────

@router.get("/{barcode_id}")
async def get_spec(barcode_id: str, db: AsyncSession = Depends(get_new_db)):
    spec = await _get_spec_or_404(db, barcode_id)

    parts_rows = (await db.execute(
        select(ManufacturerPart, Manufacturer2)
        .join(Manufacturer2, Manufacturer2.id == ManufacturerPart.manufacturer_id)
        .where(ManufacturerPart.parametric_spec_id == spec.id)
        .order_by(ManufacturerPart.mpn)
    )).all()

    total_stock = 0
    parts_data = []

    for part, mfr in parts_rows:
        lots = (await db.execute(
            select(StockLot)
            .where(StockLot.manufacturer_part_id == part.id)
            .order_by(StockLot.created_at)
        )).scalars().all()

        lots_data = []
        for lot in lots:
            loc_rows = (await db.execute(
                select(CellAssignment, Container, Fixture, Zone)
                .join(Container, Container.id == CellAssignment.container_id)
                .join(Fixture, Fixture.id == Container.fixture_id)
                .join(Zone, Zone.id == Fixture.zone_id)
                .where(CellAssignment.stock_lot_id == lot.id)
                .where(CellAssignment.active.is_(True))
            )).all()

            qty = lot.effective_quantity or 0
            total_stock += qty

            lots_data.append({
                "lot_id": lot.id,
                "quantity": qty,
                "packaging_type": lot.packaging_type,
                "tape_color": lot.tape_color,
                "stripe_color": lot.stripe_color,
                "lot_code": lot.lot_code,
                "low_stock_threshold": lot.low_stock_threshold,
                "locations": [
                    {
                        "zone": loc[3].name,
                        "fixture": loc[2].name,
                        "container": loc[1].label,
                        "cell": loc[0].cell_id,
                        "cell_quantity": loc[0].quantity,
                    }
                    for loc in loc_rows
                ],
            })

        parts_data.append({
            "part_id": part.id,
            "mpn": part.mpn,
            "manufacturer": mfr.name,
            "digikey_pn": part.digikey_pn,
            "mouser_pn": part.mouser_pn,
            "lcsc_pn": part.lcsc_pn,
            "datasheet_url": part.datasheet_url,
            "lifecycle_status": part.lifecycle_status,
            "stock_lots": lots_data,
        })

    # Taxonomy node + inherited schema
    tax_row = None
    tax_path = None
    inherited_schema = {}
    if spec.component_type_id:
        tax_row = await _get_taxonomy_row(db, spec.component_type_id)
        if tax_row:
            tax_path = tax_row.path
            all_nodes = await _load_taxonomy_nodes(db)
            inherited_schema = collect_inherited_schema(tax_path, _make_node_list(all_nodes))

    return {
        "barcode_id": spec.barcode_id,
        "name": spec.name or "",
        "specs": spec.specs or {},
        "type_path": tax_path,
        "label_format": tax_row.label_format if tax_row else None,
        "inherited_schema": inherited_schema,
        "description": spec.description,
        "notes": spec.notes,
        "image_path": spec.image_path,
        "search_alias": spec.search_alias,
        "total_stock": total_stock,
        "manufacturer_parts": parts_data,
    }


# ────────────────────────────────────────────────────────────────────────────
# POST /api/specs/  — create
# ────────────────────────────────────────────────────────────────────────────

class CreateSpecRequest(BaseModel):
    type_path: str
    specs: dict
    description: Optional[str] = None
    notes: Optional[str] = None
    search_alias: Optional[str] = None
    # Optional: create MPN + stock lot in one shot
    mpn: Optional[str] = None
    manufacturer_name: Optional[str] = None
    quantity: Optional[int] = None


@router.post("/")
async def create_spec(body: CreateSpecRequest, db: AsyncSession = Depends(get_new_db)):
    """
    Create a new ParametricSpec.
    name is auto-generated from type_path label_format + specs dict.
    If mpn is provided, a ManufacturerPart is also created.
    If mpn AND quantity >= 0, a StockLot is also created.
    """
    tax = await _resolve_taxonomy(db, body.type_path)
    if tax is None:
        raise HTTPException(400, f"Taxonomy path '{body.type_path}' not found.")

    all_nodes = await _load_taxonomy_nodes(db)
    generated_name = _auto_name(tax, body.specs, all_nodes)
    if not generated_name:
        # Fallback: use the taxonomy node name if no label_format
        generated_name = tax.name

    existing_ids = (await db.execute(select(ParametricSpec.barcode_id))).scalars().all()
    barcode_id = next_barcode_id("X", list(existing_ids))

    spec = ParametricSpec(
        id=_uid(),
        barcode_id=barcode_id,
        component_type_id=tax.id,
        name=generated_name,
        specs=body.specs or {},
        description=body.description,
        notes=body.notes,
        search_alias=body.search_alias,
    )
    db.add(spec)
    await db.flush()

    mfr_part_id: Optional[str] = None

    if body.mpn:
        mfr = await _get_or_create_manufacturer(db, body.manufacturer_name)

        conflict = (await db.execute(
            select(ManufacturerPart)
            .where(ManufacturerPart.manufacturer_id == mfr.id)
            .where(ManufacturerPart.mpn == body.mpn)
        )).scalar_one_or_none()

        if conflict:
            raise HTTPException(
                409,
                f"MPN '{body.mpn}' already exists for '{mfr.name}' "
                f"(linked to spec={conflict.parametric_spec_id}).",
            )

        part = ManufacturerPart(
            id=_uid(), mpn=body.mpn,
            manufacturer_id=mfr.id, parametric_spec_id=spec.id,
        )
        db.add(part)
        await db.flush()
        mfr_part_id = part.id

        if body.quantity is not None:
            if body.quantity < 0:
                raise HTTPException(400, "quantity must be ≥ 0")
            db.add(StockLot(
                id=_uid(), manufacturer_part_id=mfr_part_id,
                quantity=body.quantity, sigma_adjustment=0,
            ))

    await db.commit()

    return {
        "barcode_id": barcode_id,
        "name": generated_name,
        "mpn": body.mpn,
        "manufacturer": body.manufacturer_name,
        "mfr_part_id": mfr_part_id,
        "stock": body.quantity if (body.mpn and body.quantity is not None) else 0,
    }


# ────────────────────────────────────────────────────────────────────────────
# PATCH /api/specs/{barcode_id}  — update
# ────────────────────────────────────────────────────────────────────────────

class UpdateSpecRequest(BaseModel):
    specs: Optional[dict] = None          # full replacement of specs blob
    specs_patch: Optional[dict] = None    # partial merge into existing specs
    type_path: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    search_alias: Optional[str] = None
    mpn: Optional[str] = None
    manufacturer_name: Optional[str] = None


@router.patch("/{barcode_id}")
async def update_spec(barcode_id: str, body: UpdateSpecRequest, db: AsyncSession = Depends(get_new_db)):
    """
    Update a ParametricSpec.
    - specs: replace the entire specs blob
    - specs_patch: merge keys into existing specs (field-level update)
    - Changing any spec field or type_path re-generates the name.
    - mpn: add a new ManufacturerPart if provided.
    """
    spec = await _get_spec_or_404(db, barcode_id)

    tax_changed = False
    if body.type_path is not None:
        tax = await _resolve_taxonomy(db, body.type_path)
        if tax is None:
            raise HTTPException(400, f"Taxonomy path '{body.type_path}' not found.")
        spec.component_type_id = tax.id
        tax_changed = True

    specs_changed = False
    if body.specs is not None:
        spec.specs = body.specs
        specs_changed = True
    elif body.specs_patch is not None:
        current = dict(spec.specs or {})
        current.update(body.specs_patch)
        spec.specs = current
        specs_changed = True

    if body.description is not None:
        spec.description = body.description
    if body.notes is not None:
        spec.notes = body.notes
    if body.search_alias is not None:
        spec.search_alias = body.search_alias

    # Re-generate name whenever specs or taxonomy changes
    if specs_changed or tax_changed:
        tax_row = await _get_taxonomy_row(db, spec.component_type_id)
        if tax_row:
            all_nodes = await _load_taxonomy_nodes(db)
            new_name = _auto_name(tax_row, spec.specs, all_nodes)
            if new_name:
                spec.name = new_name

    added_part_id: Optional[str] = None
    if body.mpn:
        mfr = await _get_or_create_manufacturer(db, body.manufacturer_name)

        existing_this = (await db.execute(
            select(ManufacturerPart)
            .where(ManufacturerPart.manufacturer_id == mfr.id)
            .where(ManufacturerPart.mpn == body.mpn)
            .where(ManufacturerPart.parametric_spec_id == spec.id)
        )).scalar_one_or_none()

        if existing_this:
            added_part_id = existing_this.id
        else:
            conflict = (await db.execute(
                select(ManufacturerPart)
                .where(ManufacturerPart.manufacturer_id == mfr.id)
                .where(ManufacturerPart.mpn == body.mpn)
            )).scalar_one_or_none()

            if conflict:
                raise HTTPException(
                    409,
                    f"MPN '{body.mpn}' already linked to spec '{conflict.parametric_spec_id}'.",
                )

            part = ManufacturerPart(
                id=_uid(), mpn=body.mpn,
                manufacturer_id=mfr.id, parametric_spec_id=spec.id,
            )
            db.add(part)
            await db.flush()
            added_part_id = part.id

    await db.commit()

    return {
        "barcode_id": barcode_id,
        "name": spec.name,
        "updated": True,
        "added_part_id": added_part_id,
    }


# ────────────────────────────────────────────────────────────────────────────
# POST /api/specs/{barcode_id}/inventory-action
# ────────────────────────────────────────────────────────────────────────────

@router.post("/{barcode_id}/inventory-action")
async def inventory_action(
    barcode_id: str,
    action: str = Form(...),
    quantity: int = Form(...),
    stock_lot_id: Optional[str] = Form(None),
    lot_code: Optional[str] = Form(None),
    packaging_type: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_new_db),
):
    """
    Adjust stock. Valid actions: take | put | restock | calibrate

    Resolution rules:
      1. 0 ManufacturerParts → 400
      2. N > 1 parts, no stock_lot_id → 400
      3. 1 part, 0 lots → auto-create lot then adjust
      4. 1 part, 1 lot → use it
      5. 1 part, N > 1 lots, no stock_lot_id → 400
      6. stock_lot_id supplied → use directly
    """
    spec = await _get_spec_or_404(db, barcode_id)

    parts = (await db.execute(
        select(ManufacturerPart).where(ManufacturerPart.parametric_spec_id == spec.id)
    )).scalars().all()

    if len(parts) == 0:
        raise HTTPException(
            400, f"Spec '{barcode_id}' has no manufacturer parts — add an MPN first.",
        )

    if len(parts) > 1 and stock_lot_id is None:
        raise HTTPException(
            400,
            f"Spec '{barcode_id}' has {len(parts)} parts — supply stock_lot_id. "
            f"Parts: {', '.join(p.mpn for p in parts)}",
        )

    if stock_lot_id is None:
        part = parts[0]
        lots = (await db.execute(
            select(StockLot).where(StockLot.manufacturer_part_id == part.id)
        )).scalars().all()

        if len(lots) == 0:
            auto_lot = StockLot(
                id=_uid(), manufacturer_part_id=part.id,
                quantity=0, sigma_adjustment=0, low_stock_threshold=10,
                lot_code=lot_code, packaging_type=packaging_type,
            )
            db.add(auto_lot)
            await db.flush()
            stock_lot_id = auto_lot.id
        elif len(lots) == 1:
            stock_lot_id = lots[0].id
        else:
            raise HTTPException(
                400,
                f"Part '{part.mpn}' has {len(lots)} lots — supply stock_lot_id.",
            )

    lot = (await db.execute(
        select(StockLot).where(StockLot.id == stock_lot_id)
    )).scalar_one_or_none()
    if lot is None:
        raise HTTPException(404, f"StockLot '{stock_lot_id}' not found")

    owning_part = (await db.execute(
        select(ManufacturerPart)
        .where(ManufacturerPart.id == lot.manufacturer_part_id)
        .where(ManufacturerPart.parametric_spec_id == spec.id)
    )).scalar_one_or_none()
    if owning_part is None:
        raise HTTPException(400, f"StockLot '{stock_lot_id}' does not belong to spec '{barcode_id}'.")

    old_qty = lot.quantity
    action = action.lower().strip()

    if action == "take":
        if old_qty < quantity:
            raise HTTPException(400, f"Insufficient stock: {old_qty} available, cannot take {quantity}.")
        lot.quantity = old_qty - quantity
        quantity_change = -quantity
    elif action in ("put", "restock"):
        if quantity < 0:
            raise HTTPException(400, "quantity must be ≥ 0 for put/restock.")
        lot.quantity = old_qty + quantity
        quantity_change = quantity
    elif action == "calibrate":
        lot.quantity = quantity
        quantity_change = quantity - old_qty
    else:
        raise HTTPException(400, f"Unknown action '{action}'. Valid: take | put | restock | calibrate")

    await db.flush()

    refreshed = (await db.execute(
        select(StockLot.quantity, StockLot.sigma_adjustment, StockLot.effective_quantity)
        .where(StockLot.id == stock_lot_id)
    )).one()

    await db.commit()

    return {
        "barcode_id": barcode_id,
        "stock_lot_id": stock_lot_id,
        "action": action,
        "old_quantity": old_qty,
        "new_quantity": refreshed.effective_quantity,
        "quantity_change": quantity_change,
    }


# ────────────────────────────────────────────────────────────────────────────
# POST /api/specs/{barcode_id}/manufacturer-parts
# ────────────────────────────────────────────────────────────────────────────

class AddManufacturerPartRequest(BaseModel):
    mpn: str
    manufacturer_name: str
    digikey_pn: Optional[str] = None
    lcsc_pn: Optional[str] = None
    mouser_pn: Optional[str] = None
    datasheet_url: Optional[str] = None
    lifecycle_status: str = "active"


@router.post("/{barcode_id}/manufacturer-parts")
async def add_manufacturer_part(
    barcode_id: str,
    body: AddManufacturerPartRequest,
    db: AsyncSession = Depends(get_new_db),
):
    """Add a ManufacturerPart (MPN) to an existing spec."""
    spec = await _get_spec_or_404(db, barcode_id)
    mfr = await _get_or_create_manufacturer(db, body.manufacturer_name)

    existing = (await db.execute(
        select(ManufacturerPart)
        .where(ManufacturerPart.manufacturer_id == mfr.id)
        .where(ManufacturerPart.mpn == body.mpn)
    )).scalar_one_or_none()

    if existing:
        if existing.parametric_spec_id != spec.id:
            raise HTTPException(
                409, f"MPN '{body.mpn}' from '{body.manufacturer_name}' is linked to a different spec.",
            )
        return {"id": existing.id, "mpn": existing.mpn, "created": False}

    part = ManufacturerPart(
        id=_uid(), mpn=body.mpn,
        manufacturer_id=mfr.id, parametric_spec_id=spec.id,
        digikey_pn=body.digikey_pn, lcsc_pn=body.lcsc_pn,
        mouser_pn=body.mouser_pn, datasheet_url=body.datasheet_url,
        lifecycle_status=body.lifecycle_status,
    )
    db.add(part)
    await db.commit()

    return {"id": part.id, "mpn": part.mpn, "created": True}


# ────────────────────────────────────────────────────────────────────────────
# DELETE /api/specs/{barcode_id}
# ────────────────────────────────────────────────────────────────────────────

@router.delete("/{barcode_id}")
async def delete_spec(barcode_id: str, db: AsyncSession = Depends(get_new_db)):
    """
    Hard-delete a ParametricSpec. Blocks if any stock lot has quantity > 0,
    or if referenced by kit_lines / bom_lines.
    """
    spec = await _get_spec_or_404(db, barcode_id)

    total_stock = await _spec_total_stock(db, spec.id)
    if total_stock > 0:
        raise HTTPException(
            400,
            f"Cannot delete '{barcode_id}': {total_stock} units in stock. "
            f"Adjust all lots to zero first.",
        )

    kit_refs = (await db.execute(
        text("SELECT COUNT(*) FROM kit_lines WHERE parametric_spec_id = :sid"), {"sid": spec.id}
    )).scalar()
    bom_refs = (await db.execute(
        text("SELECT COUNT(*) FROM bom_lines WHERE parametric_spec_id = :sid"), {"sid": spec.id}
    )).scalar()

    if kit_refs > 0 or bom_refs > 0:
        raise HTTPException(
            400,
            f"Cannot delete '{barcode_id}': referenced by {kit_refs} kit line(s) "
            f"and {bom_refs} BOM line(s). Remove those references first.",
        )

    parts = (await db.execute(
        select(ManufacturerPart).where(ManufacturerPart.parametric_spec_id == spec.id)
    )).scalars().all()

    for part in parts:
        lots = (await db.execute(
            select(StockLot).where(StockLot.manufacturer_part_id == part.id)
        )).scalars().all()
        for lot in lots:
            await db.execute(
                text("DELETE FROM cell_assignments WHERE stock_lot_id = :lid"), {"lid": lot.id}
            )
            await db.delete(lot)
        await db.delete(part)

    await db.delete(spec)
    await db.commit()

    return {"deleted": True, "barcode_id": barcode_id}
