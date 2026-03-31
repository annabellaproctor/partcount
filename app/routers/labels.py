from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.database import get_db
from app.models.models import Component, BinAssignment, Box
from app.services.barcode_svc import generate_code128_svg
from app.services.markings_svg import build_markings_svg

router = APIRouter(prefix="/labels", tags=["labels"])


class MarkingsRequest(BaseModel):
  text: str | None = None
  tokens: list[str] | None = None
  width: int = 512
  height: int = 512


class MarkingsApplyRequest(MarkingsRequest):
  component_id: str


@router.post("/markings-svg")
async def generate_markings_svg(req: MarkingsRequest):
    if not req.text and not req.tokens:
        raise HTTPException(400, "Provide text or tokens")
    return build_markings_svg(
        text=req.text,
        tokens=req.tokens,
        width=req.width,
        height=req.height,
    )


@router.get("/markings-svg")
async def generate_markings_svg_get(text: str, width: int = 512, height: int = 512):
  if not text or len(text.strip()) < 1:
    raise HTTPException(400, "Text is required")
  return build_markings_svg(text=text, width=width, height=height)


@router.post("/markings-apply")
async def apply_markings_as_component_image(req: MarkingsApplyRequest, db: AsyncSession = Depends(get_db)):
  if not req.text and not req.tokens:
    raise HTTPException(400, "Provide text or tokens")

  comp = (await db.execute(select(Component).where(Component.id == req.component_id))).scalar_one_or_none()
  if not comp:
    raise HTTPException(404, "Component not found")

  result = build_markings_svg(
    text=req.text,
    tokens=req.tokens,
    width=req.width,
    height=req.height,
  )
  comp.image_path = result["image_data_url"]
  return {
    "updated": True,
    "component_id": comp.id,
    "image_path": comp.image_path,
    "entries": result["entries"],
  }


@router.get("/print/{barcode_id}", response_class=HTMLResponse)
async def print_label(barcode_id: str, db: AsyncSession = Depends(get_db)):
    """Printable label page — open in browser, Ctrl+P"""
    result = await db.execute(select(Component).where(Component.barcode_id == barcode_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404)

    barcode_svg = generate_code128_svg(barcode_id)

    title = comp.short_title or comp.value or comp.name

    html = f"""<!DOCTYPE html>
<html>
<head>
<style>
  @page {{ size: 1in 0.5in; margin: 0; }}
  body {{ margin: 0; font-family: monospace; }}
  .label {{
    width: 1in; height: 0.5in;
    display: flex; flex-direction: column;
    justify-content: center; align-items: center;
    padding: 1mm; box-sizing: border-box;
  }}
  .value {{ font-size: 7pt; font-weight: bold; text-align: center; }}
  .id {{ font-size: 5pt; color: #666; }}
  svg {{ width: 0.9in; height: 0.2in; }}
</style>
</head>
<body onload="window.print()">
  <div class="label">
    <div class="value">{title}</div>
    <div class="id">{barcode_id} · {comp.package or ''}</div>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


@router.get("/print-inside/{barcode_id}", response_class=HTMLResponse)
async def print_inside_label(barcode_id: str, db: AsyncSession = Depends(get_db)):
    """Inside-lid barcode label — Code 128 only"""
    result = await db.execute(select(Component).where(Component.barcode_id == barcode_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404)

    barcode_svg = generate_code128_svg(barcode_id)

    html = f"""<!DOCTYPE html>
<html>
<head>
<style>
  @page {{ size: 1in 0.5in; margin: 0; }}
  body {{ margin: 0; }}
  .label {{
    width: 1in; height: 0.5in;
    display: flex; flex-direction: column;
    justify-content: center; align-items: center;
  }}
  svg {{ width: 0.95in; height: 0.35in; }}
  .id {{ font-size: 5pt; font-family: monospace; }}
</style>
</head>
<body onload="window.print()">
  <div class="label">
    {barcode_svg}
    <div class="id">{barcode_id}</div>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


@router.get("/sheet", response_class=HTMLResponse)
async def print_sheet(db: AsyncSession = Depends(get_db)):
    """Full sheet of all component labels for batch printing"""
    result = await db.execute(select(Component).order_by(Component.barcode_id))
    components = result.scalars().all()

    labels_html = ""
    for comp in components:
        title = comp.short_title or comp.value or comp.name
        labels_html += f"""
        <div class="label">
          <div class="value">{title}</div>
          <div class="id">{comp.barcode_id} · {comp.package or ''}</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<style>
  @page {{ size: letter; margin: 0.5in; }}
  body {{ margin: 0; font-family: monospace; }}
  .sheet {{ display: flex; flex-wrap: wrap; gap: 2mm; }}
  .label {{
    width: 1in; height: 0.5in;
    border: 0.5pt solid #ccc;
    display: flex; flex-direction: column;
    justify-content: center; align-items: center;
    font-size: 6pt; text-align: center;
    padding: 1mm; box-sizing: border-box;
  }}
  .value {{ font-weight: bold; }}
  .id {{ color: #666; font-size: 5pt; }}
</style>
</head>
<body onload="window.print()">
  <div class="sheet">{labels_html}</div>
</body>
</html>"""
    return HTMLResponse(html)
