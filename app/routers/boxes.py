from fastapi import APIRouter, Depends, HTTPException, Form, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from app.models.database import get_db
from app.models.models import Box, BinAssignment, Component
from app.services.shelf_key import shelf_sort_key, shelf_display_key
import re

router = APIRouter(prefix="/api/boxes", tags=["boxes"])

BOX_TYPE_GRID = "grid"
BOX_TYPE_FILING = "filing"

# Bags whose divider was deleted land here rather than being orphaned.
UNSORTED_DIVIDER_ID = "unsorted"
UNSORTED_DIVIDER = {"id": UNSORTED_DIVIDER_ID, "label": "Unsorted", "color": "slate"}

# Divider ids are user-supplied and appear in URLs — keep them tame.
_DIVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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


def _is_filing(box: Box) -> bool:
    return (box.box_type or BOX_TYPE_GRID) == BOX_TYPE_FILING


def _dividers_of(box: Box) -> list[dict]:
    meta = box.box_metadata if isinstance(box.box_metadata, dict) else {}
    dividers = meta.get("dividers")
    return list(dividers) if isinstance(dividers, list) else []


def _set_dividers(box: Box, dividers: list[dict]) -> None:
    meta = dict(box.box_metadata) if isinstance(box.box_metadata, dict) else {}
    meta["dividers"] = dividers
    box.box_metadata = meta
    flag_modified(box, "box_metadata")  # JSON column: in-place edits go unseen


async def _get_box_or_404(box_id: str, db: AsyncSession) -> Box:
    box = (await db.execute(select(Box).where(Box.id == box_id))).scalar_one_or_none()
    if not box:
        raise HTTPException(404, "Box not found")
    return box


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
        assigned = (
            await db.execute(
                select(BinAssignment, Component)
                .join(Component, Component.id == BinAssignment.component_id, isouter=True)
                .where(BinAssignment.box_id == box.id, BinAssignment.active == True)
            )
        ).fetchall()

        base = {
            "id": box.id,
            "label": box.label,
            "model": box.model,
            "location": box.location,
            "box_type": box.box_type or BOX_TYPE_GRID,
        }

        if _is_filing(box):
            # No cells to paint — summarize as divider bands instead, sized by
            # how many bags sit behind each.
            counts: dict[str, int] = {}
            for row in assigned:
                did = row.BinAssignment.divider_id or UNSORTED_DIVIDER_ID
                counts[did] = counts.get(did, 0) + 1

            dividers = _dividers_of(box) or []
            known = {d.get("id") for d in dividers}
            bands = [
                {
                    "id": d.get("id"),
                    "label": d.get("label"),
                    "color": d.get("color"),
                    "count": counts.get(d.get("id"), 0),
                }
                for d in dividers
            ]
            # Bags whose divider is gone still exist physically — show them.
            stray = sum(n for did, n in counts.items() if did not in known)
            if stray:
                bands.append({**UNSORTED_DIVIDER, "count": stray})

            total = sum(b["count"] for b in bands)
            out.append(
                {
                    **base,
                    "cell_count": None,
                    "cols": None,
                    "rows": None,
                    "dividers": bands,
                    "occupied_count": total,
                    "occupancy_pct": None,  # a crate has no capacity to be full of
                    "cells": [],
                }
            )
            continue

        cell_count = int(box.cell_count or 144)
        cols = _grid_cols(cell_count)
        cells = [{"occupied": False, "sticker_tag_no": None} for _ in range(cell_count)]

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
                **base,
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
    cell_count: int = Form(None),
    location: str = Form(None),
    slot_index: int = Form(0),
    notes: str = Form(None),
    box_type: str = Form(BOX_TYPE_GRID),
    db: AsyncSession = Depends(get_db),
):
    box_type = (box_type or BOX_TYPE_GRID).strip().lower()
    if box_type not in (BOX_TYPE_GRID, BOX_TYPE_FILING):
        raise HTTPException(400, f"Unknown box_type: {box_type}")

    if box_type == BOX_TYPE_GRID:
        if not cell_count:
            raise HTTPException(400, "cell_count is required for a grid box")
        metadata = {}
    else:
        cell_count = None  # a crate has dividers, not cells
        metadata = {"dividers": []}

    box = Box(
        label=label,
        model=model,
        cell_count=cell_count,
        location=location,
        slot_index=slot_index,
        notes=notes,
        box_type=box_type,
        box_metadata=metadata,
    )
    db.add(box)
    await db.flush()
    return {"id": box.id, "label": label, "box_type": box_type}


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


@router.get("/{box_id}/filing")
async def box_filing(box_id: str, db: AsyncSession = Depends(get_db)):
    """Bags grouped behind each divider, shelf-sorted — drives the crate UI."""
    box = await _get_box_or_404(box_id, db)
    if not _is_filing(box):
        raise HTTPException(400, "Not a filing box")

    rows = (
        await db.execute(
            select(BinAssignment, Component)
            .join(Component, Component.id == BinAssignment.component_id)
            .where(BinAssignment.box_id == box_id, BinAssignment.active == True)
        )
    ).fetchall()

    by_divider: dict[str, list] = {}
    for r in rows:
        did = r.BinAssignment.divider_id or UNSORTED_DIVIDER_ID
        by_divider.setdefault(did, []).append(r.Component)

    dividers = _dividers_of(box)
    known = {d.get("id") for d in dividers}
    if any(did not in known for did in by_divider):
        dividers = dividers + [dict(UNSORTED_DIVIDER)]

    out = []
    for d in dividers:
        bags = sorted(by_divider.get(d.get("id"), []), key=shelf_sort_key)
        out.append(
            {
                "id": d.get("id"),
                "label": d.get("label"),
                "color": d.get("color"),
                "bags": [
                    {
                        "component_id": c.id,
                        "barcode_id": c.barcode_id,
                        "name": c.name,
                        "value": c.value,
                        "short_title": c.short_title,
                        "image_path": c.image_path,
                        "sticker_tag_no": c.sticker_tag_no,
                        "shelf_key": shelf_display_key(c),
                    }
                    for c in bags
                ],
            }
        )

    return {"box_id": box.id, "label": box.label, "dividers": out}


@router.get("/{box_id}/filing/placement")
async def filing_placement(
    box_id: str,
    component_id: str,
    divider_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Where a bag lands behind a divider — 'goes between 4R7 and 22kΩ'."""
    box = await _get_box_or_404(box_id, db)
    if not _is_filing(box):
        raise HTTPException(400, "Not a filing box")

    component = (
        await db.execute(select(Component).where(Component.id == component_id))
    ).scalar_one_or_none()
    if not component:
        raise HTTPException(404, "Component not found")

    rows = (
        await db.execute(
            select(Component)
            .join(BinAssignment, BinAssignment.component_id == Component.id)
            .where(
                BinAssignment.box_id == box_id,
                BinAssignment.active == True,
                BinAssignment.divider_id == divider_id,
            )
        )
    ).scalars().all()

    shelf = sorted([c for c in rows if c.id != component.id], key=shelf_sort_key)
    key = shelf_sort_key(component)
    position = sum(1 for c in shelf if shelf_sort_key(c) < key)

    return {
        "divider_id": divider_id,
        "position": position,
        "of": len(shelf) + 1,
        "shelf_key": shelf_display_key(component),
        "after": shelf_display_key(shelf[position - 1]) if position > 0 else None,
        "before": shelf_display_key(shelf[position]) if position < len(shelf) else None,
    }


@router.get("/{box_id}/dividers")
async def list_dividers(box_id: str, db: AsyncSession = Depends(get_db)):
    box = await _get_box_or_404(box_id, db)
    if not _is_filing(box):
        raise HTTPException(400, "Not a filing box")
    return _dividers_of(box)


@router.post("/{box_id}/dividers")
async def create_divider(
    box_id: str,
    id: str = Form(...),
    label: str = Form(...),
    color: str = Form("slate"),
    db: AsyncSession = Depends(get_db),
):
    box = await _get_box_or_404(box_id, db)
    if not _is_filing(box):
        raise HTTPException(400, "Not a filing box")

    divider_id = (id or "").strip()
    if not _DIVIDER_ID_RE.match(divider_id):
        raise HTTPException(400, "Divider id must be letters, numbers, _ or - only")
    if divider_id == UNSORTED_DIVIDER_ID:
        raise HTTPException(400, f"'{UNSORTED_DIVIDER_ID}' is reserved")

    dividers = _dividers_of(box)
    if any(d.get("id") == divider_id for d in dividers):
        raise HTTPException(409, f"Divider '{divider_id}' already exists")

    dividers.append({"id": divider_id, "label": label, "color": color})
    _set_dividers(box, dividers)
    await db.flush()
    return {"id": divider_id, "label": label, "color": color}


@router.patch("/{box_id}/dividers/{divider_id}")
async def update_divider(
    box_id: str,
    divider_id: str,
    label: str = Form(None),
    color: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    box = await _get_box_or_404(box_id, db)
    if not _is_filing(box):
        raise HTTPException(400, "Not a filing box")

    dividers = _dividers_of(box)
    for d in dividers:
        if d.get("id") == divider_id:
            if label is not None:
                d["label"] = label
            if color is not None:
                d["color"] = color
            _set_dividers(box, dividers)
            await db.flush()
            return d

    raise HTTPException(404, f"Divider '{divider_id}' not found")


@router.delete("/{box_id}/dividers/{divider_id}")
async def delete_divider(
    box_id: str,
    divider_id: str,
    reassign_to: str = None,
    db: AsyncSession = Depends(get_db),
):
    """Delete a divider. Bags behind it move to `reassign_to`, or to Unsorted —
    they exist physically, so orphaning them would lose real parts."""
    box = await _get_box_or_404(box_id, db)
    if not _is_filing(box):
        raise HTTPException(400, "Not a filing box")

    dividers = _dividers_of(box)
    if not any(d.get("id") == divider_id for d in dividers):
        raise HTTPException(404, f"Divider '{divider_id}' not found")

    target = reassign_to or UNSORTED_DIVIDER_ID
    if target != UNSORTED_DIVIDER_ID and not any(d.get("id") == target for d in dividers):
        raise HTTPException(400, f"Cannot reassign to unknown divider '{target}'")

    moved = (
        await db.execute(
            select(BinAssignment).where(
                BinAssignment.box_id == box_id,
                BinAssignment.active == True,
                BinAssignment.divider_id == divider_id,
            )
        )
    ).scalars().all()
    for assignment in moved:
        assignment.divider_id = target

    _set_dividers(box, [d for d in dividers if d.get("id") != divider_id])
    await db.flush()
    return {"deleted": divider_id, "reassigned": len(moved), "to": target}


@router.post("/{box_id}/assign")
async def assign_bin(
    box_id: str,
    cell_id: str = Form(None),
    component_id: str = Form(...),
    footprint_id: str = Form(None),
    divider_id: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    box = await _get_box_or_404(box_id, db)

    if _is_filing(box):
        if not divider_id:
            raise HTTPException(400, "divider_id is required for a filing box")
        known = {d.get("id") for d in _dividers_of(box)} | {UNSORTED_DIVIDER_ID}
        if divider_id not in known:
            raise HTTPException(400, f"Unknown divider '{divider_id}'")
        # Leave cell_id NULL: uq_bin_assignments_active_cell is unique on
        # (box_id, cell_id) WHERE active, and Postgres treats NULLs as distinct,
        # so many bags can share a divider without tripping the index.
        cell_id = None

        # A divider holds many bags, so do NOT evict on collision the way a
        # grid cell does — just move this component if it is already filed here.
        existing = (
            await db.execute(
                select(BinAssignment).where(
                    BinAssignment.box_id == box_id,
                    BinAssignment.component_id == component_id,
                    BinAssignment.active == True,
                )
            )
        ).scalars().all()
        for row in existing:
            row.active = False
    else:
        if not cell_id:
            raise HTTPException(400, "cell_id is required for a grid box")
        divider_id = None
        # One component per grid cell: deactivate whatever was here.
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
        divider_id=divider_id,
    )
    db.add(assignment)
    await db.flush()
    return {"id": assignment.id, "cell_id": cell_id, "divider_id": divider_id}


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
