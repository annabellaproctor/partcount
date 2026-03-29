from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from contextlib import asynccontextmanager
import os

from app.models.database import init_db, get_db
from app.models.models import Component, ComponentType, Box, Footprint, Project, Profile, APIKey, TodoItem, BOMItem
from app.routers import components, boxes, labels, projects, apikeys, suppliers, images, migrate
from app.services.ws_manager import manager

IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # seed default profile if not exists
    from app.models.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Profile).limit(1))
        if not result.scalar_one_or_none():
            db.add(Profile(name="Annabella", email="annabellaproctor@gmail.com", initials="AP"))
            await db.commit()
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


@app.get("/components", response_class=HTMLResponse)
async def components_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Component, ComponentType)
        .join(ComponentType, ComponentType.id == Component.type_id, isouter=True)
        .order_by(Component.barcode_id)
    )
    rows = result.fetchall()
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



@app.get("/components/{barcode_id}", response_class=HTMLResponse)
async def component_detail(barcode_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    from app.models.models import Footprint, BinAssignment, Box, BOMItem, Project, ComponentSupplier, Supplier, PurchaseOrderItem, PurchaseOrder
    comp = (await db.execute(select(Component).where(Component.barcode_id == barcode_id))).scalar_one_or_none()
    if not comp:
        raise HTTPException(404)
    ctype = (await db.execute(select(ComponentType).where(ComponentType.id == comp.type_id))).scalar_one_or_none()
    footprints = (await db.execute(select(Footprint).where(Footprint.component_id == comp.id))).scalars().all()
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
    all_suppliers = (await db.execute(select(Supplier).order_by(Supplier.name))).scalars().all()
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("component_detail.html", {
        "request": request, "comp": comp, "ctype": ctype, "footprints": footprints,
        "bins": bins_with_box, "used_in": bom_projects,
        "component_suppliers": component_suppliers, "purchase_history": ph,
        "all_suppliers": all_suppliers, "profile": profile,
    })

@app.get("/scan", response_class=HTMLResponse)
async def scan_page(request: Request, db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("scan.html", {"request": request, "profile": profile})
