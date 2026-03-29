from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.database import get_db
from app.models.models import Box, BinAssignment, Component

router = APIRouter(prefix="/api/boxes", tags=["boxes"])


@router.get("/")
async def list_boxes(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Box).order_by(Box.label))
    return result.scalars().all()


@router.post("/")
async def create_box(
    label: str = Form(...),
    model: str = Form(...),
    cell_count: int = Form(...),
    location: str = Form(None),
    notes: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    box = Box(label=label, model=model, cell_count=cell_count, location=location, notes=notes)
    db.add(box)
    await db.flush()
    return {"id": box.id, "label": label}


@router.get("/{box_id}/grid")
async def box_grid(box_id: str, db: AsyncSession = Depends(get_db)):
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
