from datetime import datetime
import html
import json
import math
import random
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from app.models.database import get_db
from app.models.models import Component, LabelPrintProfile, BinAssignment, Box
from app.services.barcode_svc import generate_code128_svg
from app.services.markings_svg import build_markings_svg
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/labels", tags=["labels"])
templates = Jinja2Templates(directory="/app/templates")

SHEET_WORDS = [
  "horse", "car", "bridge", "ember", "copper", "maple", "river", "delta",
  "radar", "circuit", "anchor", "pine", "falcon", "orbit", "grain", "forge",
]


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
  "calibration_pattern": "random_t",
  "calibration_seed": 1337,
  "calibration_runs": 3,
  "calibration_sequence": "same_seed_then_random",
  "calibration_mark_count": 12,
  "calibration_run_label": "RUN",
  "calibration_corner_marks": False,
  "calibration_corner_style": "dot",
  "calibration_line_style": "solid",
  "comp_shift_x_mm": 0.0,
  "comp_shift_y_mm": 0.0,
  "comp_skew_x_deg": 0.0,
  "comp_skew_y_deg": 0.0,
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
  _clamp_number("calibration_seed", 0, 9_999_999)
  _clamp_number("calibration_runs", 1, 9)
  _clamp_number("calibration_mark_count", 1, 200)
  _clamp_number("comp_shift_x_mm", -10.0, 10.0)
  _clamp_number("comp_shift_y_mm", -10.0, 10.0)
  _clamp_number("comp_skew_x_deg", -2.0, 2.0)
  _clamp_number("comp_skew_y_deg", -2.0, 2.0)

  s["show_cut_grid"] = bool(s.get("show_cut_grid", True))
  s["show_image"] = bool(s.get("show_image", False))
  s["cut_marker_style"] = str(s.get("cut_marker_style", "cross")).lower()
  if s["cut_marker_style"] not in {"cross", "dot"}:
    s["cut_marker_style"] = "cross"

  s["calibration_pattern"] = str(s.get("calibration_pattern", "random_t")).lower()
  if s["calibration_pattern"] not in {"random_t", "outer_cross", "random_full_cross"}:
    s["calibration_pattern"] = "random_t"

  s["calibration_sequence"] = str(s.get("calibration_sequence", "same_seed_then_random")).lower()
  if s["calibration_sequence"] not in {"same_seed_then_random", "repeat_seed", "always_random"}:
    s["calibration_sequence"] = "same_seed_then_random"

  s["calibration_seed"] = int(round(s.get("calibration_seed", 1337)))
  s["calibration_runs"] = int(round(s.get("calibration_runs", 3)))
  s["calibration_mark_count"] = int(round(s.get("calibration_mark_count", 12)))
  s["calibration_run_label"] = (str(s.get("calibration_run_label", "RUN")).strip() or "RUN")[:16]
  s["calibration_corner_marks"] = bool(s.get("calibration_corner_marks", False))
  s["calibration_corner_style"] = str(s.get("calibration_corner_style", "dot")).lower()
  if s["calibration_corner_style"] not in {"dot", "inverse_angle"}:
    s["calibration_corner_style"] = "dot"
  s["calibration_line_style"] = str(s.get("calibration_line_style", "solid")).lower()
  if s["calibration_line_style"] not in {"solid", "dashed", "dotted", "mixed"}:
    s["calibration_line_style"] = "solid"
  return s


def _compute_grid(settings: dict) -> tuple[int, int]:
  usable_w = settings["page_width_in"] - settings["margin_left_in"] - settings["margin_right_in"]
  usable_h = settings["page_height_in"] - settings["margin_top_in"] - settings["margin_bottom_in"]
  cw, ch = settings["cell_width_in"], settings["cell_height_in"]
  gx, gy = settings["gap_x_in"], settings["gap_y_in"]

  cols = max(1, int(math.floor((usable_w + gx) / (cw + gx))))
  rows = max(1, int(math.floor((usable_h + gy) / (ch + gy))))
  return cols, rows


def _new_sheet_code() -> str:
  rnd = random.SystemRandom()
  return f"{rnd.choice(SHEET_WORDS)}-{rnd.randint(2,9)}-{rnd.choice(SHEET_WORDS)}-{rnd.randint(2,9)}-{rnd.choice(SHEET_WORDS)}"


def _cell_id_to_row_col(cell_id: str) -> tuple[int, int]:
  raw = (cell_id or "").strip().upper()
  m_rc = re.match(r"^R(\d+)C(\d+)$", raw)
  if m_rc:
    return int(m_rc.group(1)), int(m_rc.group(2))

  m_grid = re.match(r"^([A-Z]+)(\d+)$", raw)
  if m_grid:
    letters = m_grid.group(1)
    row = 0
    for ch in letters:
      row = (row * 26) + (ord(ch) - ord('A') + 1)
    row -= 1
    col = int(m_grid.group(2)) - 1
    return max(0, row), max(0, col)

  nums = re.findall(r"\d+", raw)
  if len(nums) >= 2:
    return int(nums[0]), int(nums[1])
  if len(nums) == 1:
    return int(nums[0]), 9999
  return 9999, 9999


async def _component_sort_slots(db: AsyncSession) -> dict[str, tuple[int, str, int, int]]:
  q = (
    select(BinAssignment.component_id, Box.slot_index, Box.label, BinAssignment.cell_id)
    .join(Box, Box.id == BinAssignment.box_id)
    .where(BinAssignment.active.is_(True))
  )
  rows = (await db.execute(q)).all()
  out: dict[str, tuple[int, str, int, int]] = {}
  for comp_id, slot_idx, box_label, cell_id in rows:
    r, c = _cell_id_to_row_col(cell_id or "")
    cand = (int(slot_idx or 0), str(box_label or "~"), r, c)
    prev = out.get(comp_id)
    if prev is None or cand < prev:
      out[comp_id] = cand
  return out


def _build_sheet_margin_headers(settings: dict, cols: int, rows: int, sheet_code: str, printed_at: str) -> str:
  ml = float(settings["margin_left_in"])
  mt = float(settings["margin_top_in"])
  cw = float(settings["cell_width_in"])
  ch = float(settings["cell_height_in"])
  gx = float(settings["gap_x_in"])
  gy = float(settings["gap_y_in"])
  pw = float(settings["page_width_in"])
  ph = float(settings["page_height_in"])

  col_marks = []
  for c in range(cols):
    x = ml + (c * (cw + gx)) + (cw / 2.0)
    col_marks.append(f'<div class="sheet-col top" style="left:{x:.6f}in;">C{c}</div>')
    col_marks.append(f'<div class="sheet-col bottom" style="left:{x:.6f}in;">C{c}</div>')

  row_marks = []
  for r in range(rows):
    y = mt + (r * (ch + gy)) + (ch / 2.0)
    row_marks.append(f'<div class="sheet-row left" style="top:{y:.6f}in;">R{r}</div>')
    row_marks.append(f'<div class="sheet-row right" style="top:{y:.6f}in;">R{r}</div>')

  return (
    f'<div class="sheet-meta top">BARCODE SHEET {html.escape(sheet_code)} · {html.escape(printed_at)}</div>'
    f'<div class="sheet-meta bottom">BARCODE SHEET {html.escape(sheet_code)} · {html.escape(printed_at)}</div>'
    f'<div class="sheet-meta-rail">{"".join(col_marks)}{"".join(row_marks)}</div>'
    f'<div class="sheet-corners-note tl" style="left:0.06in;top:{(mt * 0.5):.6f}in;">{html.escape(sheet_code)}</div>'
    f'<div class="sheet-corners-note br" style="right:0.06in;bottom:{(max(0.04, (ph - ((mt + rows * ch) + max(0, rows-1) * gy)) * 0.5)):.6f}in;">{html.escape(sheet_code)}</div>'
  )


async def _record_barcode_print_job(
  db: AsyncSession,
  profile_id: str,
  sheet_code: str,
  placements: list[dict],
):
  job_id = str(uuid.uuid4())
  await db.execute(
    text(
      """
      INSERT INTO barcode_print_jobs (id, sheet_code, profile_id, printed_at)
      VALUES (:id, :sheet_code, :profile_id, NOW())
      """
    ),
    {"id": job_id, "sheet_code": sheet_code, "profile_id": profile_id},
  )

  if placements:
    await db.execute(
      text(
        """
        INSERT INTO barcode_print_items (
          id, job_id, component_id, barcode_id, sheet_row, sheet_col, cell_slot, box_label, box_row, box_col
        ) VALUES (
          :id, :job_id, :component_id, :barcode_id, :sheet_row, :sheet_col, :cell_slot, :box_label, :box_row, :box_col
        )
        """
      ),
      [
        {
          "id": str(uuid.uuid4()),
          "job_id": job_id,
          "component_id": p.get("component_id"),
          "barcode_id": p.get("barcode_id"),
          "sheet_row": p.get("sheet_row"),
          "sheet_col": p.get("sheet_col"),
          "cell_slot": p.get("cell_slot"),
          "box_label": p.get("box_label"),
          "box_row": p.get("box_row"),
          "box_col": p.get("box_col"),
        }
        for p in placements
      ],
    )


def _normalize_barcode_for_print(raw: str | None) -> str:
  return re.sub(r"[^A-Za-z0-9]", "", (raw or "").upper())


def _title_with_hyphen_breaks(raw: str | None) -> str:
  safe = html.escape((raw or "").strip())
  return safe.replace("-", "-<wbr>")


def _build_front_cell(comp: Component, settings: dict) -> str:
  title = _title_with_hyphen_breaks(comp.short_title or comp.name or "")
  bid = html.escape(_normalize_barcode_for_print(comp.barcode_id))

  # Each printable cell is split into two micro labels for the 14.9x9.9mm workflow.
  micro = (
    '<div class="front-mini">'
    f'<div class="front-name">{title}</div>'
    f'<div class="front-id">ID: {bid}</div>'
    '</div>'
  )
  return (
    '<div class="front front-dual">'
    f'{micro}{micro}'
    '<div class="sticker-zone" aria-hidden="true"></div>'
    '</div>'
  )


def _build_barcode_cell(comp: Component, settings: dict) -> str:
  bid = _normalize_barcode_for_print(comp.barcode_id)
  raw_svg = generate_code128_svg(bid)
  # Remove text captions so bars can fill the full target block consistently.
  raw_svg = re.sub(r"<text[^>]*>.*?</text>", "", raw_svg, flags=re.IGNORECASE | re.DOTALL)
  svg = raw_svg
  if "<svg" in raw_svg:
    svg = raw_svg.replace("<svg", '<svg preserveAspectRatio="none"', 1)
  mini = f'<div class="barcode-mini">{svg}</div>'
  return f'<div class="barcode-wrap barcode-dual">{mini}{mini}<div class="sticker-zone" aria-hidden="true"></div></div>'


def _build_grid_test_cell() -> str:
  return (
    '<div class="grid-test">'
    '<div class="grid-mini"></div>'
    '<div class="grid-mini"></div>'
    '<div class="sticker-zone"></div>'
    '</div>'
  )


def _build_calibration_cell(run_label: str, marker_style: str, full_cross: dict | None = None) -> str:
  if isinstance(full_cross, dict):
    x_pct = max(5.0, min(95.0, float(full_cross.get("x_pct", 50.0))))
    y_pct = max(5.0, min(95.0, float(full_cross.get("y_pct", 50.0))))
    line_style = str(full_cross.get("line_style", "solid")).lower()
    if line_style not in {"solid", "dashed", "dotted"}:
      line_style = "solid"

    dasharray = ""
    linecap = "square"
    if line_style == "dashed":
      dasharray = ' stroke-dasharray="4 2"'
    elif line_style == "dotted":
      dasharray = ' stroke-dasharray="0.8 2.2"'
      linecap = "round"

    return (
      '<div class="calibration">'
      '<div class="full-cross">'
      f'<svg class="full-cross-svg" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
      f'<line x1="0" y1="{y_pct:.2f}" x2="100" y2="{y_pct:.2f}" stroke="#000" stroke-width="0.7" stroke-linecap="{linecap}"{dasharray}></line>'
      f'<line x1="{x_pct:.2f}" y1="0" x2="{x_pct:.2f}" y2="100" stroke="#000" stroke-width="0.7" stroke-linecap="{linecap}"{dasharray}></line>'
      '</svg>'
      '</div>'
      f'<div class="cal-rev">{html.escape(run_label)}</div>'
      '</div>'
    )

  marker_class = "tmark" if marker_style == "t" else "cross"
  return (
    '<div class="calibration">'
    f'<div class="micro {marker_class}" style="left:50%;top:50%;"></div>'
    f'<div class="cal-rev">{html.escape(run_label)}</div>'
    '</div>'
  )


def _calibration_seed_for_run(base_seed: int, rev: int, run_no: int, sequence: str) -> int:
  seq = (sequence or "same_seed_then_random").lower()
  base = int(base_seed) + (int(rev) * 100_003)
  if seq == "repeat_seed":
    return base
  if seq == "same_seed_then_random":
    if run_no <= 2:
      return base
    return base + (run_no * 7_919)
  return base + (run_no * 7_919)


def _outer_cross_indices(cols: int, rows: int, wanted: int, run_no: int) -> list[int]:
  rings = min(cols, rows) // 2
  if rings <= 0:
    return [0]
  ring = min((run_no - 1) % rings, rings - 1)
  left, right = ring, cols - 1 - ring
  top, bottom = ring, rows - 1 - ring
  edge = []
  for c in range(left, right + 1):
    edge.append(top * cols + c)
  for r in range(top + 1, bottom):
    edge.append(r * cols + right)
  if bottom != top:
    for c in range(right, left - 1, -1):
      edge.append(bottom * cols + c)
  if right != left:
    for r in range(bottom - 1, top, -1):
      edge.append(r * cols + left)
  if not edge:
    return [0]
  step = max(1, len(edge) // max(1, wanted))
  picked = edge[::step]
  return picked[:max(1, wanted)]


def _calibration_indices(settings: dict, cols: int, rows: int, run_no: int, rev: int) -> tuple[list[int], str]:
  capacity = cols * rows
  wanted = max(1, min(capacity, int(settings.get("calibration_mark_count", 12))))
  pattern = settings.get("calibration_pattern", "random_t")
  seed = _calibration_seed_for_run(
    int(settings.get("calibration_seed", 1337)),
    rev,
    run_no,
    settings.get("calibration_sequence", "same_seed_then_random"),
  )

  if pattern == "outer_cross":
    return _outer_cross_indices(cols, rows, wanted, run_no), "cross"

  rnd = random.Random(seed)
  if wanted >= capacity:
    return list(range(capacity)), "t"
  return rnd.sample(list(range(capacity)), wanted), "t"


def _build_calibration_cover_page(
  settings: dict,
  cols: int,
  rows: int,
  rev: int,
  run_count: int,
  seeds_by_run: list[int],
  marks_by_run: list[list[int]],
) -> str:
  pw = float(settings["page_width_in"])
  ph = float(settings["page_height_in"])
  ml = float(settings["margin_left_in"])
  mt = float(settings["margin_top_in"])
  cw = float(settings["cell_width_in"])
  ch = float(settings["cell_height_in"])
  gx = float(settings["gap_x_in"])
  gy = float(settings["gap_y_in"])

  seq = html.escape(str(settings.get("calibration_sequence", "same_seed_then_random")))
  label = html.escape(str(settings.get("calibration_run_label", "RUN")))
  count = int(settings.get("calibration_mark_count", 12))

  rows_html = []
  max_rows = 24
  for run_no in range(1, run_count + 1):
    seed = seeds_by_run[run_no - 1]
    marks = marks_by_run[run_no - 1]
    for idx in marks:
      if len(rows_html) >= max_rows:
        break
      r = idx // cols
      c = idx % cols
      x_left = ml + c * (cw + gx) + (cw / 2.0)
      y_top = mt + r * (ch + gy) + (ch / 2.0)
      x_right = pw - x_left
      y_bottom = ph - y_top
      rows_html.append(
        "<tr>"
        f"<td>{run_no}</td>"
        f"<td>{seed}</td>"
        f"<td>R{r}C{c}</td>"
        f"<td>{x_left:.4f}</td><td>{x_left * 25.4:.2f}</td>"
        f"<td>{y_top:.4f}</td><td>{y_top * 25.4:.2f}</td>"
        f"<td>{x_right:.4f}</td><td>{x_right * 25.4:.2f}</td>"
        f"<td>{y_bottom:.4f}</td><td>{y_bottom * 25.4:.2f}</td>"
        "</tr>"
      )
    if len(rows_html) >= max_rows:
      break

  table_rows = "".join(rows_html) if rows_html else "<tr><td colspan='11'>No marks configured</td></tr>"

  return f"""
  <div class=\"print-page cover-page\">
    <div class=\"cover\">
      <h1>Calibration Cover · rev {rev}</h1>
      <div class=\"meta\">Grid prediction: {cols} cols x {rows} rows | Marks/run: {count} | Run label: {label}</div>
      <div class=\"meta\">Sheet: {pw:.3f}in x {ph:.3f}in ({pw * 25.4:.2f} x {ph * 25.4:.2f} mm)</div>
      <div class=\"meta\">Cell: {cw:.3f}in x {ch:.3f}in ({cw * 25.4:.2f} x {ch * 25.4:.2f} mm) | Gap: {gx:.3f}in x {gy:.3f}in ({gx * 25.4:.2f} x {gy * 25.4:.2f} mm)</div>
      <div class=\"meta\">Margins (T,R,B,L): {settings['margin_top_in']}, {settings['margin_right_in']}, {settings['margin_bottom_in']}, {settings['margin_left_in']} in</div>
      <div class=\"meta\">Sequence: {seq} | Printer target: cross marks only, no borders</div>
      <hr />
      <div class=\"note\">Distances below are expected center positions from sheet edges (in and mm). Use ruler/caliper to compare print reality. (First {max_rows} marks shown to keep this cover to one page.)</div>
      <table>
        <thead>
          <tr>
            <th>Run</th><th>Seed</th><th>Cell</th>
            <th>Left in</th><th>Left mm</th>
            <th>Top in</th><th>Top mm</th>
            <th>Right in</th><th>Right mm</th>
            <th>Bottom in</th><th>Bottom mm</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </div>
  """


def _pick_full_cross_line_style(base_style: str, seed: int, idx: int) -> str:
  style = (base_style or "solid").lower()
  if style in {"solid", "dashed", "dotted"}:
    return style
  rnd = random.Random((seed * 1009) + (idx * 9176) + 17)
  return rnd.choice(["solid", "dashed", "dotted"])


def _full_cross_point_for_cell(seed: int, idx: int) -> tuple[float, float]:
  rnd = random.Random((seed * 37) + (idx * 7919) + 11)
  # Keep intersection away from the very edge so line style remains legible.
  x_pct = 14.0 + (rnd.random() * 72.0)
  y_pct = 14.0 + (rnd.random() * 72.0)
  return x_pct, y_pct


def _build_calibration_corner_overlay(style: str, settings: dict, cols: int, rows: int) -> str:
  style_name = "inverse_angle" if style == "inverse_angle" else "dot"
  marker_class = "corner-angle" if style_name == "inverse_angle" else "corner-dot"
  ml = float(settings["margin_left_in"])
  mt = float(settings["margin_top_in"])
  mr = float(settings["margin_right_in"])
  mb = float(settings["margin_bottom_in"])
  cw = float(settings["cell_width_in"])
  ch = float(settings["cell_height_in"])
  gx = float(settings["gap_x_in"])
  gy = float(settings["gap_y_in"])

  grid_w = (cols * cw) + (max(0, cols - 1) * gx)
  grid_h = (rows * ch) + (max(0, rows - 1) * gy)
  # Corner overlay coordinates are in page space; grid starts at configured margins.
  left = ml
  top = mt
  right = ml + grid_w
  bottom = mt + grid_h

  def _mark(corner_class: str, x_in: float, y_in: float, hlen_in: float, vlen_in: float) -> str:
    return (
      f'<div class="{marker_class} {corner_class}" '
      f'style="left:{x_in:.6f}in;top:{y_in:.6f}in;--hlen:{max(0.02, hlen_in):.6f}in;--vlen:{max(0.02, vlen_in):.6f}in;"></div>'
    )

  return (
    f'<div class="corner-debug {style_name}">'
    f'{_mark("tl", left, top, ml, mt)}'
    f'{_mark("tr", right, top, mr, mt)}'
    f'{_mark("bl", left, bottom, ml, mb)}'
    f'{_mark("br", right, bottom, mr, mb)}'
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


@router.get("/barcode-sheet/{sheet_code}")
async def get_barcode_sheet_tracking(sheet_code: str, db: AsyncSession = Depends(get_db)):
  code = (sheet_code or "").strip().lower()
  if not code:
    raise HTTPException(400, "sheet_code is required")

  job_row = (await db.execute(
    text(
      """
      SELECT id, sheet_code, profile_id, printed_at
      FROM barcode_print_jobs
      WHERE LOWER(sheet_code) = :code
      LIMIT 1
      """
    ),
    {"code": code},
  )).mappings().first()
  if not job_row:
    raise HTTPException(404, "Barcode sheet not found")

  items = (await db.execute(
    text(
      """
      SELECT component_id, barcode_id, sheet_row, sheet_col, cell_slot, box_label, box_row, box_col, created_at
      FROM barcode_print_items
      WHERE job_id = :job_id
      ORDER BY sheet_row, sheet_col, cell_slot
      """
    ),
    {"job_id": job_row["id"]},
  )).mappings().all()

  return {
    "sheet_code": job_row["sheet_code"],
    "profile_id": job_row["profile_id"],
    "printed_at": str(job_row["printed_at"]),
    "count": len(items),
    "items": [dict(x) for x in items],
  }


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
  mode: str = Query("front", pattern="^(front|barcode|calibration|grid_test)$"),
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
  take = max(1, min(limit, 2000))

  stmt = select(Component).order_by(Component.barcode_id)
  if barcode_ids:
    ids = [_normalize_barcode_for_print(x) for x in barcode_ids.split(",") if x.strip()]
    if ids:
      stmt = stmt.where(func.replace(func.upper(Component.barcode_id), '-', '').in_(ids))
  elif q and q.strip():
    like = f"%{q.strip()}%"
    stmt = stmt.where(
      (Component.barcode_id.ilike(like)) |
      (Component.name.ilike(like)) |
      (Component.value.ilike(like)) |
      (Component.package.ilike(like))
    )

  comps = (await db.execute(stmt.limit(take))).scalars().all()
  if mode in {"front", "barcode"}:
    slot_map = await _component_sort_slots(db)
    comps.sort(key=lambda c: slot_map.get(c.id, (9999, "~~~", 9999, 9999)))
  else:
    slot_map = {}

  if mode == "calibration":
    row.calibration_revision = int(row.calibration_revision or 0) + 1
    rev = row.calibration_revision
    run_count = int(settings.get("calibration_runs", 3))
  else:
    rev = int(row.calibration_revision or 0)
    run_count = 1

  page_blocks = []
  sheet_code = _new_sheet_code() if mode == "barcode" else ""
  printed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC") if mode == "barcode" else ""
  placements: list[dict] = []
  seeds_by_run: list[int] = []
  marks_by_run: list[list[int]] = []
  for run_no in range(1, run_count + 1):
    cells = []
    marked_set = set()
    marker_style = "cross"
    corner_overlay = ""
    if mode == "calibration":
      marked, _ = _calibration_indices(settings, cols, rows, run_no, rev)
      marked_set = set(marked)
      marks_by_run.append(sorted(marked_set))
      seeds_by_run.append(_calibration_seed_for_run(
        int(settings.get("calibration_seed", 1337)),
        rev,
        run_no,
        settings.get("calibration_sequence", "same_seed_then_random"),
      ))
      if settings.get("calibration_corner_marks"):
        corner_overlay = _build_calibration_corner_overlay(
          str(settings.get("calibration_corner_style", "dot")),
          settings,
          cols,
          rows,
        )

    for i in range(capacity):
      if mode == "calibration":
        if i in marked_set:
          run_label = f"{settings.get('calibration_run_label', 'RUN')} {run_no}"
          pattern_name = str(settings.get("calibration_pattern", "random_t"))
          if pattern_name == "random_full_cross":
            seed_for_run = seeds_by_run[run_no - 1]
            x_pct, y_pct = _full_cross_point_for_cell(seed_for_run, i)
            line_style = _pick_full_cross_line_style(str(settings.get("calibration_line_style", "solid")), seed_for_run, i)
            inner = _build_calibration_cell(
              run_label,
              marker_style,
              full_cross={"x_pct": x_pct, "y_pct": y_pct, "line_style": line_style},
            )
          else:
            inner = _build_calibration_cell(run_label, marker_style)
        else:
          inner = ""
        classes = "cell calibration-cell"
      else:
        if mode == "grid_test":
          inner = _build_grid_test_cell()
        elif i >= len(comps):
          inner = ""
        elif mode == "barcode":
          inner = _build_barcode_cell(comps[i], settings)
        else:
          inner = _build_front_cell(comps[i], settings)
        classes = "cell"

      cross = ""
      if settings["show_cut_grid"] and mode not in {"calibration", "grid_test"}:
        cross = '<div class="cut-x cut-a"></div><div class="cut-x cut-b"></div>'

      cells.append(f'<div class="{classes}">{cross}{inner}</div>')

    sheet = "".join(cells)
    header = ""
    margin_headers = ""
    if mode == "calibration":
      seed_show = seeds_by_run[run_no - 1]
      header = f'<div class="cal-page-head">CAL {html.escape(settings.get("calibration_run_label", "RUN"))} {run_no} · seed {seed_show} · rev {rev}</div>'
    elif mode == "barcode":
      margin_headers = _build_sheet_margin_headers(settings, cols, rows, sheet_code, printed_at)
    page_blocks.append(f'<div class="print-page">{header}{margin_headers}<div class="page">{corner_overlay}{sheet}</div></div>')

  if mode == "calibration":
    cover = _build_calibration_cover_page(settings, cols, rows, rev, run_count, seeds_by_run, marks_by_run)
    page_blocks.insert(0, cover)
  elif mode == "barcode":
    for i, comp in enumerate(comps[:capacity]):
      r = i // cols
      c = i % cols
      _, box_label, box_r, box_c = slot_map.get(comp.id, (None, None, None, None))
      bid = _normalize_barcode_for_print(comp.barcode_id)
      placements.append({
        "component_id": comp.id,
        "barcode_id": bid,
        "sheet_row": r,
        "sheet_col": c,
        "cell_slot": 1,
        "box_label": box_label,
        "box_row": box_r,
        "box_col": box_c,
      })
      placements.append({
        "component_id": comp.id,
        "barcode_id": bid,
        "sheet_row": r,
        "sheet_col": c,
        "cell_slot": 2,
        "box_label": box_label,
        "box_row": box_r,
        "box_col": box_c,
      })
    await _record_barcode_print_job(db, profile_id, sheet_code, placements)

  pages_html = "".join(page_blocks)
  radius_mm = settings["corner_radius_mm"]
  barcode_css_h = min(settings["barcode_max_height_in"], settings["cell_height_in"] * settings["barcode_height_ratio"])
  meta_top_in = max(0.02, float(settings["margin_top_in"]) * 0.26)
  meta_bottom_in = max(0.02, float(settings["margin_bottom_in"]) * 0.26)
  col_top_in = max(0.01, float(settings["margin_top_in"]) * 0.72)
  col_bottom_in = max(0.01, float(settings["margin_bottom_in"]) * 0.72)
  row_left_in = max(0.01, float(settings["margin_left_in"]) * 0.32)
  row_right_in = max(0.01, float(settings["margin_right_in"]) * 0.32)

  html_doc = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"UTF-8\">
  <title>Label Print</title>
  <style>
  @page {{ size: {settings['page_width_in']}in {settings['page_height_in']}in; margin: 0; }}
  html, body {{ margin: 0; padding: 0; background: #fff; color: #000; font-family: Arial, Helvetica, sans-serif; }}
  .print-page {{
    position: relative;
    width: {settings['page_width_in']}in;
    height: {settings['page_height_in']}in;
    page-break-after: always;
  }}
  .print-page:last-child {{ page-break-after: auto; }}
  .sheet-meta {{
    position: absolute;
    left: {settings['margin_left_in']}in;
    right: {settings['margin_right_in']}in;
    font-size: 6pt;
    font-family: 'Bahnschrift Condensed', 'Arial Narrow', Arial, sans-serif;
    font-weight: 700;
    text-align: center;
    letter-spacing: 0.2pt;
    color: #111;
    z-index: 30;
    pointer-events: none;
  }}
  .sheet-meta.top {{ top: {meta_top_in:.6f}in; }}
  .sheet-meta.bottom {{
    bottom: {meta_bottom_in:.6f}in;
    transform: rotate(180deg);
    transform-origin: center;
  }}
  .sheet-meta-rail {{ position: absolute; inset: 0; z-index: 30; pointer-events: none; }}
  .sheet-col, .sheet-row {{
    position: absolute;
    font-size: 5pt;
    font-family: 'Bahnschrift Condensed', 'Arial Narrow', Arial, sans-serif;
    color: #222;
    line-height: 1;
    user-select: none;
  }}
  .sheet-col.top {{ top: {col_top_in:.6f}in; transform: translateX(-50%); }}
  .sheet-col.bottom {{
    bottom: {col_bottom_in:.6f}in;
    transform: translateX(-50%) rotate(180deg);
  }}
  .sheet-row.left {{ left: {row_left_in:.6f}in; transform: translateY(-50%); }}
  .sheet-row.right {{ right: {row_right_in:.6f}in; transform: translateY(-50%) rotate(180deg); }}
  .sheet-corners-note {{
    position: absolute;
    font-size: 5pt;
    color: #333;
    font-family: monospace;
    z-index: 30;
    pointer-events: none;
  }}
  .sheet-corners-note.br {{ transform: rotate(180deg); }}
  .cover-page {{
    padding: 0.22in;
    box-sizing: border-box;
    break-inside: avoid;
    page-break-inside: avoid;
    overflow: hidden;
  }}
  .cover h1 {{ margin: 0 0 8px; font-size: 14pt; }}
  .cover .meta {{ font-size: 9pt; margin: 2px 0; }}
  .cover .note {{ font-size: 8.5pt; margin: 6px 0 8px; }}
  .cover table {{ width: 100%; border-collapse: collapse; font-size: 8pt; }}
  .cover th, .cover td {{ border: 0.5pt solid #555; padding: 2px 3px; text-align: left; }}
  .cover th {{ background: #f0f0f0; }}
  .cal-page-head {{
    font-size: 7pt;
    font-family: monospace;
    opacity: 0.95;
    margin: 0.06in {settings['margin_left_in']}in 0.02in;
  }}
  .page {{
    position: relative;
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
    transform-origin: top left;
    transform: translate({settings['comp_shift_x_mm']}mm, {settings['comp_shift_y_mm']}mm) skewX({settings['comp_skew_x_deg']}deg) skewY({settings['comp_skew_y_deg']}deg);
  }}
  .cell {{
    position: relative;
    border-radius: {radius_mm}mm;
    overflow: hidden;
    box-sizing: border-box;
  }}
  .calibration-cell {{ border-radius: 0; }}
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
    display: flex;
    align-items: stretch;
    justify-content: stretch;
    gap: 0;
    padding: 0.8mm;
    box-sizing: border-box;
    text-align: left;
  }}
  .front-dual {{ padding-right: 0.28in; }}
  .front-dual::before {{
    content: '';
    position: absolute;
    top: 0.75mm;
    bottom: 0.75mm;
    left: calc((100% - 0.28in) / 2);
    border-left: 0.4pt dotted #444;
    z-index: 2;
    pointer-events: none;
  }}
  .front-mini {{
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    gap: 0.3mm;
    padding: 0.4mm 0.5mm 0.5mm;
    box-sizing: border-box;
    position: relative;
    overflow: hidden;
  }}
  .front-img-wrap {{ height: 42%; width: 100%; display: flex; align-items: center; justify-content: center; }}
  .front-img {{ max-height: 100%; max-width: 95%; object-fit: contain; }}
  .front-name {{
    font-family: 'Bahnschrift SemiCondensed', 'Arial Narrow', 'Liberation Sans Narrow', Arial, sans-serif;
    font-weight: 800;
    font-size: {settings['name_font_pt']}pt;
    line-height: 1.03;
    width: 100%;
    min-height: 2.06em;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    white-space: normal;
    overflow-wrap: anywhere;
    word-break: break-word;
    hyphens: manual;
  }}
  .front-id {{
    font-family: 'Bahnschrift Condensed', 'Arial Narrow', 'Liberation Sans Narrow', Arial, sans-serif;
    font-weight: 700;
    font-size: {settings['id_font_pt']}pt;
    line-height: 1;
    margin-top: auto;
    text-align: left;
    width: 100%;
    padding-top: 0.35mm;
  }}
  .sticker-zone {{
    position: absolute;
    right: 0.06in;
    bottom: 0.05in;
    width: 0.25in;
    height: 0.25in;
    border-radius: 50%;
    pointer-events: none;
  }}
  .barcode-wrap {{
    position: absolute; inset: 0;
    display: flex;
    align-items: stretch;
    justify-content: stretch;
    gap: 0;
    padding: 0.8mm;
    box-sizing: border-box;
  }}
  .barcode-dual {{ padding-right: 0.28in; }}
  .barcode-dual::before {{
    content: '';
    position: absolute;
    top: 0.75mm;
    bottom: 0.75mm;
    left: calc((100% - 0.28in) / 2);
    border-left: 0.4pt dotted #444;
    z-index: 2;
    pointer-events: none;
  }}
  .barcode-mini {{
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }}
  .barcode-wrap svg {{ width: 100%; height: min(100%, {barcode_css_h}in); display: block; }}
  .barcode-wrap text {{ font-size: 5pt !important; }}
  .grid-test {{ position: absolute; inset: 0; padding: 0.8mm; box-sizing: border-box; display: flex; gap: 0; }}
  .grid-mini {{ flex: 1 1 0; border: 0.3pt solid #444; border-radius: 0.35mm; }}
  .grid-test::before {{
    content: '';
    position: absolute;
    top: 0.75mm;
    bottom: 0.75mm;
    left: calc((100% - 0.28in) / 2);
    border-left: 0.4pt dotted #444;
    z-index: 2;
    pointer-events: none;
  }}
  .calibration {{ position: absolute; inset: 0; }}
  .micro {{ position: absolute; width: 2.2mm; height: 2.2mm; opacity: 1; }}
  .micro.dot {{ border-radius: 50%; background: #000; }}
  .micro.cross::before, .micro.cross::after {{
    content: ''; position: absolute; left: 50%; top: 50%; background: #000; transform: translate(-50%, -50%);
  }}
  .micro.cross::before {{ width: 2.2mm; height: 0.5mm; }}
  .micro.cross::after {{ width: 0.5mm; height: 2.2mm; }}
  .micro.tmark::before, .micro.tmark::after {{
    content: ''; position: absolute; left: 50%; top: 50%; background: #000; transform: translate(-50%, -50%);
  }}
  .micro.tmark::before {{ width: 2.5mm; height: 0.5mm; top: 30%; }}
  .micro.tmark::after {{ width: 0.5mm; height: 2.5mm; top: 60%; }}
  .cal-rev {{
    position: absolute; right: 0.7mm; bottom: 0.4mm;
    font-size: 5pt; opacity: 0.9; font-family: monospace;
  }}
  .full-cross {{ position: absolute; inset: 0; }}
  .full-cross-svg {{ position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; shape-rendering: geometricPrecision; }}
  .corner-debug {{
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 20;
  }}
  .corner-dot {{
    position: absolute;
    width: 2.6mm;
    height: 2.6mm;
    border-radius: 50%;
    background: #000;
  }}
  .corner-dot.tl {{ transform: translate(0, 0); }}
  .corner-dot.tr {{ transform: translate(-100%, 0); }}
  .corner-dot.bl {{ transform: translate(0, -100%); }}
  .corner-dot.br {{ transform: translate(-100%, -100%); }}
  .corner-angle {{
    position: absolute;
    width: 0;
    height: 0;
  }}
  .corner-angle.tl {{ transform: translate(0, 0); }}
  .corner-angle.tr {{ transform: translate(-100%, 0); }}
  .corner-angle.bl {{ transform: translate(0, -100%); }}
  .corner-angle.br {{ transform: translate(-100%, -100%); }}
  .corner-angle::before,
  .corner-angle::after {{
    content: '';
    position: absolute;
    background: #000;
  }}
  .corner-angle.tl::before {{ right: 0; top: 0; width: var(--hlen); height: 0.5mm; }}
  .corner-angle.tl::after {{ left: 0; bottom: 0; width: 0.5mm; height: var(--vlen); }}
  .corner-angle.tr::before {{ left: 0; top: 0; width: var(--hlen); height: 0.5mm; }}
  .corner-angle.tr::after {{ right: 0; bottom: 0; width: 0.5mm; height: var(--vlen); }}
  .corner-angle.bl::before {{ right: 0; bottom: 0; width: var(--hlen); height: 0.5mm; }}
  .corner-angle.bl::after {{ left: 0; top: 0; width: 0.5mm; height: var(--vlen); }}
  .corner-angle.br::before {{ left: 0; bottom: 0; width: var(--hlen); height: 0.5mm; }}
  .corner-angle.br::after {{ right: 0; top: 0; width: 0.5mm; height: var(--vlen); }}
  </style>
</head>
<body onload=\"window.print()\">
  {pages_html}
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

    barcode_svg = generate_code128_svg(_normalize_barcode_for_print(barcode_id))

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
    <div class="id">{_normalize_barcode_for_print(barcode_id)} · {comp.package or ''}</div>
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

    barcode_svg = generate_code128_svg(_normalize_barcode_for_print(barcode_id))

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
    <div class="id">{_normalize_barcode_for_print(barcode_id)}</div>
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
