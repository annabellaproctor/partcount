"""
BOM resolution endpoint: /api/projects/{project_id}/resolve-bom

Bridges the old BOMItem (legacy schema) with the new ParametricSpec/StockLot
stock tracking to allocate stock to project BOM lines.

The endpoint is additive — it only creates BomLine and BomAllocation rows in
the new schema; the legacy BOMItem table is untouched.

Re-running resolve-bom on the same project is safe: existing BomAllocation rows
for (bom_line_id, stock_lot_id) pairs are updated in-place (upsert semantics),
so repeated calls don't double-count stock.
"""

import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.models.models import BOMItem, Component, Project
from app.models.new_models import (
    BomAllocation,
    BomLine,
    ManufacturerPart,
    ParametricSpec,
    StockLot,
)

router = APIRouter(prefix="/api/projects", tags=["bom"])


def _uid() -> str:
    return str(uuid.uuid4())


@router.post("/{project_id}/resolve-bom")
async def resolve_bom(project_id: str, db: AsyncSession = Depends(get_db)):
    """
    Allocate new-schema stock to BOM lines for a project.

    For each BOMItem that has a linked Component:
      1. Find the migrated ParametricSpec by barcode_id.
      2. Sum available stock across all ManufacturerParts for that spec,
         accounting for existing allocations from other projects.
      3. Pull from fullest lots first (greedy allocation).
      4. Create / update BomLine + BomAllocation records.

    Idempotent: calling twice on the same project updates existing allocations
    rather than creating duplicates.

    Returns a breakdown of fully-allocated, partially-allocated, and missing lines.
    """
    project = (await db.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")

    bom_items = (await db.execute(
        select(BOMItem, Component)
        .outerjoin(Component, Component.id == BOMItem.component_id)
        .where(BOMItem.project_id == project_id)
        .order_by(BOMItem.id)
    )).all()

    if not bom_items:
        return {
            "project_id": project_id,
            "project_name": project.name,
            "allocated_lines": [],
            "partial_lines": [],
            "missing_lines": [],
        }

    allocated_lines = []
    partial_lines = []
    missing_lines = []

    for bom_item, old_comp in bom_items:
        need = int(bom_item.quantity_needed or 1)
        ref = bom_item.description or "N/A"
        comp_name = old_comp.name if old_comp else bom_item.description

        # BOM items without a linked component can't be allocated
        if not old_comp:
            missing_lines.append({
                "ref_des": ref,
                "component_name": comp_name,
                "needed": need,
                "available": 0,
                "reason": "No component linked to this BOM line",
            })
            continue

        # Find migrated spec
        spec = (await db.execute(
            select(ParametricSpec).where(ParametricSpec.barcode_id == old_comp.barcode_id)
        )).scalar_one_or_none()

        if not spec:
            missing_lines.append({
                "ref_des": ref,
                "component_name": comp_name,
                "needed": need,
                "available": 0,
                "reason": f"Component {old_comp.barcode_id} not in new schema",
            })
            continue

        # Get or create BomLine for this (project, spec) pair
        bom_line = (await db.execute(
            select(BomLine)
            .where(BomLine.project_id == project_id)
            .where(BomLine.parametric_spec_id == spec.id)
        )).scalar_one_or_none()

        if not bom_line:
            bom_line = BomLine(
                id=_uid(),
                project_id=project_id,
                ref_des=ref,
                position=0,
                quantity=need,
                parametric_spec_id=spec.id,
            )
            db.add(bom_line)
            await db.flush()
        else:
            bom_line.quantity = need

        # Find all lots for this spec, ordered fullest-first.
        # Subtract already-allocated quantities (from any project) to get available.
        lots = (await db.execute(
            select(StockLot)
            .join(ManufacturerPart, ManufacturerPart.id == StockLot.manufacturer_part_id)
            .where(ManufacturerPart.parametric_spec_id == spec.id)
            .order_by(StockLot.effective_quantity.desc())
        )).scalars().all()

        # Build lot_id → already_allocated map across ALL projects
        if lots:
            lot_ids = [lot.id for lot in lots]
            alloc_rows = (await db.execute(
                select(
                    BomAllocation.stock_lot_id,
                    func.sum(BomAllocation.quantity_allocated).label("total"),
                )
                .where(BomAllocation.stock_lot_id.in_(lot_ids))
                .group_by(BomAllocation.stock_lot_id)
            )).all()
            already_allocated = {r.stock_lot_id: int(r.total) for r in alloc_rows}
        else:
            already_allocated = {}

        remaining = need
        allocations_made: list[tuple[str, int]] = []

        for lot in lots:
            if remaining <= 0:
                break
            total_in_lot = int(lot.effective_quantity or 0)
            used = already_allocated.get(lot.id, 0)
            available = total_in_lot - used
            if available <= 0:
                continue
            to_alloc = min(remaining, available)

            # Upsert: update existing allocation for this (bom_line, lot) pair
            existing_alloc = (await db.execute(
                select(BomAllocation)
                .where(BomAllocation.bom_line_id == bom_line.id)
                .where(BomAllocation.stock_lot_id == lot.id)
            )).scalar_one_or_none()

            if existing_alloc:
                existing_alloc.quantity_allocated = to_alloc
            else:
                db.add(BomAllocation(
                    id=_uid(),
                    bom_line_id=bom_line.id,
                    stock_lot_id=lot.id,
                    quantity_allocated=to_alloc,
                ))

            allocations_made.append((lot.id, to_alloc))
            remaining -= to_alloc

        await db.flush()

        allocated_qty = need - remaining
        if remaining == 0:
            allocated_lines.append({
                "ref_des": ref,
                "component_name": spec.name,
                "barcode_id": spec.barcode_id,
                "allocated_qty": allocated_qty,
                "stock_lot_count": len(allocations_made),
            })
        elif allocated_qty > 0:
            partial_lines.append({
                "ref_des": ref,
                "component_name": spec.name,
                "barcode_id": spec.barcode_id,
                "needed": need,
                "allocated": allocated_qty,
                "shortage": remaining,
            })
        else:
            total_available = sum(
                max(0, int(l.effective_quantity or 0) - already_allocated.get(l.id, 0))
                for l in lots
            )
            missing_lines.append({
                "ref_des": ref,
                "component_name": spec.name,
                "barcode_id": spec.barcode_id,
                "needed": need,
                "available": total_available,
                "reason": "Insufficient stock across all lots",
            })

    await db.commit()

    return {
        "project_id": project_id,
        "project_name": project.name,
        "allocated_lines": allocated_lines,
        "partial_lines": partial_lines,
        "missing_lines": missing_lines,
    }
