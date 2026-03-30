"""Auto-generate compact label titles for small-bin printed labels."""

import re

MAX_LABEL_TITLE_LEN = 18


def _type_prefix(type_path: str | None, unit: str | None, name: str | None) -> str:
    t = (type_path or "").lower()
    n = (name or "").lower()

    if "resistor" in t or (unit == "Ω") or "resistor" in n:
        return "RES"
    if "capacitor" in t or unit == "F" or "capacitor" in n:
        return "CAP"
    if "inductor" in t or unit == "H" or "inductor" in n:
        return "IND"
    if "diode" in t:
        return "DIO"
    if "transistor" in t or "mosfet" in t:
        return "TR"
    if "microcontroller" in t:
        return "MCU"
    if "/ic" in t or t.startswith("actives/ic"):
        return "IC"
    if "module" in t or "development-board" in t:
        return "MOD"
    if "sensor" in t:
        return "SNS"
    if "connector" in t:
        return "CON"
    if "relay" in t:
        return "RLY"
    if "switch" in t:
        return "SWT"
    return "CMP"


def _format_value(value: str | None, unit: str | None, type_path: str | None, name: str | None) -> str:
    if not value:
        return ""

    v = re.sub(r"\s+", "", str(value))
    t = (type_path or "").lower()
    n = (name or "").lower()

    is_res = "resistor" in t or unit == "Ω" or "resistor" in n
    if is_res and "Ω" not in v:
        if re.search(r"\d", v):
            v = f"{v}Ω"

    if unit and unit not in v and unit != "Ω":
        v = f"{v}{unit}"

    return v


def _name_fallback(name: str | None) -> str:
    if not name:
        return "COMP"
    words = re.findall(r"[A-Za-z0-9+.-]+", name.upper())
    if not words:
        return "COMP"

    stop = {"THE", "AND", "WITH", "BOARD", "MODULE", "DEVELOPMENT", "KIT"}
    keep = [w for w in words if w not in stop]
    if not keep:
        keep = words
    return " ".join(keep[:2])


def generate_short_title(
    *,
    name: str | None,
    value: str | None,
    unit: str | None,
    package: str | None,
    type_path: str | None,
) -> str:
    """Generate compact human-readable label text suitable for tiny lid labels."""
    prefix = _type_prefix(type_path, unit, name)

    material = ""
    n = (name or "").lower()
    if prefix == "RES" and "carbon" in n:
        material = "CF"
    elif prefix == "RES" and "metal film" in n:
        material = "MF"

    val = _format_value(value, unit, type_path, name)
    pkg = (package or "").strip().upper()
    if len(pkg) > 8:
        pkg = ""

    parts = [p for p in [material, prefix, val] if p]
    title = " ".join(parts).strip()

    if pkg and title and len(title) + 1 + len(pkg) <= MAX_LABEL_TITLE_LEN:
        title = f"{title} {pkg}"

    if not title:
        title = _name_fallback(name)

    if len(title) > MAX_LABEL_TITLE_LEN:
        # Prefer preserving prefix and value when present.
        if val:
            compact = " ".join([p for p in [material, prefix, val] if p])
            title = compact[:MAX_LABEL_TITLE_LEN]
        else:
            title = title[:MAX_LABEL_TITLE_LEN]

    return title.strip() or "COMP"
