from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.database import get_db
from app.models.models import Project, TodoItem, BOMItem, Component, Profile
from app.services.ws_manager import manager

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("/")
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).order_by(Project.updated_at.desc()))
    return result.scalars().all()


@router.post("/")
async def create_project(
    name: str = Form(...),
    description: str = Form(None),
    status: str = Form("active"),
    db: AsyncSession = Depends(get_db),
):
    profile = await _get_or_create_profile(db)
    proj = Project(name=name, description=description, status=status, profile_id=profile.id)
    db.add(proj)
    await db.flush()
    await manager.broadcast("project_created", {"id": proj.id, "name": name})
    return {"id": proj.id}


@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    proj = await _get_project(project_id, db)
    todos = await db.execute(select(TodoItem).where(TodoItem.project_id == project_id).order_by(TodoItem.priority.desc(), TodoItem.created_at))
    bom = await db.execute(
        select(BOMItem, Component)
        .join(Component, Component.id == BOMItem.component_id, isouter=True)
        .where(BOMItem.project_id == project_id)
    )
    return {
        "project": proj,
        "todos": todos.scalars().all(),
        "bom": [{"item": r.BOMItem, "component": r.Component} for r in bom.fetchall()],
    }


@router.patch("/{project_id}/status")
async def update_status(project_id: str, status: str = Form(...), db: AsyncSession = Depends(get_db)):
    proj = await _get_project(project_id, db)
    proj.status = status
    return {"status": status}


# --- TODO ---

@router.post("/{project_id}/todos")
async def add_todo(
    project_id: str,
    text: str = Form(...),
    priority: int = Form(0),
    db: AsyncSession = Depends(get_db),
):
    todo = TodoItem(project_id=project_id, text=text, priority=priority)
    db.add(todo)
    await db.flush()
    return {"id": todo.id}


@router.patch("/todos/{todo_id}/toggle")
async def toggle_todo(todo_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TodoItem).where(TodoItem.id == todo_id))
    todo = result.scalar_one_or_none()
    if not todo:
        raise HTTPException(404)
    todo.done = not todo.done
    return {"done": todo.done}


@router.delete("/todos/{todo_id}")
async def delete_todo(todo_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TodoItem).where(TodoItem.id == todo_id))
    todo = result.scalar_one_or_none()
    if not todo:
        raise HTTPException(404)
    await db.delete(todo)
    return {"deleted": True}


# --- BOM ---

@router.post("/{project_id}/bom")
async def add_bom_item(
    project_id: str,
    description: str = Form(...),
    quantity_needed: int = Form(1),
    quantity_have: int = Form(0),
    component_id: str = Form(None),
    notes: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    item = BOMItem(
        project_id=project_id,
        description=description,
        quantity_needed=quantity_needed,
        quantity_have=quantity_have,
        component_id=component_id or None,
        notes=notes,
    )
    db.add(item)
    await db.flush()
    return {"id": item.id}


@router.patch("/bom/{item_id}/sourced")
async def toggle_sourced(item_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BOMItem).where(BOMItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404)
    item.sourced = not item.sourced
    return {"sourced": item.sourced}


@router.delete("/bom/{item_id}")
async def delete_bom_item(item_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(BOMItem).where(BOMItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404)
    await db.delete(item)
    return {"deleted": True}


async def _get_project(project_id: str, db: AsyncSession) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    proj = result.scalar_one_or_none()
    if not proj:
        raise HTTPException(404, "Project not found")
    return proj


async def _get_or_create_profile(db: AsyncSession) -> Profile:
    result = await db.execute(select(Profile).limit(1))
    profile = result.scalar_one_or_none()
    if not profile:
        profile = Profile(name="Annabella", email="annabellaproctor@gmail.com", initials="AP")
        db.add(profile)
        await db.flush()
    return profile
