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
from app.routers import components, boxes, labels, projects, apikeys
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


@app.get("/scan", response_class=HTMLResponse)
async def scan_page(request: Request, db: AsyncSession = Depends(get_db)):
    profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
    return templates.TemplateResponse("scan.html", {"request": request, "profile": profile})
