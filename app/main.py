from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from contextlib import asynccontextmanager
import os

from app.models.database import init_db, get_db
from app.models.models import Component, ComponentType, Box, Footprint
from app.routers import components, boxes, labels
from app.services.ws_manager import manager

IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Lab Inventory", lifespan=lifespan)

app.include_router(components.router)
app.include_router(boxes.router)
app.include_router(labels.router)

app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")
app.mount("/static", StaticFiles(directory="/app/app/static"), name="static")

templates = Jinja2Templates(directory="/app/templates")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # client can send barcode scan events directly via WS
            if data.startswith("SCAN:"):
                barcode_id = data[5:].strip()
                await websocket.send_text(f'{{"event":"scan_ack","data":{{"barcode_id":"{barcode_id}"}}}}')
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    comp_count = await db.execute(select(func.count()).select_from(Component))
    box_count = await db.execute(select(func.count()).select_from(Box))
    recent = await db.execute(select(Component).order_by(Component.created_at.desc()).limit(10))
    return templates.TemplateResponse("index.html", {
        "request": request,
        "comp_count": comp_count.scalar(),
        "box_count": box_count.scalar(),
        "recent": recent.scalars().all(),
    })


@app.get("/components", response_class=HTMLResponse)
async def components_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Component, ComponentType)
        .join(ComponentType, ComponentType.id == Component.type_id, isouter=True)
        .order_by(Component.barcode_id)
    )
    rows = result.fetchall()
    types = await db.execute(select(ComponentType).order_by(ComponentType.name))
    return templates.TemplateResponse("components.html", {
        "request": request,
        "rows": rows,
        "types": types.scalars().all(),
    })


@app.get("/boxes", response_class=HTMLResponse)
async def boxes_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Box).order_by(Box.label))
    return templates.TemplateResponse("boxes.html", {
        "request": request,
        "boxes": result.scalars().all(),
    })


@app.get("/scan", response_class=HTMLResponse)
async def scan_page(request: Request):
    return templates.TemplateResponse("scan.html", {"request": request})
