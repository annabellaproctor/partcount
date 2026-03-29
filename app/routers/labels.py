from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.database import get_db
from app.models.models import Component, BinAssignment, Box
from app.services.barcode_svc import generate_code128_svg

router = APIRouter(prefix="/labels", tags=["labels"])


@router.get("/print/{barcode_id}", response_class=HTMLResponse)
async def print_label(barcode_id: str, db: AsyncSession = Depends(get_db)):
    """Printable label page — open in browser, Ctrl+P"""
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
    <div class="value">{comp.value or comp.name}</div>
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
        labels_html += f"""
        <div class="label">
          <div class="value">{comp.value or comp.name}</div>
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
