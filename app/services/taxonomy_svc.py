"""
Taxonomy service: field inheritance and label auto-generation.

collect_inherited_schema(path, all_nodes)
    Walk from root → leaf along a taxonomy path, merging spec_schema dicts at each
    level.  Lower nodes override upper nodes for the same key (more specific wins).
    Returns: dict of field_name → field_descriptor (type, unit, required, values…)

render_label(label_format, specs, inherited_schema)
    Fill a label_format template like "{resistance}-CFR-{power_rating}-{tolerance}"
    with values from the specs dict, formatting numbers with engineering units.
    Missing values render as "?".
    Returns: formatted string, or None if label_format is empty.
"""

import re
import math
from typing import Optional


# ---------------------------------------------------------------------------
# Field inheritance
# ---------------------------------------------------------------------------

def collect_inherited_schema(
    path: str,
    all_nodes: list[dict],
) -> dict:
    """
    Walk the taxonomy path root → leaf, accumulating spec_schema.
    Each level's fields are merged in; deeper nodes win on key conflicts.

    all_nodes: list of dicts with at least {"path": str, "spec_schema": dict|None}
    Returns: merged dict of {field_name: descriptor_dict}
    """
    by_path = {n["path"]: n for n in all_nodes}

    # Build the ancestor chain: ["component", "component/electronic", ..., path]
    segments = path.split("/")
    ancestor_paths = ["/".join(segments[:i]) for i in range(1, len(segments) + 1)]

    merged: dict = {}
    for ap in ancestor_paths:
        node = by_path.get(ap)
        if not node:
            continue
        schema = node.get("spec_schema") or {}
        # Filter out "constant" type fields — those are taxonomy metadata, not
        # user-supplied fields (e.g. technology="carbon-film" is implicit).
        for key, descriptor in schema.items():
            if isinstance(descriptor, dict) and descriptor.get("type") == "constant":
                continue
            merged[key] = descriptor

    return merged


def _make_node_list(rows) -> list[dict]:
    """Convert SQLAlchemy row results to plain dicts for collect_inherited_schema."""
    return [
        {"path": r.path, "spec_schema": r.spec_schema or {}}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Engineering unit formatting
# ---------------------------------------------------------------------------

_POWER_DISPLAY = {
    0.063: "1/16W", 0.1: "1/10W", 0.125: "1/8W",
    0.25: "1/4W",   0.5: "1/2W",  0.75: "3/4W",
    1.0: "1W",      2.0: "2W",    3.0: "3W",
    5.0: "5W",      10.0: "10W",  25.0: "25W",
    50.0: "50W",    100.0: "100W",
}


def _fmt_resistance(ohms: float) -> str:
    if ohms >= 1_000_000:
        v = ohms / 1_000_000
        return _trim(v) + "MΩ"
    if ohms >= 1_000:
        v = ohms / 1_000
        return _trim(v) + "kΩ"
    return _trim(ohms) + "Ω"


def _fmt_capacitance(farads: float) -> str:
    if farads < 1e-9:
        return _trim(farads * 1e12) + "pF"
    if farads < 1e-6:
        return _trim(farads * 1e9) + "nF"
    if farads < 1e-3:
        return _trim(farads * 1e6) + "µF"
    return _trim(farads * 1e3) + "mF"


def _fmt_inductance(henries: float) -> str:
    if henries < 1e-6:
        return _trim(henries * 1e9) + "nH"
    if henries < 1e-3:
        return _trim(henries * 1e6) + "µH"
    if henries < 1:
        return _trim(henries * 1e3) + "mH"
    return _trim(henries) + "H"


def _fmt_power(watts: float) -> str:
    # Check exact match to preferred display string
    for w, label in _POWER_DISPLAY.items():
        if abs(watts - w) < 1e-9:
            return label
    return _trim(watts) + "W"


def _trim(v: float) -> str:
    """Remove trailing zeros after decimal, e.g. 4.70 → '4.7', 10.0 → '10'."""
    s = f"{v:.6g}"
    return s


def _format_field_value(field_name: str, raw_value) -> str:
    """
    Apply engineering unit formatting for known numeric field types.
    Falls back to str(raw_value) for unknown fields.
    """
    if raw_value is None:
        return "?"
    try:
        fv = float(raw_value)
    except (TypeError, ValueError):
        return str(raw_value)

    name = field_name.lower()
    if name == "resistance":
        return _fmt_resistance(fv)
    if name == "capacitance":
        return _fmt_capacitance(fv)
    if name == "inductance":
        return _fmt_inductance(fv)
    if name == "power_rating":
        return _fmt_power(fv)
    if name in ("voltage_rating", "voltage", "zener_voltage", "varistor_voltage",
                "output_voltage", "standby_voltage", "clamping_voltage"):
        return _trim(fv) + "V"
    if name in ("current_rating", "max_forward_current", "max_output_current",
                "rated_current", "saturation_current", "ic_max"):
        return _trim(fv) + "A"
    if name == "frequency":
        if fv >= 1e9:
            return _trim(fv / 1e9) + "GHz"
        if fv >= 1e6:
            return _trim(fv / 1e6) + "MHz"
        if fv >= 1e3:
            return _trim(fv / 1e3) + "kHz"
        return _trim(fv) + "Hz"
    # Default: return numeric string (no unit suffix; units live in descriptor)
    return _trim(fv)


# ---------------------------------------------------------------------------
# Label rendering
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{([^}]+)\}")


def render_label(
    label_format: Optional[str],
    specs: Optional[dict],
    inherited_schema: Optional[dict] = None,
) -> Optional[str]:
    """
    Fill label_format template with values from specs dict.

    label_format: e.g. "{resistance}-CFR-{power_rating}-{tolerance}"
    specs:        e.g. {"resistance": 100, "power_rating": 0.25, "tolerance": "5%"}
    inherited_schema: used to know which fields are numeric for unit formatting.

    Returns the rendered string, or None if label_format is falsy.
    """
    if not label_format:
        return None
    if not specs:
        # All placeholders become "?"
        return _PLACEHOLDER_RE.sub("?", label_format)

    schema = inherited_schema or {}

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        raw = specs.get(key)
        if raw is None:
            return "?"
        descriptor = schema.get(key) or {}
        dtype = descriptor.get("type", "string")
        # Format numeric fields with engineering units
        if dtype in ("number",) or key in (
            "resistance", "capacitance", "inductance", "power_rating",
            "voltage_rating", "current_rating", "zener_voltage", "varistor_voltage",
            "output_voltage", "standby_voltage", "clamping_voltage",
            "max_forward_current", "max_output_current", "rated_current",
        ):
            return _format_field_value(key, raw)
        return str(raw)

    return _PLACEHOLDER_RE.sub(_replace, label_format)
