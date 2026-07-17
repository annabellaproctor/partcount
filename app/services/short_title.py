"""Auto-generate compact label titles for small-bin printed labels."""

import re

MAX_LABEL_TITLE_LEN = 18


# Most components carry no type_path, so the name has to carry the type.
# Matched as a leading token ("RES-1/4w-5%-1MΩ") or a whole word ("10k Resistor"),
# never as a bare substring — "RES" inside "PRESSURE" is not a resistor.
_NAME_TYPE_HINTS = (
    ("RES", ("res", "resistor", "resistors")),
    ("CAP", ("cap", "capacitor", "capacitors")),
    ("IND", ("ind", "inductor", "inductors", "choke")),
    ("DIO", ("dio", "diode", "diodes", "led", "zener", "schottky")),
    ("TR", ("tr", "transistor", "mosfet", "bjt", "igbt")),
    ("MCU", ("mcu", "microcontroller")),
    ("MOD", ("mod", "module", "modules", "devkit", "breakout")),
    ("SNS", ("sns", "sensor", "sensors")),
    ("CON", ("con", "conn", "connector", "connectors", "header")),
    ("RLY", ("rly", "relay", "relays")),
    ("SWT", ("swt", "switch", "switches", "button")),
    ("XTAL", ("xtal", "crystal", "oscillator", "resonator")),
    ("FUS", ("fus", "fuse", "fuses", "ptc")),
)


def _prefix_from_name(name: str | None) -> str | None:
    if not name:
        return None
    tokens = [w for w in re.split(r"[^A-Za-z]+", name.lower()) if w]
    if not tokens:
        return None
    for prefix, hints in _NAME_TYPE_HINTS:
        # Leading token wins: "RES-1/4w" is a resistor, "LED Resistor Kit" is not.
        if tokens[0] in hints:
            return prefix
    for prefix, hints in _NAME_TYPE_HINTS:
        # Whole-word match anywhere, but only for unambiguous full words.
        if any(h in tokens for h in hints if len(h) > 3):
            return prefix
    return None


def _type_prefix(type_path: str | None, unit: str | None, name: str | None) -> str:
    t = (type_path or "").lower()
    n = (name or "").lower()

    if "resistor" in t or "resist" in t or (unit == "Ω") or "resistor" in n:
        return "RES"
    if "capacitor" in t or "capacit" in t or unit == "F" or "capacitor" in n:
        return "CAP"
    if "inductor" in t or "induct" in t or unit == "H" or "inductor" in n:
        return "IND"
    if "diode" in t:
        return "DIO"
    if "transistor" in t or "mosfet" in t:
        return "TR"
    if "microcontroller" in t:
        return "MCU"
    if "/ic" in t or t.startswith("actives/ic"):
        return "IC"
    if "module" in t or "modules" in t or "development-board" in t:
        return "MOD"
    if "sensor" in t:
        return "SNS"
    if "connector" in t:
        return "CON"
    if "relay" in t:
        return "RLY"
    if "switch" in t:
        return "SWT"

    # type_path is missing on most components — fall back to the name before
    # giving up and calling it a generic CMP.
    from_name = _prefix_from_name(name)
    if from_name:
        return from_name

    return "CMP"


def _format_value(value: str | None, unit: str | None, type_path: str | None, name: str | None) -> str:
    if not value:
        return ""

    v = re.sub(r"\s+", "", str(value))
    t = (type_path or "").lower()
    n = (name or "").lower()

    is_res = "resistor" in t or "resist" in t or unit == "Ω" or "resistor" in n
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
