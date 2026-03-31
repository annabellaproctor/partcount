from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.database import get_db
from app.models.models import Supplier, ComponentSupplier, PurchaseOrder, PurchaseOrderItem, Component, Footprint, InventoryEvent
from app.services.ws_manager import manager
from datetime import datetime
import uuid

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])


# ── Suppliers ──────────────────────────────────────────────────────────────

@router.get("/")
async def list_suppliers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Supplier).order_by(Supplier.name))
    return result.scalars().all()


@router.post("/")
async def create_supplier(
    name: str = Form(...),
    url: str = Form(None),
    notes: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    s = Supplier(id=str(uuid.uuid4()), name=name, url=url, notes=notes)
    db.add(s)
    await db.flush()
    return {"id": s.id, "name": name}


# ── Component Suppliers (SKU/MPN links) ────────────────────────────────────

@router.post("/link")
async def link_component_supplier(
    component_id: str = Form(...),
    supplier_id: str = Form(...),
    sku: str = Form(None),
    mpn: str = Form(None),
    unit_price: float = Form(None),
    pack_size: int = Form(1),
    url: str = Form(None),
    notes: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    cs = ComponentSupplier(
        id=str(uuid.uuid4()),
        component_id=component_id,
        supplier_id=supplier_id,
        sku=sku, mpn=mpn,
        unit_price=unit_price,
        pack_size=pack_size,
        url=url, notes=notes,
    )
    db.add(cs)
    await db.flush()
    return {"id": cs.id}


@router.delete("/link/{cs_id}")
async def unlink_component_supplier(cs_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ComponentSupplier).where(ComponentSupplier.id == cs_id))
    cs = result.scalar_one_or_none()
    if not cs:
        raise HTTPException(404)
    await db.delete(cs)
    return {"deleted": True}


# ── Purchase Orders ────────────────────────────────────────────────────────

@router.get("/orders")
async def list_orders(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc()))
    return result.scalars().all()


@router.post("/orders")
async def create_order(
    supplier_id: str = Form(...),
    reference: str = Form(None),
    order_url: str = Form(None),
    total_cost: float = Form(None),
    notes: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    po = PurchaseOrder(
        id=str(uuid.uuid4()),
        supplier_id=supplier_id,
        reference=reference,
        order_url=order_url,
        total_cost=total_cost,
        notes=notes,
        status="ordered",
        order_date=datetime.utcnow(),
    )
    db.add(po)
    await db.flush()
    await manager.broadcast("order_created", {"id": po.id, "reference": reference})
    return {"id": po.id}


@router.post("/orders/{order_id}/items")
async def add_order_item(
    order_id: str,
    component_id: str = Form(...),
    quantity_ordered: int = Form(1),
    unit_price: float = Form(None),
    notes: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    item = PurchaseOrderItem(
        id=str(uuid.uuid4()),
        order_id=order_id,
        component_id=component_id,
        quantity_ordered=quantity_ordered,
        unit_price=unit_price,
        notes=notes,
    )
    db.add(item)
    await db.flush()

    if component_id:
        fp = (await db.execute(select(Footprint).where(Footprint.component_id == component_id).limit(1))).scalar_one_or_none()
        if not fp:
            fp = Footprint(
                id=str(uuid.uuid4()),
                component_id=component_id,
                quantity=0,
                sigma_adjustment=0,
                manufacturer="Unspecified",
                source="Manual Ledger",
            )
            db.add(fp)
            await db.flush()

        eff = max(0, int(fp.quantity or 0) + int(fp.sigma_adjustment or 0))
        db.add(InventoryEvent(
            id=str(uuid.uuid4()),
            component_id=component_id,
            footprint_id=fp.id,
            event_type="order",
            quantity_input=int(quantity_ordered or 0),
            quantity_change=0,
            sigma_change=0,
            resulting_raw_quantity=int(fp.quantity or 0),
            resulting_effective_quantity=eff,
            reference_id=order_id,
            notes=notes,
        ))
    return {"id": item.id}


@router.post("/orders/{order_id}/receive")
async def receive_order(order_id: str, db: AsyncSession = Depends(get_db)):
    """Mark order received — auto-increments stock for each line item"""
    result = await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == order_id))
    po = result.scalar_one_or_none()
    if not po:
        raise HTTPException(404)

    items_result = await db.execute(
        select(PurchaseOrderItem).where(PurchaseOrderItem.order_id == order_id)
    )
    items = items_result.scalars().all()

    for item in items:
        item.quantity_received = item.quantity_ordered
        # bump stock on first footprint if exists, else create one
        if item.component_id:
            fp_result = await db.execute(
                select(Footprint).where(Footprint.component_id == item.component_id).limit(1)
            )
            fp = fp_result.scalar_one_or_none()
            if fp:
                fp.quantity += item.quantity_ordered
            else:
                fp = Footprint(
                    id=str(uuid.uuid4()),
                    component_id=item.component_id,
                    quantity=item.quantity_ordered,
                    sigma_adjustment=0,
                    manufacturer="Unknown",
                    source="Purchase Order",
                )
                db.add(fp)

            eff = max(0, int(fp.quantity or 0) + int(fp.sigma_adjustment or 0))
            db.add(InventoryEvent(
                id=str(uuid.uuid4()),
                component_id=item.component_id,
                footprint_id=fp.id,
                event_type="restock",
                quantity_input=int(item.quantity_ordered or 0),
                quantity_change=int(item.quantity_ordered or 0),
                sigma_change=0,
                resulting_raw_quantity=int(fp.quantity or 0),
                resulting_effective_quantity=eff,
                reference_id=order_id,
                notes="Auto-restock from purchase order receive",
            ))

    po.status = "received"
    po.received_date = datetime.utcnow()
    await manager.broadcast("order_received", {"id": order_id})
    return {"status": "received", "items_processed": len(items)}
