from datetime import datetime
import html
import json
import math
import random

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.database import get_db
from app.models.models import Component, LabelPrintProfile
from app.services.barcode_svc import generate_code128_svg
from app.services.markings_svg import build_markings_svg
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/labels", tags=["labels"])
templates = Jinja2Templates(directory="/app/templates")


DEFAULT_LABEL_SETTINGS = {
  "page_width_in": 8.5,
  "page_height_in": 11.0,
  "margin_top_in": 0.2,
  "margin_right_in": 0.2,
  "margin_bottom_in": 0.2,
  "margin_left_in": 0.2,
  "cell_width_in": 1.0,
  "cell_height_in": 0.5,
  "cut_width_in": 1.0,
  "cut_height_in": 0.5,
  "gap_x_in": 0.0,
  "gap_y_in": 0.0,
  "corner_radius_mm": 0.6,
  "show_cut_grid": True,
  "cut_marker_style": "cross",
  "show_image": False,
  "image_min_height_in": 0.8,
  "name_font_pt": 8,
  "id_font_pt": 6,
  "barcode_height_ratio": 0.88,
  "barcode_max_height_in": 0.42,
}


PRESET_PROFILES = [
  {
    "name": "Avery 0.5x1 for Small Boxes",
    "settings": {
      **DEFAULT_LABEL_SETTINGS,
      "cell_width_in": 1.0,
      "cell_height_in": 0.5,
      "cut_width_in": 1.0,
      "cut_height_in": 0.5,
      "name_font_pt": 8,
      "id_font_pt": 6,
      "show_image": False,
    },
  },
  {
    "name": "Avery 0.5x1.75 for Small Boxes",
    "settings": {
      **DEFAULT_LABEL_SETTINGS,
      "cell_width_in": 1.75,
      "cell_height_in": 0.5,
      "cut_width_in": 1.75,
      "cut_height_in": 0.5,
      "name_font_pt": 8,
      "id_font_pt": 6,
      "show_image": False,
    },
  },
  {
    "name": "Avery 0.5x1.75 for Medium Boxes",
    "settings": {
      **DEFAULT_LABEL_SETTINGS,
      "cell_width_in": 1.75,
      "cell_height_in": 0.6,
      "cut_width_in": 1.75,
      "cut_height_in": 0.6,
      "name_font_pt": 9,
      "id_font_pt": 6,
      "show_image": True,
      "image_min_height_in": 0.6,
    },
  },
]


def _clean_settings(settings: dict | None) -> dict:
  s = dict(DEFAULT_LABEL_SETTINGS)
  if isinstance(settings, dict):
    s.update(settings)

  def _clamp_number(key: str, lo: float, hi: float):
    try:
      v = float(s.get(key, s[key]))
    except Exception:
      v = float(DEFAULT_LABEL_SETTINGS[key])
    s[key] = max(lo, min(hi, v))

  _clamp_number("page_width_in", 1.0, 24.0)
  _clamp_number("page_height_in", 1.0, 24.0)
  _clamp_number("margin_top_in", 0.0, 2.0)
  _clamp_number("margin_right_in", 0.0, 2.0)
  _clamp_number("margin_bottom_in", 0.0, 2.0)
  _clamp_number("margin_left_in", 0.0, 2.0)
  _clamp_number("cell_width_in", 0.2, 8.0)
  _clamp_number("cell_height_in", 0.2, 8.0)
  _clamp_number("cut_width_in", 0.1, 12.0)
  _clamp_number("cut_height_in", 0.1, 12.0)
  _clamp_number("gap_x_in", 0.0, 2.0)
  _clamp_number("gap_y_in", 0.0, 2.0)
  _clamp_number("corner_radius_mm", 0.0, 12.0)
  _clamp_number("image_min_height_in", 0.2, 4.0)
  _clamp_number("name_font_pt", 4.0, 24.0)
  _clamp_number("id_font_pt", 4.0, 18.0)
  _clamp_number("barcode_height_ratio", 0.3, 1.0)
  _clamp_number("barcode_max_height_in", 0.15, 3.0)

  s["show_cut_grid"] = bool(s.get("show_cut_grid", True))
  s["show_image"] = bool(s.get("show_image", False))
  s["cut_marker_style"] = str(s.get("cut_marker_style", "cross")).lower()
  if s["cut_marker_style"] not in {"cross", "dot"}:
    s["cut_marker_style"] = "cross"
  return s


def _compute_grid(settings: dict) -> tuple[int, int]:
  usable_w = settings["page_width_in"] - settings["margin_left_in"] - settings["margin_right_in"]
  usable_h = settings["page_height_in"] - settings["margin_top_in"] - settings["margin_bottom_in"]
  cw, ch = settings["cell_width_in"], settings["cell_height_in"]
  gx, gy = settings["gap_x_in"], settings["gap_y_in"]

  cols = max(1, int(math.floor((usable_w + gx) / (cw + gx))))
  rows = max(1, int(math.floor((usable_h + gy) / (ch + gy))))
  return cols, rows


def _build_front_cell(comp: Component, settings: dict) -> str:
  title = html.escape((comp.short_title or comp.name or "").strip())
  bid = html.escape(comp.barcode_id or "")
  show_image = bool(settings.get("show_image")) and settings["cell_height_in"] >= settings["image_min_height_in"] and bool(comp.image_path)
  img_html = ""
  if show_image:
    img_html = f'<div class="front-img-wrap"><img class="front-img" src="{html.escape(comp.image_path)}" alt=""></div>'
  return (
    '<div class="front">'
    f'{img_html}'
    f'<div class="front-name">{title}</div>'
    f'<div class="front-id">ID: {bid}</div>'
    '</div>'
  )


def _build_barcode_cell(comp: Component, settings: dict) -> str:
  raw_svg = generate_code128_svg(comp.barcode_id)
  svg = raw_svg
  if "<svg" in raw_svg:
    svg = raw_svg.replace("<svg", '<svg preserveAspectRatio="none"', 1)
  return f'<div class="barcode-wrap">{svg}</div>'


def _build_calibration_cell(rev: int, idx: int, style: str) -> str:
  rnd = random.Random((rev * 100_003) + idx)
  micro = []
  for i in range(5):
    x = 10 + int(rnd.random() * 80)
    y = 12 + i * 14 + int(rnd.random() * 4)
    micro.append(f'<div class="micro {style}" style="left:{x}%;top:{y}%;"></div>')
  return (
    '<div class="calibration">'
    f'{"".join(micro)}'
    f'<div class="cal-rev">rev {rev}.{idx+1}</div>'
    '</div>'
  )


async def _ensure_presets(db: AsyncSession):
  existing = (await db.execute(select(LabelPrintProfile))).scalars().all()
  names = {p.name for p in existing}
  changed = False
  if not existing:
    for i, p in enumerate(PRESET_PROFILES):
      db.add(LabelPrintProfile(
        name=p["name"],
        settings=_clean_settings(p["settings"]),
        is_default=(i == 0),
      ))
      changed = True
  else:
    for p in PRESET_PROFILES:
      if p["name"] in names:
        continue
      db.add(LabelPrintProfile(
        name=p["name"],
        settings=_clean_settings(p["settings"]),
        is_default=False,
      ))
      changed = True
  if changed:
    await db.flush()


class LabelProfileRequest(BaseModel):
  name: str
  settings: dict


class LabelProfileUpdateRequest(BaseModel):
  name: str | None = None
  settings: dict | None = None
  is_default: bool | None = None


class MarkingsRequest(BaseModel):
  text: str | None = None
  tokens: list[str] | None = None
  width: int = 512
  height: int = 512


class MarkingsApplyRequest(MarkingsRequest):
  component_id: str


@router.get("/designer", response_class=HTMLResponse)
async def label_designer_page(request: Request, db: AsyncSession = Depends(get_db)):
  from app.models.models import Profile
  await _ensure_presets(db)
  profile = (await db.execute(select(Profile).limit(1))).scalar_one_or_none()
  return templates.TemplateResponse("labels_designer.html", {
    "request": request,
    "profile": profile,
  })


@router.get("/profiles")
async def list_label_profiles(db: AsyncSession = Depends(get_db)):
  await _ensure_presets(db)
  rows = (await db.execute(select(LabelPrintProfile).order_by(LabelPrintProfile.name))).scalars().all()
  return [
    {
      "id": p.id,
      "name": p.name,
      "settings": _clean_settings(p.settings if isinstance(p.settings, dict) else {}),
      "is_default": bool(p.is_default),
      "calibration_revision": int(p.calibration_revision or 0),
    }
    for p in rows
  ]


@router.post("/profiles")
async def create_label_profile(req: LabelProfileRequest, db: AsyncSession = Depends(get_db)):
  name = (req.name or "").strip()
  if not name:
    raise HTTPException(400, "Profile name is required")
  exists = (await db.execute(select(LabelPrintProfile).where(LabelPrintProfile.name == name))).scalar_one_or_none()
  if exists:
    raise HTTPException(409, "Profile name already exists")
  profile = LabelPrintProfile(
    name=name,
    settings=_clean_settings(req.settings),
    is_default=False,
  )
  db.add(profile)
  await db.flush()
  return {"id": profile.id, "name": profile.name}


@router.put("/profiles/{profile_id}")
async def update_label_profile(profile_id: str, req: LabelProfileUpdateRequest, db: AsyncSession = Depends(get_db)):
  row = (await db.execute(select(LabelPrintProfile).where(LabelPrintProfile.id == profile_id))).scalar_one_or_none()
  if not row:
    raise HTTPException(404, "Profile not found")
  if req.name is not None:
    name = req.name.strip()
    if not name:
      raise HTTPException(400, "Profile name is required")
    dupe = (await db.execute(select(LabelPrintProfile).where(LabelPrintProfile.name == name, LabelPrintProfile.id != profile_id))).scalar_one_or_none()
    if dupe:
      raise HTTPException(409, "Profile name already exists")
    row.name = name
  if req.settings is not None:
    row.settings = _clean_settings(req.settings)
  if req.is_default is True:
    all_rows = (await db.execute(select(LabelPrintProfile))).scalars().all()
    for p in all_rows:
      p.is_default = (p.id == row.id)
  return {"updated": True}


@router.get("/print-sheet", response_class=HTMLResponse)
async def print_sheet_designer(
  profile_id: str,
  mode: str = Query("front", pattern="^(front|barcode|calibration)$"),
  q: str | None = None,
  barcode_ids: str | None = None,
  limit: int = 500,
  db: AsyncSession = Depends(get_db),
):
  row = (await db.execute(select(LabelPrintProfile).where(LabelPrintProfile.id == profile_id))).scalar_one_or_none()
  if not row:
    raise HTTPException(404, "Profile not found")

  settings = _clean_settings(row.settings if isinstance(row.settings, dict) else {})
  cols, rows = _compute_grid(settings)
  capacity = cols * rows
  take = max(1, min(limit, capacity if mode == "calibration" else 2000))

  stmt = select(Component).order_by(Component.barcode_id)
  if barcode_ids:
    ids = [x.strip().upper() for x in barcode_ids.split(",") if x.strip()]
    if ids:
      stmt = stmt.where(Component.barcode_id.in_(ids))
  elif q and q.strip():
    like = f"%{q.strip()}%"
    stmt = stmt.where(
      (Component.barcode_id.ilike(like)) |
      (Component.name.ilike(like)) |
      (Component.value.ilike(like)) |
      (Component.package.ilike(like))
    )

  comps = (await db.execute(stmt.limit(take))).scalars().all()

  if mode == "calibration":
    row.calibration_revision = int(row.calibration_revision or 0) + 1
    rev = row.calibration_revision
    count = capacity
  else:
    rev = int(row.calibration_revision or 0)
    count = len(comps)

  cells = []
  for i in range(capacity):
    if mode == "calibration":
      inner = _build_calibration_cell(rev, i, settings["cut_marker_style"])
      classes = "cell calibration-cell"
    else:
      if i >= len(comps):
        inner = ""
      elif mode == "barcode":
        inner = _build_barcode_cell(comps[i], settings)
      else:
        inner = _build_front_cell(comps[i], settings)
      classes = "cell"

    cross = ""
    if settings["show_cut_grid"]:
      cross = '<div class="cut-x cut-a"></div><div class="cut-x cut-b"></div>'

    cells.append(f'<div class="{classes}">{cross}{inner}</div>')

  sheet = "".join(cells)
  radius_mm = settings["corner_radius_mm"]
  barcode_css_h = min(settings["barcode_max_height_in"], settings["cell_height_in"] * settings["barcode_height_ratio"])

  html_doc = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"UTF-8\">
  <title>Label Print</title>
  <style>
  @page {{ size: {settings['page_width_in']}in {settings['page_height_in']}in; margin: 0; }}
  html, body {{ margin: 0; padding: 0; background: #fff; color: #000; font-family: Arial, Helvetica, sans-serif; }}
  .page {{
    width: {settings['page_width_in']}in;
    height: {settings['page_height_in']}in;
    box-sizing: border-box;
    padding: {settings['margin_top_in']}in {settings['margin_right_in']}in {settings['margin_bottom_in']}in {settings['margin_left_in']}in;
    display: grid;
    grid-template-columns: repeat({cols}, {settings['cell_width_in']}in);
    grid-template-rows: repeat({rows}, {settings['cell_height_in']}in);
    column-gap: {settings['gap_x_in']}in;
    row-gap: {settings['gap_y_in']}in;
    overflow: hidden;
  }}
  .cell {{
    position: relative;
    border-radius: {radius_mm}mm;
    overflow: hidden;
    box-sizing: border-box;
  }}
  .cut-x {{
    position: absolute;
    left: 0; top: 0;
    width: 100%; height: 100%;
    pointer-events: none;
    opacity: {0.22 if mode == 'calibration' else 0.14};
  }}
  .cut-a {{ border-top: 0.3pt solid #777; transform: rotate(25deg) scale(1.35); transform-origin: center; }}
  .cut-b {{ border-top: 0.3pt solid #777; transform: rotate(-25deg) scale(1.35); transform-origin: center; }}
  .front {{
    position: absolute; inset: 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 1.5mm;
    padding: 1.2mm;
    box-sizing: border-box;
    text-align: center;
  }}
  .front-img-wrap {{ height: 42%; width: 100%; display: flex; align-items: center; justify-content: center; }}
  .front-img {{ max-height: 100%; max-width: 95%; object-fit: contain; }}
  .front-name {{
    font-family: 'Arial Narrow', Arial, sans-serif;
    font-weight: 700;
    font-size: {settings['name_font_pt']}pt;
    line-height: 1.1;
    width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .front-id {{
    font-family: 'Arial Narrow', Arial, sans-serif;
    font-size: {settings['id_font_pt']}pt;
    line-height: 1;
  }}
  .barcode-wrap {{
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    padding: 0.8mm;
    box-sizing: border-box;
  }}
  .barcode-wrap svg {{ width: 96%; height: {barcode_css_h}in; display: block; }}
  .barcode-wrap text {{ font-size: 5pt !important; }}
  .calibration {{ position: absolute; inset: 0; }}
  .micro {{ position: absolute; width: 1.2mm; height: 1.2mm; opacity: 0.35; }}
  .micro.dot {{ border-radius: 50%; background: #000; }}
  .micro.cross::before, .micro.cross::after {{
    content: ''; position: absolute; left: 50%; top: 50%; background: #000; transform: translate(-50%, -50%);
  }}
  .micro.cross::before {{ width: 1.2mm; height: 0.2mm; }}
  .micro.cross::after {{ width: 0.2mm; height: 1.2mm; }}
  .cal-rev {{
    position: absolute; right: 0.7mm; bottom: 0.4mm;
    font-size: 4.5pt; opacity: 0.42; font-family: monospace;
  }}
  </style>
</head>
<body onload=\"window.print()\">
  <div class=\"page\">{sheet}</div>
</body>
</html>"""
  return HTMLResponse(html_doc)


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
  query_text = (req.text or "").strip()
  if not query_text and req.tokens:
    query_text = " ".join([str(t).strip() for t in req.tokens if str(t).strip()])
  comp.image_path = result["image_data_url"]
  comp.image_query = query_text or None
  return {
    "updated": True,
    "component_id": comp.id,
    "image_path": comp.image_path,
    "image_query": comp.image_query,
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
