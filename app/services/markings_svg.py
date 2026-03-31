import html
import re
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


def _contrast_text_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "#111827"
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    # Relative luminance approximation.
    y = (0.299 * r) + (0.587 * g) + (0.114 * b)
    return "#111827" if y > 160 else "#f9fafb"


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
                "label": token.capitalize(),
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
    width: int = 720,
    height: int = 96,
    gap: int = 8,
    show_labels: bool = True,
) -> str:
    if width < 120:
        width = 120
    if height < 40:
        height = 40
    if gap < 0:
        gap = 0

    count = len(entries)
    if count == 0:
        entries = [{"kind": "text", "text": "(empty)", "token": "(empty)"}]
        count = 1

    inner_x = 8
    inner_y = 8
    inner_w = width - (inner_x * 2)
    inner_h = height - (inner_y * 2)
    slot_w = max(12, int((inner_w - (gap * (count - 1))) / count))

    shapes: list[str] = [
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="10" fill="#f8fafc"/>',
        f'<rect x="4" y="4" width="{width - 8}" height="{height - 8}" rx="8" fill="#ffffff" stroke="#d1d5db"/>'
    ]

    x = inner_x
    for entry in entries:
        kind = entry.get("kind")
        if kind == "color":
            fill = str(entry.get("hex") or "#9ca3af")
            text_color = _contrast_text_color(fill)
            label = html.escape(str(entry.get("label") or entry.get("token") or ""))
            shapes.append(
                f'<rect x="{x}" y="{inner_y}" width="{slot_w}" height="{inner_h}" rx="4" fill="{fill}" stroke="#374151"/>'
            )
            if show_labels:
                shapes.append(
                    f'<text x="{x + (slot_w / 2)}" y="{inner_y + (inner_h / 2) + 4}" '
                    f'font-size="12" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" '
                    f'fill="{text_color}" text-anchor="middle">{label}</text>'
                )
        else:
            text = html.escape(str(entry.get("text") or entry.get("token") or ""))
            shapes.append(
                f'<rect x="{x}" y="{inner_y}" width="{slot_w}" height="{inner_h}" rx="4" fill="#eef2ff" stroke="#9ca3af"/>'
            )
            shapes.append(
                f'<text x="{x + (slot_w / 2)}" y="{inner_y + (inner_h / 2) + 4}" '
                f'font-size="13" font-weight="700" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" '
                f'fill="#1f2937" text-anchor="middle">{text}</text>'
            )
        x += slot_w + gap

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Component markings">'
        + "".join(shapes)
        + "</svg>"
    )


def build_markings_svg(text: str | None = None, tokens: list[str] | None = None, width: int = 720, height: int = 96) -> dict[str, Any]:
    entries = parse_marking_tokens(text=text, tokens=tokens)
    svg = render_markings_svg(entries, width=width, height=height)
    return {
        "entries": entries,
        "svg": svg,
    }
