from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from contextlib import asynccontextmanager
import asyncio, os, json

from app.models.database import get_db
from app.models.models import Component, ComponentType, Box, Footprint, Project, Profile, APIKey, TodoItem, BOMItem, Kit, KitComponent, PurchaseOrder, PurchaseOrderItem, Supplier, InventoryEvent
from app.routers import components, boxes, labels, projects, apikeys, suppliers, images, migrate, lookup, manufacturers, ai_parse, usage_stats, kits
from app.services.ws_manager import manager
from app.schemas.type_hierarchy import get_fields_for_type, flatten_type_paths

IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.migrations import run_migrations
    from app.services.seed import seed_all
    await run_migrations()
    await seed_all()
    yield


app = FastAPI(title="Lab Inventory", lifespan=lifespan)

app.include_router(components.router)
app.include_router(boxes.router)
app.include_router(labels.router)
app.include_router(projects.router)
app.include_router(apikeys.router)
app.include_router(suppliers.router)
app.include_router(images.router)
app.include_router(migrate.router)
app.include_router(lookup.router)
app.include_router(manufacturers.router)
app.include_router(ai_parse.router)
app.include_router(usage_stats.router)
app.include_router(kits.router)

app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")
app.mount("/static", StaticFiles(directory="/app/app/static"), name="static")

templates = Jinja2Templates(directory="/app/templates")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data.startswith("SCAN:"):
                barcode_id = data[5:].strip()
                await websocket.send_text(f'{{"event":"scan_ack","data":{{"barcode_id":"{barcode_id}"}}}}')
    except WebSocketDisconnect:
        manager.disconnect(websocket)




@app.get("/api/sources")
async def get_sources():
    """Returns which supplier APIs are currently configured and available."""
    import os
    return {
        "digikey": bool(os.getenv("DIGIKEY_CLIENT_ID")),
        "mouser":  bool(os.getenv("MOUSER_API_KEY")),
        "gemini":  bool(os.getenv("GEMINI_API_KEY")),
        "trustedparts": False,  # pending
    }

@app.get("/api/icons/{component_type}")
async def get_icon(component_type: str):
    from app.services.generic_icons import get_icon
    from fastapi.responses import Response
    svg = get_icon(component_type)
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/search-index")
async def search_index(db: AsyncSession = Depends(get_db), limit: int = 200):
    take = max(20, min(limit, 1200))
    comps = (await db.execute(select(Component).order_by(Component.updated_at.desc()).limit(take))).scalars().all()
    kits_rows = (await db.execute(select(Kit).order_by(Kit.updated_at.desc()).limit(max(30, min(300, take // 2))))).scalars().all()
    projects_rows = (await db.execute(select(Project).order_by(Project.updated_at.desc()).limit(120))).scalars().all()
    boxes_rows = (await db.execute(select(Box).order_by(Box.slot_index, Box.label).limit(300))).scalars().all()

    pages = [
        {"label": "Dashboard", "route": "/", "kind": "page"},
        {"label": "Components", "route": "/components", "kind": "page"},
        {"label": "Registry", "route": "/registry", "kind": "page"},
        {"label": "Kits", "route": "/kits", "kind": "page"},
        {"label": "Orders", "route": "/orders", "kind": "page"},
        {"label": "Boxes", "route": "/boxes", "kind": "page"},
        {"label": "Projects", "route": "/projects", "kind": "page"},
        {"label": "Labels", "route": "/labels/designer", "kind": "page"},
        {"label": "Scan", "route": "/scan", "kind": "page"},
        {"label": "Settings", "route": "/settings", "kind": "page"},
    ]

    items = pages + [
        {
            "label": f"{c.barcode_id} · {c.name}",
            "route": f"/components/{c.barcode_id}",
            "kind": "component",
            "tokens": " ".join(filter(None, [c.barcode_id, c.name, c.value, c.package, c.search_alias]))
        }
        for c in comps
    ] + [
        {
            "label": f"{k.barcode_id} · {k.name}",
            "route": f"/kits/{k.id}",
            "kind": "kit",
            "tokens": " ".join(filter(None, [k.barcode_id, k.name, k.description]))
        }
        for k in kits_rows
    ] + [
        {
            "label": p.name,
            "route": f"/projects/{p.id}",
            "kind": "project",
            "tokens": " ".join(filter(None, [p.name, p.description]))
        }
        for p in projects_rows
    ] + [
        {
            "label": f"{b.label} · {b.model or 'Box'}",
            "route": "/boxes",
            "kind": "box",
            "tokens": " ".join(filter(None, [b.label, b.model, b.location]))
        }
        for b in boxes_rows
    ]

    return {"items": items}

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    comp_count = (await db.execute(select(func.count()).select_from(Component))).scalar()
    box_count = (await db.execute(select(func.count()).select_from(Box))).scalar()
    proj_count = (await db.execute(select(func.count()).select_from(Project))).scalar()
    recent = (await db.execute(select(Component).order_by(Component.created_at.desc()).limit(8))).scalars().all()
    boxes_ordered = (await db.execute(select(Box).order_by(Box.slot_index, Box.label))).scalars().all()
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    projects_active = (await db.execute(select(Project).where(Project.status == "active").order_by(Project.updated_at.desc()).limit(5))).scalars().all()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "comp_count": comp_count,
        "box_count": box_count,
        "proj_count": proj_count,
        "recent": recent,
        "boxes": boxes_ordered,
        "profile": profile,
        "projects_active": projects_active,
    })



@app.get("/add", response_class=HTMLResponse)
async def add_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Standalone full-page add form — opened via Shift+click or new tab"""
    types = (await db.execute(select(ComponentType).order_by(ComponentType.name))).scalars().all()
    from app.models.models import Manufacturer
    manufacturers = (await db.execute(select(Manufacturer).order_by(Manufacturer.name))).scalars().all()
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("add_component.html", {
        "request": request, "types": types, "manufacturers": manufacturers, "profile": profile,
    })

@app.get("/components", response_class=HTMLResponse)
async def components_page(request: Request, db: AsyncSession = Depends(get_db)):
    # Fetch each component with its type and summed footprint quantity
    result = await db.execute(
        select(
            Component,
            ComponentType,
            func.coalesce(func.sum(Footprint.quantity), 0).label("total_stock"),
            func.count(func.distinct(KitComponent.kit_id)).label("kit_refs"),
        )
        .join(ComponentType, ComponentType.id == Component.type_id, isouter=True)
        .join(Footprint, Footprint.component_id == Component.id, isouter=True)
        .join(KitComponent, KitComponent.component_id == Component.id, isouter=True)
        .group_by(Component.id, ComponentType.id)
        .order_by(Component.barcode_id)
    )
    raw_rows = result.fetchall()

    # For generic components, aggregate stock across children too
    generic_ids = [r.Component.id for r in raw_rows if r.Component.is_generic]
    child_stock: dict = {}
    if generic_ids:
        child_agg = await db.execute(
            select(Component.parent_id, func.sum(Footprint.quantity).label("child_total"))
            .join(Footprint, Footprint.component_id == Component.id)
            .where(Component.parent_id.in_(generic_ids))
            .group_by(Component.parent_id)
        )
        child_stock = {r.parent_id: int(r.child_total or 0) for r in child_agg.fetchall()}

    rows = []
    for r in raw_rows:
        own_stock = int(r.total_stock or 0)
        total = (own_stock + child_stock.get(r.Component.id, 0)) if r.Component.is_generic else own_stock
        td = r.Component.type_data if isinstance(r.Component.type_data, dict) else {}
        td_keys = [
            "mcu_family", "variant", "clock_speed_mhz", "ram_size_kb", "flash_size_mb",
            "pin_count", "wireless", "interface", "core_count",
        ]
        bits = []
        for k in td_keys:
            if k in td and td[k] not in (None, "", []):
                bits.append(f"{k}={td[k]}")
        archetype_summary = ", ".join(bits[:3])
        rows.append((r.Component, r.ComponentType, total, int(r.kit_refs or 0), archetype_summary))

    types = (await db.execute(select(ComponentType).order_by(ComponentType.name))).scalars().all()
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("components.html", {
        "request": request, "rows": rows, "types": types, "profile": profile,
    })


@app.get("/boxes", response_class=HTMLResponse)
async def boxes_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = (await db.execute(select(Box).order_by(Box.slot_index, Box.label))).scalars().all()
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("boxes.html", {
        "request": request, "boxes": result, "profile": profile,
    })



@app.get("/boxes/{box_id}", response_class=HTMLResponse)
async def box_grid_page(box_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    from app.models.models import BinAssignment
    box = (await db.execute(select(Box).where(Box.id == box_id))).scalar_one_or_none()
    if not box:
        raise HTTPException(404)
    assignments = (await db.execute(
        select(BinAssignment, Component)
        .join(Component, Component.id == BinAssignment.component_id, isouter=True)
        .where(BinAssignment.box_id == box_id, BinAssignment.active == True)
    )).fetchall()
    cell_map = {r.BinAssignment.cell_id: r.Component for r in assignments}
    all_components = (await db.execute(select(Component).order_by(Component.barcode_id))).scalars().all()
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("box_grid.html", {
        "request": request,
        "box": box,
        "cell_map": cell_map,
        "all_components": all_components,
        "profile": profile,
    })

@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = (await db.execute(select(Project).order_by(Project.updated_at.desc()))).scalars().all()
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("projects.html", {
        "request": request, "projects": result, "profile": profile,
    })


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(project_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    from app.models.models import TodoItem, BOMItem
    proj = (await db.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()
    if not proj:
        raise HTTPException(404)
    todos = (await db.execute(
        select(TodoItem).where(TodoItem.project_id == project_id)
        .order_by(TodoItem.priority.desc(), TodoItem.created_at)
    )).scalars().all()
    bom_rows = (await db.execute(
        select(BOMItem, Component)
        .join(Component, Component.id == BOMItem.component_id, isouter=True)
        .where(BOMItem.project_id == project_id)
    )).fetchall()
    components_all = (await db.execute(select(Component).order_by(Component.barcode_id))).scalars().all()
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("project_detail.html", {
        "request": request,
        "project": proj,
        "todos": todos,
        "bom_rows": bom_rows,
        "components_all": components_all,
        "profile": profile,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    keys = (await db.execute(select(APIKey).order_by(APIKey.created_at.desc()))).scalars().all()
    return templates.TemplateResponse("settings.html", {
        "request": request, "profile": profile, "keys": keys,
    })


@app.get("/registry", response_class=HTMLResponse)
async def registry_page(request: Request, db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    all_paths = flatten_type_paths()

    counts_rows = (await db.execute(
        select(Component.type_path, func.count(Component.id))
        .group_by(Component.type_path)
        .order_by(func.count(Component.id).desc())
    )).all()

    path_counts = []
    for path, cnt in counts_rows:
        if not path:
            continue
        path_counts.append({"path": path, "count": int(cnt or 0)})

    return templates.TemplateResponse("registry.html", {
        "request": request,
        "profile": profile,
        "paths": all_paths,
        "path_counts": path_counts,
    })



@app.get("/components/{barcode_id}", response_class=HTMLResponse)
async def component_detail(barcode_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    from app.models.models import Footprint, BinAssignment, Box, BOMItem, Project, ComponentSupplier, Supplier, PurchaseOrderItem, PurchaseOrder, InventoryEvent
    comp = (await db.execute(select(Component).where(Component.barcode_id == barcode_id))).scalar_one_or_none()
    if not comp:
        raise HTTPException(404)
    ctype = (await db.execute(select(ComponentType).where(ComponentType.id == comp.type_id))).scalar_one_or_none()
    footprints = (await db.execute(select(Footprint).where(Footprint.component_id == comp.id))).scalars().all()
    for fp in footprints:
        fp.sigma_adjustment = int(fp.sigma_adjustment or 0)
        fp.effective_quantity = int(fp.quantity or 0) + fp.sigma_adjustment
    bins = (await db.execute(
        select(BinAssignment, Box).join(Box, Box.id == BinAssignment.box_id)
        .where(BinAssignment.component_id == comp.id, BinAssignment.active == True)
    )).fetchall()
    bins_with_box = [type("Bin", (), {"cell_id": r.BinAssignment.cell_id, "box": r.Box})() for r in bins]
    bom_projects = (await db.execute(
        select(Project).join(BOMItem, BOMItem.project_id == Project.id)
        .where(BOMItem.component_id == comp.id).distinct()
    )).scalars().all()
    cs_rows = (await db.execute(
        select(ComponentSupplier, Supplier).join(Supplier, Supplier.id == ComponentSupplier.supplier_id)
        .where(ComponentSupplier.component_id == comp.id)
    )).fetchall()
    component_suppliers = [type("CS", (), {**{c: getattr(r.ComponentSupplier, c) for c in ["id","sku","mpn","unit_price","pack_size","url","notes"]}, "supplier": r.Supplier})() for r in cs_rows]
    purchase_history = (await db.execute(
        select(PurchaseOrderItem, PurchaseOrder)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.order_id)
        .where(PurchaseOrderItem.component_id == comp.id)
        .order_by(PurchaseOrder.order_date.desc())
    )).fetchall()
    ph = [type("PH", (), {"quantity_ordered": r.PurchaseOrderItem.quantity_ordered, "quantity_received": r.PurchaseOrderItem.quantity_received, "order": r.PurchaseOrder})() for r in purchase_history]
    inventory_events = (await db.execute(
        select(InventoryEvent)
        .where(InventoryEvent.component_id == comp.id)
        .order_by(InventoryEvent.created_at.desc())
        .limit(60)
    )).scalars().all()
    all_suppliers = (await db.execute(select(Supplier).order_by(Supplier.name))).scalars().all()
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()

    # Generic component aggregation
    generic_stock = None
    generic_parent = None
    generic_children = []
    archetype_details = []
    if comp.is_generic:
        # Aggregate own + children stock
        children_result = await db.execute(select(Component).where(Component.parent_id == comp.id))
        generic_children = children_result.scalars().all()
        all_ids = [comp.id] + [c.id for c in generic_children]
        agg = await db.execute(
            select(func.coalesce(func.sum(Footprint.quantity + func.coalesce(Footprint.sigma_adjustment, 0)), 0))
            .where(Footprint.component_id.in_(all_ids))
        )
        generic_stock = int(agg.scalar() or 0)
    elif comp.parent_id:
        parent_result = await db.execute(select(Component).where(Component.id == comp.parent_id))
        generic_parent = parent_result.scalar_one_or_none()

    # Build type/archetype-specific details from direct fields + JSON type_data.
    type_data = comp.type_data if isinstance(comp.type_data, dict) else {}
    if comp.type_path:
        rules = get_fields_for_type(comp.type_path)
        seen = set()
        for field in rules.get("fields", []):
            val = getattr(comp, field, None)
            if val in (None, ""):
                val = type_data.get(field)
            if val in (None, ""):
                continue
            seen.add(field)
            archetype_details.append({
                "key": field,
                "label": field.replace("_", " ").title(),
                "value": val,
            })

        # Show additional inferred details even if not part of current schema node.
        for key, val in type_data.items():
            if key in seen or val in (None, ""):
                continue
            archetype_details.append({
                "key": key,
                "label": key.replace("_", " ").title(),
                "value": val,
            })

    return templates.TemplateResponse("component_detail.html", {
        "request": request, "comp": comp, "ctype": ctype, "footprints": footprints,
        "bins": bins_with_box, "used_in": bom_projects,
        "component_suppliers": component_suppliers, "purchase_history": ph,
        "all_suppliers": all_suppliers, "profile": profile,
        "generic_stock": generic_stock,
        "generic_parent": generic_parent,
        "generic_children": generic_children,
        "archetype_details": archetype_details,
        "inventory_events": inventory_events,
    })

@app.get("/scan", response_class=HTMLResponse)
async def scan_page(request: Request, db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("scan.html", {"request": request, "profile": profile})


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    suppliers_list = (await db.execute(select(Supplier).order_by(Supplier.name))).scalars().all()
    orders = (await db.execute(select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc()))).scalars().all()
    order_summary_rows = (await db.execute(
        select(
            PurchaseOrder.id,
            PurchaseOrder.reference,
            PurchaseOrder.status,
            PurchaseOrder.created_at,
            PurchaseOrder.order_date,
            PurchaseOrder.total_cost,
            Supplier.name.label("supplier_name"),
            func.count(PurchaseOrderItem.id).label("line_count"),
            func.coalesce(func.sum(PurchaseOrderItem.quantity_ordered - PurchaseOrderItem.quantity_received), 0).label("open_qty"),
        )
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id, isouter=True)
        .join(PurchaseOrderItem, PurchaseOrderItem.order_id == PurchaseOrder.id, isouter=True)
        .group_by(PurchaseOrder.id, Supplier.name)
        .order_by(PurchaseOrder.created_at.desc())
    )).all()
    order_summaries = [
        {
            "id": r.id,
            "reference": r.reference,
            "status": r.status,
            "created_at": r.created_at,
            "order_date": r.order_date,
            "total_cost": r.total_cost,
            "supplier_name": r.supplier_name,
            "line_count": int(r.line_count or 0),
            "open_qty": int(r.open_qty or 0),
        }
        for r in order_summary_rows
    ]
    components_list = (await db.execute(select(Component).order_by(Component.barcode_id))).scalars().all()
    kits_list = (await db.execute(select(Kit).order_by(Kit.barcode_id))).scalars().all()
    components_json = json.dumps([
        {"id": c.id, "name": c.name, "barcode_id": c.barcode_id}
        for c in components_list
    ])
    kits_json = json.dumps([
        {"id": k.id, "name": k.name, "barcode_id": k.barcode_id}
        for k in kits_list
    ])
    return templates.TemplateResponse("orders.html", {
        "request": request,
        "profile": profile,
        "suppliers": suppliers_list,
        "orders": orders,
        "order_summaries": order_summaries,
        "components": components_list,
        "kits": kits_list,
        "components_json": components_json,
        "kits_json": kits_json,
    })


@app.get("/kits", response_class=HTMLResponse)
async def kits_page(request: Request, db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    kits_rows = (await db.execute(select(Kit).order_by(Kit.barcode_id))).scalars().all()
    return templates.TemplateResponse("kits.html", {
        "request": request,
        "profile": profile,
        "kits": kits_rows,
    })


@app.get("/kits/{kit_id}", response_class=HTMLResponse)
async def kit_detail_page(kit_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    kit = (await db.execute(select(Kit).where(Kit.id == kit_id))).scalar_one_or_none()
    if not kit:
        raise HTTPException(404)
    rows = (await db.execute(
        select(KitComponent, Component)
        .join(Component, Component.id == KitComponent.component_id)
        .where(KitComponent.kit_id == kit_id)
        .order_by(KitComponent.position)
    )).all()
    return templates.TemplateResponse("kit_detail.html", {
        "request": request,
        "profile": profile,
        "kit": kit,
        "rows": rows,
    })
