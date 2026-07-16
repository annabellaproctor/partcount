from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.database import get_db
from app.models.models import Box, BinAssignment, Component
import re

router = APIRouter(prefix="/api/boxes", tags=["boxes"])


def _grid_cols(cell_count: int) -> int:
    cc = cell_count or 144
    if cc >= 144:
        return 12
    if cc >= 96:
        return 10
    if cc >= 48:
        return 6
    return 4


def _cell_index(cell_id: str, cols: int) -> int | None:
    m = re.match(r"^R(\d+)C(\d+)$", cell_id or "")
    if not m:
        return None
    r = int(m.group(1))
    c = int(m.group(2))
    return r * cols + c


@router.get("/")
async def list_boxes(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Box).order_by(Box.label))
    return result.scalars().all()


@router.get("/minimap")
async def boxes_minimap(db: AsyncSession = Depends(get_db)):
    """Compact per-box occupancy map for scan/search/dashboard minimap cards."""
    boxes = (await db.execute(select(Box).order_by(Box.slot_index, Box.label))).scalars().all()
    out = []
    for box in boxes:
        cell_count = int(box.cell_count or 144)
        cols = _grid_cols(cell_count)
        cells = [{"occupied": False, "sticker_tag_no": None} for _ in range(cell_count)]

        assigned = (
            await db.execute(
                select(BinAssignment, Component)
                .join(Component, Component.id == BinAssignment.component_id, isouter=True)
                .where(BinAssignment.box_id == box.id, BinAssignment.active == True)
            )
        ).fetchall()

        for row in assigned:
            idx = _cell_index(row.BinAssignment.cell_id, cols)
            if idx is None or idx < 0 or idx >= cell_count:
                continue
            cells[idx] = {
                "occupied": True,
                "sticker_tag_no": row.Component.sticker_tag_no if row.Component else None,
            }

        taken = sum(1 for c in cells if c["occupied"])
        out.append(
            {
                "id": box.id,
                "label": box.label,
                "model": box.model,
                "location": box.location,
                "cell_count": cell_count,
                "cols": cols,
                "rows": (cell_count + cols - 1) // cols,
                "occupied_count": taken,
                "occupancy_pct": round((taken / cell_count) * 100, 1) if cell_count else 0,
                "cells": cells,
            }
        )

    return out


@router.post("/")
async def create_box(
    label: str = Form(...),
    model: str = Form(...),
    cell_count: int = Form(...),
    location: str = Form(None),
    slot_index: int = Form(0),
    notes: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    box = Box(label=label, model=model, cell_count=cell_count, location=location, slot_index=slot_index, notes=notes)
    db.add(box)
    await db.flush()
    return {"id": box.id, "label": label}


@router.get("/{box_id}/grid")
async def box_grid(box_id: str, db: AsyncSession = Depends(get_db), format: str = "json"):
    """Returns all cell assignments for a box — used to render the grid UI"""
    result = await db.execute(
        select(BinAssignment, Component)
        .join(Component, Component.id == BinAssignment.component_id)
        .where(BinAssignment.box_id == box_id, BinAssignment.active == True)
    )
    rows = result.fetchall()
    return [
        {
            "cell_id": r.BinAssignment.cell_id,
            "barcode_id": r.Component.barcode_id,
            "name": r.Component.name,
            "value": r.Component.value,
            "image_path": r.Component.image_path,
            "sticker_tag_no": r.Component.sticker_tag_no,
        }
        for r in rows
    ]


@router.post("/{box_id}/assign")
async def assign_bin(
    box_id: str,
    cell_id: str = Form(...),
    component_id: str = Form(...),
    footprint_id: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    # deactivate any existing assignment for this cell
    existing = await db.execute(
        select(BinAssignment).where(
            BinAssignment.box_id == box_id,
            BinAssignment.cell_id == cell_id,
            BinAssignment.active == True,
        )
    )
    for row in existing.scalars():
        row.active = False

    assignment = BinAssignment(
        box_id=box_id,
        cell_id=cell_id,
        component_id=component_id,
        footprint_id=footprint_id,
    )
    db.add(assignment)
    await db.flush()
    return {"id": assignment.id}


@router.post("/{box_id}/reorder")
async def reorder_cells(
    box_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Accepts JSON body with {from_cell, to_cell} to swap two assignments"""
    from fastapi import Request
    return {"status": "use /api/boxes/{box_id}/swap"}


@router.post("/{box_id}/swap")
async def swap_cells(
    box_id: str,
    from_cell: str = Form(...),
    to_cell: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Swap component assignments between two cells"""
    from_result = await db.execute(
        select(BinAssignment).where(
            BinAssignment.box_id == box_id,
            BinAssignment.cell_id == from_cell,
            BinAssignment.active == True,
        )
    )
    to_result = await db.execute(
        select(BinAssignment).where(
            BinAssignment.box_id == box_id,
            BinAssignment.cell_id == to_cell,
            BinAssignment.active == True,
        )
    )
    from_bin = from_result.scalar_one_or_none()
    to_bin = to_result.scalar_one_or_none()

    if from_bin and to_bin:
        # swap cell IDs
        from_bin.cell_id, to_bin.cell_id = to_cell, from_cell
    elif from_bin:
        from_bin.cell_id = to_cell
    elif to_bin:
        to_bin.cell_id = from_cell

    return {"status": "swapped", "from": from_cell, "to": to_cell}


@router.delete("/{box_id}/cell/{cell_id}")
async def clear_cell(
    box_id: str,
    cell_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BinAssignment).where(
            BinAssignment.box_id == box_id,
            BinAssignment.cell_id == cell_id,
            BinAssignment.active == True,
        )
    )
    assignment = result.scalar_one_or_none()
    if assignment:
        assignment.active = False
    return {"cleared": True}


@router.patch("/slot")
async def update_slot_order(
    box_id: str = Form(...),
    slot_index: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Box).where(Box.id == box_id))
    box = result.scalar_one_or_none()
    if not box:
        raise HTTPException(404)
    box.slot_index = slot_index
    return {"slot_index": slot_index}
