"""Dewey-style shelf keys for ordering bags inside a filing crate.

Two representations, both derived from the component — never stored:

    sort key     CAP::000000000.000000100000000::0016::0000::0100
                 (fixed width, no units — padding is what makes it sort)
    display key  CAP::100nF::16V::10%
                 (padding stripped, units restored, for labels/UI)

Fixed-width zero padding is what makes a plain string sort match numeric
order, so segment widths here are load-bearing: changing one reorders shelves.
"""

import re

from app.services.short_title import _type_prefix

# Value is normalized to base units (Ω, F, H) and can span picofarads to
# megaohms, so it is carried as a zero-padded fixed-point decimal rather than
# a float — exponent notation does not sort lexically.
_VALUE_INT_DIGITS = 9
_VALUE_FRAC_DIGITS = 15
_VALUE_WIDTH = _VALUE_INT_DIGITS + 1 + _VALUE_FRAC_DIGITS

# Only `m`/`M` are case-sensitive (milli vs mega); every other suffix folds.
_SI_SUFFIXES = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,  # U+03BC greek mu, distinct from U+00B5 micro sign
    "m": 1e-3,
    "M": 1e6,
    "r": 1.0,  # resistor notation: 4R7 == 4.7Ω
    "k": 1e3,
    "meg": 1e6,
    "g": 1e9,
}

_SUFFIX_CHARS = "pnuµμmMrRkKgG"

# Unit words/symbols that may trail a value; stripped before parsing.
_UNIT_TAIL = re.compile(
    r"(ohms?|Ω|ω|farads?|henr(?:y|ies)|volts?|amps?|amperes?|[FHVA])\s*$",
    re.IGNORECASE,
)

# 4k7 / 1M5 / 4R7 — suffix acts as the decimal point.
_INFIX = re.compile(rf"^(\d+)\s*(meg|MEG|Meg|[{_SUFFIX_CHARS}])\s*(\d+)$")

# 4.7k / 100n / 470R / 100
_SUFFIXED = re.compile(rf"^(\d*\.?\d+)\s*(meg|MEG|Meg|[{_SUFFIX_CHARS}])?$")


def _unit_multiplier(unit: str | None) -> float:
    """Multiplier carried by a unit string: 'kΩ' -> 1e3, 'uF' -> 1e-6, 'Ω' -> 1.

    Much of the inventory splits a value across two columns -- value='10',
    unit='kΩ' -- so the magnitude is meaningless without the unit.
    """
    if not unit:
        return 1.0
    u = str(unit).strip()
    if len(u) < 2:
        return 1.0
    # A leading SI prefix only counts if a real unit symbol follows it.
    head, tail = u[0], u[1:]
    if not _UNIT_TAIL.match(tail.strip()):
        return 1.0
    return _multiplier(head) or 1.0


def parse_value(raw: str | None, unit: str | None = None) -> float | None:
    """Parse a component value into base units. Returns None if unreadable.

    Case matters: `m` is milli, `M` is mega. `4k7` and `4R7` place the suffix
    where the decimal point goes. When the value carries no SI prefix of its
    own, one is taken from `unit` ('10' + 'kΩ' -> 10000.0).
    """
    if raw is None:
        return None

    v = str(raw).strip()
    if not v:
        return None

    v = v.replace(",", "")
    had_own_suffix = bool(_SUFFIXED.match(v) and _SUFFIXED.match(v).group(2)) or bool(_INFIX.match(v))
    v = _UNIT_TAIL.sub("", v).strip()
    if not v:
        return None

    scale = 1.0 if had_own_suffix else _unit_multiplier(unit)

    m = _INFIX.match(v)
    if m:
        whole, suffix, frac = m.groups()
        mult = _multiplier(suffix)
        if mult is None:
            return None
        try:
            return float(f"{whole}.{frac}") * mult * scale
        except ValueError:
            return None

    m = _SUFFIXED.match(v)
    if m:
        number, suffix = m.groups()
        mult = 1.0 if suffix is None else _multiplier(suffix)
        if mult is None:
            return None
        try:
            return float(number) * mult * scale
        except ValueError:
            return None

    return None


def _multiplier(suffix: str) -> float | None:
    if suffix in _SI_SUFFIXES:  # exact match first: preserves m/M case
        return _SI_SUFFIXES[suffix]
    return _SI_SUFFIXES.get(suffix.lower())


def _pad_value(value: float | None) -> str:
    if value is None:
        return "?" * _VALUE_WIDTH  # unknown sorts last, after every real value
    return f"{value:0{_VALUE_WIDTH}.{_VALUE_FRAC_DIGITS}f}"


def _pad_rating(value: float | None, width: int = 4) -> str:
    if value is None:
        return "0" * width
    return f"{int(round(value)):0{width}d}"


def parse_tolerance(raw: str | None) -> float | None:
    """Pull a percentage out of free-text tolerance (`±1%`, `5 %`, `0.1%`)."""
    if not raw:
        return None
    m = re.search(r"(\d*\.?\d+)\s*%", str(raw))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _pad_tolerance(pct: float | None) -> str:
    if pct is None:
        return "0000"
    return f"{int(round(pct * 10)):04d}"  # tenths, so 0.1% -> 0001


def _unit_symbol(component) -> str:
    """Best-effort BASE unit symbol, from the component's own unit or its type.

    Strips any SI prefix ('kΩ' -> 'Ω'): the magnitude is already folded into the
    parsed value, and re-appending it would render 10kΩ as "10kkΩ".
    """
    unit = (getattr(component, "unit", None) or "").strip()
    if unit:
        if len(unit) > 1 and _UNIT_TAIL.match(unit[1:].strip()) and _multiplier(unit[0]):
            return unit[1:].strip()
        return unit

    prefix = _type_prefix(
        getattr(component, "type_path", None),
        None,
        getattr(component, "name", None),
    )
    return {"RES": "Ω", "CAP": "F", "IND": "H"}.get(prefix, "")


def _humanize_value(value: float | None, raw: str | None, unit: str = "") -> str:
    """Render a base-unit value back to compact engineering notation with unit.

    Sub-kilo ohms use R-notation (4R7) rather than a bare decimal, which reads
    as a version number on a bag label.
    """
    if value is None:
        return (raw or "?").strip()
    if value == 0:
        return f"0{unit}"

    for suffix, scale in (
        ("G", 1e9),
        ("M", 1e6),
        ("k", 1e3),
        ("", 1.0),
        ("m", 1e-3),
        ("u", 1e-6),
        ("n", 1e-9),
        ("p", 1e-12),
    ):
        if abs(value) >= scale:
            scaled = value / scale
            text = f"{scaled:.3f}".rstrip("0").rstrip(".")

            if unit == "Ω" and not suffix:
                # 4.7Ω -> 4R7, 470Ω -> 470R
                return f"{text.replace('.', 'R')}R" if "." not in text else text.replace(".", "R")

            return f"{text}{suffix}{unit}"

    return f"{value:g}{unit}"


def shelf_sort_key(component) -> str:
    """Fixed-width key ordering a bag on the shelf. Compute, never store."""
    prefix = _type_prefix(
        getattr(component, "type_path", None),
        getattr(component, "unit", None),
        getattr(component, "name", None),
    )
    value = parse_value(getattr(component, "value", None), getattr(component, "unit", None))
    tolerance = parse_tolerance(getattr(component, "tolerance", None))

    return "::".join(
        (
            prefix,
            _pad_value(value),
            _pad_rating(getattr(component, "voltage_rating", None)),
            _pad_rating(getattr(component, "current_rating", None)),
            _pad_tolerance(tolerance),
        )
    )


def shelf_display_key(component) -> str:
    """Human-facing key for labels and the crate UI. Padding stripped."""
    prefix = _type_prefix(
        getattr(component, "type_path", None),
        getattr(component, "unit", None),
        getattr(component, "name", None),
    )
    raw_value = getattr(component, "value", None)
    parsed = parse_value(raw_value, getattr(component, "unit", None))

    if parsed is None:
        # No readable value — fall back to something nameable rather than "?"
        fallback = (
            getattr(component, "short_title", None)
            or getattr(component, "mpn", None)
            or (raw_value or "").strip()
            or getattr(component, "name", None)
            or "?"
        )
        parts = [prefix, str(fallback).strip()]
    else:
        parts = [prefix, _humanize_value(parsed, raw_value, _unit_symbol(component))]

    voltage = getattr(component, "voltage_rating", None)
    if voltage:
        parts.append(f"{voltage:g}V")

    current = getattr(component, "current_rating", None)
    if current:
        parts.append(f"{current:g}A")

    tolerance = parse_tolerance(getattr(component, "tolerance", None))
    if tolerance is not None:
        parts.append(f"{tolerance:g}%")

    return "::".join(parts)
