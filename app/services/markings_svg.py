import html
import re
import base64
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


def render_markings_svg(
    entries: list[dict[str, Any]],
    width: int = 512,
    height: int = 512,
) -> str:
    # Output is always square with transparent background, centered pill.
    side = max(128, min(width, height))
    width = side
    height = side

    colors = [e for e in entries if e.get("kind") == "color"]
    texts = [str(e.get("text") or e.get("token") or "").strip() for e in entries if e.get("kind") == "text"]
    overlay_text = html.escape(" ".join([t for t in texts if t]))

    container_w = int(width * 0.84)
    container_h = int(container_w * 0.75)  # 3 high by 4 wide ratio.
    container_x = int((width - container_w) / 2)
    container_y = int((height - container_h) / 2)
    radius = int(container_h / 2)
    clip_id = "markings_clip"

    shapes: list[str] = [
        f'<defs><clipPath id="{clip_id}"><rect x="{container_x}" y="{container_y}" width="{container_w}" height="{container_h}" rx="{radius}" ry="{radius}"/></clipPath></defs>',
    ]

    color_count = len(colors)
    if color_count == 0:
        shapes.append(
            f'<rect x="{container_x}" y="{container_y}" width="{container_w}" height="{container_h}" rx="{radius}" ry="{radius}" fill="#374151"/>'
        )
    elif color_count <= 2:
        base = colors[0].get("hex") or "#6b7280"
        shapes.append(
            f'<rect x="{container_x}" y="{container_y}" width="{container_w}" height="{container_h}" rx="{radius}" ry="{radius}" fill="{base}"/>'
        )
        if color_count == 2:
            stripe = colors[1].get("hex") or "#e5e7eb"
            stripe_x = container_x + int(container_w * 0.74)
            stripe_w = max(10, int(container_w * 0.16))
            shapes.append(
                f'<rect x="{stripe_x}" y="{container_y + 6}" width="{stripe_w}" height="{container_h - 12}" '
                f'rx="6" ry="6" fill="{stripe}"/>'
            )
    else:
        # 3+ colors: render touching bands clipped to outer pill so only outer edges are rounded.
        band_count = min(color_count, 8)
        band_w = container_w / band_count
        for i in range(band_count):
            fill = colors[i].get("hex") or "#9ca3af"
            x = container_x + (i * band_w)
            shapes.append(
                f'<rect x="{x:.2f}" y="{container_y}" width="{band_w + 0.5:.2f}" height="{container_h}" '
                f'fill="{fill}" clip-path="url(#{clip_id})"/>'
            )

    shapes.append(
        f'<rect x="{container_x}" y="{container_y}" width="{container_w}" height="{container_h}" '
        f'rx="{radius}" ry="{radius}" fill="none" stroke="#d1d5db" stroke-width="2"/>'
    )

    if overlay_text:
        text_x = container_x + (container_w / 2)
        text_y = container_y + (container_h / 2) + 5
        max_font = int(container_h * 0.50)
        min_font = 18
        text_len = max(1, len(overlay_text))
        target_w = container_w * 0.82
        # Approximate monospace/sans average width factor for adaptive size.
        est_from_width = int(target_w / (0.58 * text_len))
        font_size = max(min_font, min(max_font, est_from_width))
        shapes.append(
            f'<text x="{text_x:.2f}" y="{text_y:.2f}" text-anchor="middle" '
            f'font-size="{font_size}" font-weight="800" letter-spacing="0.3" '
            f'font-family="ui-sans-serif, -apple-system, Segoe UI, Helvetica, Arial" '
            f'fill="#ffffff" stroke="#000000" stroke-width="2" paint-order="stroke fill">{overlay_text}</text>'
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
