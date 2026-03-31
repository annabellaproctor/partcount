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
    "beige": "#d9c7a3",
}

DEFAULT_BG = COLOR_MAP["beige"]


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

        marker = ""
        raw = token
        if token[0] in "+-_":
            marker = token[0]
            raw = token[1:].strip()
        raw_low = raw.lower()
        color_hex = COLOR_MAP.get(raw_low)

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

    container_w = int(width * 0.84)
    container_h = int(container_w * (9.0 / 16.0))
    container_x = int((width - container_w) / 2)
    container_y = int((height - container_h) / 2)
    radius = int(container_h * 0.38)  # Slightly less rounded than full capsule.
    clip_id = "markings_clip"
    gap = max(2, int(container_w * 0.0065))

    shapes: list[str] = [
        f'<defs><clipPath id="{clip_id}"><rect x="{container_x}" y="{container_y}" width="{container_w}" height="{container_h}" rx="{radius}" ry="{radius}"/></clipPath></defs>',
        f'<rect x="{container_x}" y="{container_y}" width="{container_w}" height="{container_h}" rx="{radius}" ry="{radius}" fill="{layout["background"]}"/>',
    ]

    left = layout["left_stripes"][:2]
    right = layout["right_stripes"][:2]
    middle = layout["middle_bands"]

    stripe_w = int(container_w * 0.09)
    inner_pad = max(8, int(container_h * 0.11))
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
        f'<rect x="{container_x}" y="{container_y}" width="{container_w}" height="{container_h}" '
        f'rx="{radius}" ry="{radius}" fill="none" stroke="#d1d5db" stroke-width="2"/>'
    )

    if overlay_text:
        text_x = container_x + (container_w / 2)
        text_y = container_y + (container_h / 2) + 5
        max_font = int(container_h * 0.56)
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
