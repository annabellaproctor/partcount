import html
import re
import base64
import math
from typing import Any


COLOR_MAP = {
    "black": "#1a1a1a",
    "brown": "#6f4e37",
    "red": "#d63b3b",
    "orange": "#f08c00",
    "yellow": "#f5c542",
    "green": "#2f9e44",
    "blue": "#3b82f6",
    "violet": "#7c3aed",
    "purple": "#7c3aed",
    "gray": "#6b7280",
    "grey": "#6b7280",
    "white": "#f3f4f6",
    "gold": "#c9a227",
    "silver": "#b0b7c3",
    "pink": "#ec4899",
    "beige": "#d9c7a3",
}

DEFAULT_BG = COLOR_MAP["beige"]
SYMBOL_ALIASES = {
    "screw": "screw",
    "screwhead": "screw",
    "phillips": "screw",
    "philips": "screw",
    "torx": "screw",
    "drill": "drill",
    "drillbit": "drill",
    "bit": "drill",
}


def parse_marking_tokens(text: str | None = None, tokens: list[str] | None = None) -> list[dict[str, Any]]:
    items: list[str] = []
    if tokens:
        items.extend([str(t).strip() for t in tokens if str(t).strip()])

    if text:
        normalized = text.replace(",", " ")
        raw = [x.strip() for x in re.split(r"\s+", normalized) if x.strip()]
        for token in raw:
            if token.lower() in {"and", "&", "/"}:
                continue
            items.append(token)

    parsed: list[dict[str, Any]] = []
    for token in items:
        low = token.lower()
        color = COLOR_MAP.get(low)
        if color:
            parsed.append({
                "kind": "color",
                "token": token,
                "name": low,
                "hex": color,
            })
        else:
            parsed.append({
                "kind": "text",
                "token": token,
                "text": token,
            })
    return parsed


def parse_marking_layout(entries: list[dict[str, Any]]) -> dict[str, Any]:
    layout = {
        "background": DEFAULT_BG,
        "left_stripes": [],
        "right_stripes": [],
        "middle_bands": [],
        "text": "",
        "shape": None,
        "symbols": [],
    }

    text_parts: list[str] = []
    for e in entries:
        token = str(e.get("token") or "").strip()
        if not token:
            continue

        low = token.lower()
        # Transparent spacer band in the middle track.
        if low == "_":
            layout["middle_bands"].append(None)
            continue

        # Dot-shape grammar: .8 .3 .0 .4 .R .5*
        if low.startswith("."):
            if low in {".r", ".rx"}:
                layout["shape"] = {"type": "rectangle", "axis": "x"}
                continue
            if low == ".ry":
                layout["shape"] = {"type": "rectangle", "axis": "y"}
                continue
            m = re.match(r"^\.(\d+)(\*+)?$", low)
            if m:
                points = int(m.group(1))
                is_star = bool(m.group(2))
                if is_star:
                    layout["shape"] = {"type": "star", "points": max(3, points)}
                elif points == 0:
                    layout["shape"] = {"type": "circle"}
                elif points == 4:
                    layout["shape"] = {"type": "square"}
                else:
                    layout["shape"] = {"type": "polygon", "points": max(3, points)}
                continue

        marker = ""
        raw = token
        if token[0] in "+-_":
            marker = token[0]
            raw = token[1:].strip()
        raw_low = raw.lower()
        color_hex = COLOR_MAP.get(raw_low)

        symbol = SYMBOL_ALIASES.get(raw_low)
        if symbol:
            if len(layout["symbols"]) < 4:
                layout["symbols"].append(symbol)
            continue

        if marker == "_" and color_hex:
            # _beige or _silver sets base body color.
            layout["background"] = color_hex
            continue

        if color_hex:
            if marker == "-":
                if len(layout["left_stripes"]) < 2:
                    layout["left_stripes"].append(color_hex)
            elif marker == "+":
                if len(layout["right_stripes"]) < 2:
                    layout["right_stripes"].append(color_hex)
            else:
                # Default/unsigned colors are centered bands.
                layout["middle_bands"].append(color_hex)
            continue

        # Unknown tokens are text overlays.
        text_parts.append(token)

    layout["text"] = " ".join(text_parts).strip()
    return layout


def _regular_polygon_points(cx: float, cy: float, r: float, n: int, start_deg: float = -90.0) -> str:
    pts = []
    for i in range(n):
        a = math.radians(start_deg + (360.0 * i / n))
        pts.append(f"{cx + r * math.cos(a):.2f},{cy + r * math.sin(a):.2f}")
    return " ".join(pts)


def _star_points(cx: float, cy: float, r_outer: float, r_inner: float, n: int, start_deg: float = -90.0) -> str:
    pts = []
    total = n * 2
    for i in range(total):
        a = math.radians(start_deg + (360.0 * i / total))
        r = r_outer if i % 2 == 0 else r_inner
        pts.append(f"{cx + r * math.cos(a):.2f},{cy + r * math.sin(a):.2f}")
    return " ".join(pts)


def _shape_element(shape: dict | None, x: float, y: float, w: float, h: float, default_rx: float, fill: str = "none", stroke: str = "none", stroke_width: float = 0) -> str:
    style = f'fill="{fill}" stroke="{stroke}"'
    if stroke_width > 0:
        style += f' stroke-width="{stroke_width}"'

    if not shape:
        return f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" rx="{default_rx:.2f}" ry="{default_rx:.2f}" {style}/>'

    st = shape.get("type")
    cx = x + (w / 2.0)
    cy = y + (h / 2.0)

    if st == "circle":
        r = min(w, h) / 2.0
        return f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{r:.2f}" ry="{r:.2f}" {style}/>'

    if st == "square":
        s = min(w, h)
        sx = cx - (s / 2.0)
        sy = cy - (s / 2.0)
        return f'<rect x="{sx:.2f}" y="{sy:.2f}" width="{s:.2f}" height="{s:.2f}" {style}/>'

    if st == "rectangle":
        axis = str(shape.get("axis") or "x").lower()
        if axis == "y":
            rw = w * 0.58
            rh = h * 0.92
        else:
            # Default rectangle is longways (sideways/horizontal).
            rw = w * 0.92
            rh = h * 0.58
        rx = cx - (rw / 2.0)
        ry = cy - (rh / 2.0)
        return f'<rect x="{rx:.2f}" y="{ry:.2f}" width="{rw:.2f}" height="{rh:.2f}" {style}/>'

    if st == "polygon":
        n = int(shape.get("points") or 6)
        r = (min(w, h) / 2.0)
        pts = _regular_polygon_points(cx, cy, r, max(3, n))
        return f'<polygon points="{pts}" {style}/>'

    if st == "star":
        n = int(shape.get("points") or 5)
        r_outer = (min(w, h) / 2.0)
        r_inner = r_outer * 0.48
        pts = _star_points(cx, cy, r_outer, r_inner, max(3, n))
        return f'<polygon points="{pts}" {style}/>'

    return f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" rx="{default_rx:.2f}" ry="{default_rx:.2f}" {style}/>'


def _render_symbol(symbol: str, x: float, y: float, size: float) -> str:
    if symbol == "screw":
        cx = x + (size / 2.0)
        cy = y + (size / 2.0)
        r = size * 0.42
        return (
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="#ffffff" stroke="#000000" stroke-width="1.6"/>'
            f'<line x1="{cx - r * 0.55:.2f}" y1="{cy:.2f}" x2="{cx + r * 0.55:.2f}" y2="{cy:.2f}" stroke="#000000" stroke-width="1.6"/>'
            f'<line x1="{cx:.2f}" y1="{cy - r * 0.55:.2f}" x2="{cx:.2f}" y2="{cy + r * 0.55:.2f}" stroke="#000000" stroke-width="1.6"/>'
        )

    if symbol == "drill":
        x0 = x + size * 0.10
        y0 = y + size * 0.12
        x1 = x + size * 0.78
        y1 = y + size * 0.50
        x2 = x + size * 0.26
        y2 = y + size * 0.90
        return (
            f'<path d="M{x0:.2f},{y0:.2f} L{x1:.2f},{y1:.2f} L{x2:.2f},{y2:.2f} Z" fill="#ffffff" stroke="#000000" stroke-width="1.4"/>'
            f'<line x1="{x0 + size * 0.12:.2f}" y1="{y0 + size * 0.12:.2f}" x2="{x2 - size * 0.03:.2f}" y2="{y2 - size * 0.02:.2f}" stroke="#000000" stroke-width="1.2"/>'
        )

    return ""


def render_markings_svg(
    entries: list[dict[str, Any]],
    width: int = 512,
    height: int = 512,
) -> str:
    # Output is always square with transparent background, centered pill.
    side = max(128, min(width, height))
    width = side
    height = side

    layout = parse_marking_layout(entries)
    overlay_text = html.escape(layout["text"])

    middle_slots = len(layout["middle_bands"])
    if middle_slots <= 0:
        middle_slots = len(layout["left_stripes"]) + len(layout["right_stripes"])

    shape_mode = bool(layout.get("shape"))

    # Default schema by band amount if no dot-shape token.
    compact_mode = 1 <= middle_slots <= 3
    if shape_mode:
        # Explicit shape mode uses the largest centered square region.
        side_inner = int(width * 0.90)
        container_w = side_inner
        container_h = side_inner
        radius = int(side_inner * 0.10)
        gap = max(2, int(side_inner * 0.010))
    elif compact_mode:
        container_h = int(height * 0.78)
        container_w = int(container_h * (3.0 / 4.0))
        container_w = min(container_w, int(width * 0.82))
        radius = int(container_h * 0.12)
        gap = max(1, int(container_w * 0.0035))
    else:
        container_w = int(width * 0.84)
        container_h = int(container_w * (9.0 / 16.0))
        radius = int(container_h * 0.28)
        gap = max(4, int(container_w * 0.013))

    container_x = int((width - container_w) / 2)
    container_y = int((height - container_h) / 2)
    clip_id = "markings_clip"

    clip_shape = _shape_element(layout.get("shape"), container_x, container_y, container_w, container_h, radius)
    shapes: list[str] = [
        f'<defs><clipPath id="{clip_id}">{clip_shape}</clipPath></defs>',
        _shape_element(layout.get("shape"), container_x, container_y, container_w, container_h, radius, fill=layout["background"]),
    ]

    left = layout["left_stripes"][:2]
    right = layout["right_stripes"][:2]
    middle = layout["middle_bands"]

    stripe_w = int(container_w * (0.11 if compact_mode else 0.085))
    inner_pad = max(4, int(container_h * (0.08 if compact_mode else 0.12)))
    stripe_y = container_y + inner_pad
    stripe_h = container_h - (inner_pad * 2)

    cursor_left = container_x + gap
    for c in left:
        shapes.append(
            f'<rect x="{cursor_left}" y="{stripe_y}" width="{stripe_w}" height="{stripe_h}" '
            f'rx="6" ry="6" fill="{c}" clip-path="url(#{clip_id})"/>'
        )
        cursor_left += stripe_w + gap

    cursor_right = container_x + container_w - gap
    for c in right:
        cursor_right -= stripe_w
        shapes.append(
            f'<rect x="{cursor_right}" y="{stripe_y}" width="{stripe_w}" height="{stripe_h}" '
            f'rx="6" ry="6" fill="{c}" clip-path="url(#{clip_id})"/>'
        )
        cursor_right -= gap

    middle_x = cursor_left
    middle_w = max(0, cursor_right - middle_x)
    band_count = len(middle)
    if band_count > 0 and middle_w > 8:
        total_gaps = gap * (band_count - 1)
        band_w = max(6, (middle_w - total_gaps) / band_count)
        x = middle_x
        for i in range(band_count):
            color = middle[i]
            if color:
                shapes.append(
                    f'<rect x="{x:.2f}" y="{container_y + gap}" width="{band_w:.2f}" height="{container_h - (2 * gap)}" '
                    f'fill="{color}" clip-path="url(#{clip_id})"/>'
                )
            x += band_w + gap

    shapes.append(
        _shape_element(layout.get("shape"), container_x, container_y, container_w, container_h, radius, fill="none", stroke="#d1d5db", stroke_width=2)
    )

    # Optional icon symbols (screw/drill) near top-right.
    if layout.get("symbols"):
        icon = max(14, int(container_h * 0.17))
        sx = container_x + container_w - icon - 8
        sy = container_y + 7
        for i, sym in enumerate(layout["symbols"][:3]):
            shapes.append(_render_symbol(sym, sx - (i * (icon + 4)), sy, icon))

    if overlay_text:
        text_x = container_x + (container_w / 2)
        text_y = container_y + (container_h / 2) + 5
        max_font = int(container_h * 0.60)
        min_font = 16
        text_len = max(1, len(overlay_text))
        target_w = container_w * 0.78
        # Approximate monospace/sans average width factor for adaptive size.
        est_from_width = int(target_w / (0.58 * text_len))
        font_size = max(min_font, min(max_font, est_from_width))
        shapes.append(
            f'<text x="{text_x:.2f}" y="{text_y:.2f}" text-anchor="middle" '
            f'font-size="{font_size}" font-weight="800" letter-spacing="0.3" '
            f'font-family="ui-sans-serif, -apple-system, Segoe UI, Helvetica, Arial" '
            f'fill="#ffffff" stroke="#000000" stroke-width="2.6" paint-order="stroke fill">{overlay_text}</text>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Component markings">'
        + "".join(shapes)
        + "</svg>"
    )


def build_markings_svg(text: str | None = None, tokens: list[str] | None = None, width: int = 512, height: int = 512) -> dict[str, Any]:
    entries = parse_marking_tokens(text=text, tokens=tokens)
    svg = render_markings_svg(entries, width=width, height=height)
    data_url = "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {
        "entries": entries,
        "svg": svg,
        "image_data_url": data_url,
    }
