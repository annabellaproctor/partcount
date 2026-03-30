"""
Generic component SVG icons — schematic/electronic symbol style.
Used as fallback when no image available.
CC0 / original artwork.
"""

ICONS: dict[str, str] = {
    "resistor": '''<svg viewBox="0 0 60 30" xmlns="http://www.w3.org/2000/svg">
  <line x1="0" y1="15" x2="10" y2="15" stroke="#3ddc84" stroke-width="2"/>
  <rect x="10" y="8" width="40" height="14" rx="2" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <line x1="50" y1="15" x2="60" y2="15" stroke="#3ddc84" stroke-width="2"/>
</svg>''',

    "capacitor": '''<svg viewBox="0 0 60 40" xmlns="http://www.w3.org/2000/svg">
  <line x1="0" y1="20" x2="24" y2="20" stroke="#3ddc84" stroke-width="2"/>
  <line x1="24" y1="5" x2="24" y2="35" stroke="#3ddc84" stroke-width="2.5"/>
  <line x1="28" y1="5" x2="28" y2="35" stroke="#3ddc84" stroke-width="2.5"/>
  <line x1="28" y1="20" x2="52" y2="20" stroke="#3ddc84" stroke-width="2"/>
  <line x1="0" y1="20" x2="52" y2="20" stroke="none"/>
</svg>''',

    "diode": '''<svg viewBox="0 0 60 30" xmlns="http://www.w3.org/2000/svg">
  <line x1="0" y1="15" x2="18" y2="15" stroke="#3ddc84" stroke-width="2"/>
  <polygon points="18,5 18,25 38,15" fill="none" stroke="#3ddc84" stroke-width="2" stroke-linejoin="round"/>
  <line x1="38" y1="5" x2="38" y2="25" stroke="#3ddc84" stroke-width="2"/>
  <line x1="38" y1="15" x2="60" y2="15" stroke="#3ddc84" stroke-width="2"/>
</svg>''',

    "led": '''<svg viewBox="0 0 70 35" xmlns="http://www.w3.org/2000/svg">
  <line x1="0" y1="17" x2="18" y2="17" stroke="#3ddc84" stroke-width="2"/>
  <polygon points="18,7 18,27 38,17" fill="rgba(61,220,132,0.15)" stroke="#3ddc84" stroke-width="2" stroke-linejoin="round"/>
  <line x1="38" y1="7" x2="38" y2="27" stroke="#3ddc84" stroke-width="2"/>
  <line x1="38" y1="17" x2="60" y2="17" stroke="#3ddc84" stroke-width="2"/>
  <line x1="42" y1="3" x2="52" y2="0" stroke="#3ddc84" stroke-width="1.5" marker-end="url(#a)"/>
  <line x1="46" y1="8" x2="56" y2="5" stroke="#3ddc84" stroke-width="1.5"/>
</svg>''',

    "transistor": '''<svg viewBox="0 0 50 50" xmlns="http://www.w3.org/2000/svg">
  <circle cx="25" cy="25" r="20" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <line x1="5" y1="25" x2="18" y2="25" stroke="#3ddc84" stroke-width="2"/>
  <line x1="18" y1="10" x2="18" y2="40" stroke="#3ddc84" stroke-width="2.5"/>
  <line x1="18" y1="18" x2="35" y2="8" stroke="#3ddc84" stroke-width="2"/>
  <line x1="35" y1="8" x2="35" y2="0" stroke="#3ddc84" stroke-width="2"/>
  <line x1="18" y1="32" x2="35" y2="42" stroke="#3ddc84" stroke-width="2"/>
  <line x1="35" y1="42" x2="35" y2="50" stroke="#3ddc84" stroke-width="2"/>
  <polygon points="30,38 35,42 29,44" fill="#3ddc84"/>
</svg>''',

    "mosfet": '''<svg viewBox="0 0 50 60" xmlns="http://www.w3.org/2000/svg">
  <circle cx="25" cy="30" r="22" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <line x1="0" y1="30" x2="13" y2="30" stroke="#3ddc84" stroke-width="2"/>
  <line x1="13" y1="15" x2="13" y2="45" stroke="#3ddc84" stroke-width="2.5"/>
  <line x1="16" y1="16" x2="16" y2="26" stroke="#3ddc84" stroke-width="2.5"/>
  <line x1="16" y1="34" x2="16" y2="44" stroke="#3ddc84" stroke-width="2.5"/>
  <line x1="16" y1="21" x2="35" y2="21" stroke="#3ddc84" stroke-width="2"/>
  <line x1="35" y1="21" x2="35" y2="5" stroke="#3ddc84" stroke-width="2"/>
  <line x1="16" y1="39" x2="35" y2="39" stroke="#3ddc84" stroke-width="2"/>
  <line x1="35" y1="39" x2="35" y2="55" stroke="#3ddc84" stroke-width="2"/>
</svg>''',

    "ic": '''<svg viewBox="0 0 60 50" xmlns="http://www.w3.org/2000/svg">
  <rect x="10" y="8" width="40" height="34" rx="2" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <line x1="0" y1="16" x2="10" y2="16" stroke="#3ddc84" stroke-width="1.5"/>
  <line x1="0" y1="24" x2="10" y2="24" stroke="#3ddc84" stroke-width="1.5"/>
  <line x1="0" y1="32" x2="10" y2="32" stroke="#3ddc84" stroke-width="1.5"/>
  <line x1="50" y1="16" x2="60" y2="16" stroke="#3ddc84" stroke-width="1.5"/>
  <line x1="50" y1="24" x2="60" y2="24" stroke="#3ddc84" stroke-width="1.5"/>
  <line x1="50" y1="32" x2="60" y2="32" stroke="#3ddc84" stroke-width="1.5"/>
  <text x="30" y="30" font-size="10" fill="#3ddc84" text-anchor="middle" font-family="monospace">IC</text>
</svg>''',

    "inductor": '''<svg viewBox="0 0 80 30" xmlns="http://www.w3.org/2000/svg">
  <line x1="0" y1="15" x2="8" y2="15" stroke="#3ddc84" stroke-width="2"/>
  <path d="M8,15 Q14,2 20,15 Q26,28 32,15 Q38,2 44,15 Q50,28 56,15 Q62,2 68,15 Q74,28 80,15" fill="none" stroke="#3ddc84" stroke-width="2"/>
</svg>''',

    "connector": '''<svg viewBox="0 0 50 50" xmlns="http://www.w3.org/2000/svg">
  <rect x="8" y="5" width="34" height="40" rx="3" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <circle cx="25" cy="15" r="3" fill="none" stroke="#3ddc84" stroke-width="1.5"/>
  <circle cx="25" cy="25" r="3" fill="none" stroke="#3ddc84" stroke-width="1.5"/>
  <circle cx="25" cy="35" r="3" fill="none" stroke="#3ddc84" stroke-width="1.5"/>
</svg>''',

    "relay": '''<svg viewBox="0 0 60 50" xmlns="http://www.w3.org/2000/svg">
  <rect x="5" y="10" width="20" height="14" rx="1" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <line x1="0" y1="14" x2="5" y2="14" stroke="#3ddc84" stroke-width="2"/>
  <line x1="0" y1="20" x2="5" y2="20" stroke="#3ddc84" stroke-width="2"/>
  <path d="M8,17 Q12,12 16,17 Q20,22 24,17" fill="none" stroke="#3ddc84" stroke-width="1.5"/>
  <line x1="30" y1="15" x2="60" y2="15" stroke="#3ddc84" stroke-width="1.5" stroke-dasharray="3,2"/>
  <line x1="30" y1="35" x2="60" y2="35" stroke="#3ddc84" stroke-width="2"/>
  <line x1="40" y1="15" x2="48" y2="30" stroke="#3ddc84" stroke-width="2"/>
</svg>''',

    "crystal": '''<svg viewBox="0 0 60 40" xmlns="http://www.w3.org/2000/svg">
  <line x1="0" y1="20" x2="15" y2="20" stroke="#3ddc84" stroke-width="2"/>
  <line x1="15" y1="5" x2="15" y2="35" stroke="#3ddc84" stroke-width="2"/>
  <rect x="18" y="8" width="24" height="24" rx="1" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <line x1="42" y1="5" x2="42" y2="35" stroke="#3ddc84" stroke-width="2"/>
  <line x1="42" y1="20" x2="60" y2="20" stroke="#3ddc84" stroke-width="2"/>
</svg>''',

    "switch": '''<svg viewBox="0 0 60 30" xmlns="http://www.w3.org/2000/svg">
  <line x1="0" y1="15" x2="15" y2="15" stroke="#3ddc84" stroke-width="2"/>
  <circle cx="15" cy="15" r="2.5" fill="#3ddc84"/>
  <line x1="17" y1="12" x2="42" y2="5" stroke="#3ddc84" stroke-width="2"/>
  <circle cx="42" cy="15" r="2.5" fill="#3ddc84"/>
  <line x1="42" y1="15" x2="60" y2="15" stroke="#3ddc84" stroke-width="2"/>
</svg>''',

    "fuse": '''<svg viewBox="0 0 60 30" xmlns="http://www.w3.org/2000/svg">
  <line x1="0" y1="15" x2="10" y2="15" stroke="#3ddc84" stroke-width="2"/>
  <rect x="10" y="8" width="40" height="14" rx="7" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <line x1="10" y1="15" x2="50" y2="15" stroke="#3ddc84" stroke-width="1" stroke-dasharray="4,3"/>
  <line x1="50" y1="15" x2="60" y2="15" stroke="#3ddc84" stroke-width="2"/>
</svg>''',

    "module": '''<svg viewBox="0 0 60 50" xmlns="http://www.w3.org/2000/svg">
  <rect x="5" y="5" width="50" height="40" rx="3" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <rect x="12" y="12" width="16" height="12" rx="1" fill="none" stroke="#3ddc84" stroke-width="1.5"/>
  <rect x="32" y="12" width="16" height="12" rx="1" fill="none" stroke="#3ddc84" stroke-width="1.5"/>
  <line x1="12" y1="30" x2="48" y2="30" stroke="#3ddc84" stroke-width="1.5"/>
  <line x1="12" y1="36" x2="36" y2="36" stroke="#3ddc84" stroke-width="1.5"/>
</svg>''',

    "sensor": '''<svg viewBox="0 0 50 50" xmlns="http://www.w3.org/2000/svg">
  <circle cx="25" cy="25" r="18" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <circle cx="25" cy="25" r="8" fill="none" stroke="#3ddc84" stroke-width="1.5"/>
  <circle cx="25" cy="25" r="3" fill="#3ddc84"/>
  <line x1="25" y1="7" x2="25" y2="0" stroke="#3ddc84" stroke-width="2"/>
  <line x1="25" y1="43" x2="25" y2="50" stroke="#3ddc84" stroke-width="2"/>
</svg>''',

    "default": '''<svg viewBox="0 0 50 50" xmlns="http://www.w3.org/2000/svg">
  <circle cx="25" cy="25" r="20" fill="none" stroke="#3ddc84" stroke-width="2"/>
  <text x="25" y="30" font-size="18" fill="#3ddc84" text-anchor="middle" font-family="monospace">?</text>
</svg>''',
}


def get_icon(component_type: str) -> str:
    """Returns SVG string for a component type."""
    t = (component_type or "").lower().strip()
    return ICONS.get(t, ICONS["default"])


def get_icon_data_url(component_type: str) -> str:
    """Returns data URL for inline use."""
    import base64
    svg = get_icon(component_type)
    b64 = base64.b64encode(svg.encode()).decode()
    return f"data:image/svg+xml;base64,{b64}"


# Type keyword mapping for auto-detection from part name/description
TYPE_KEYWORDS = {
    "resistor": ["resistor", "resist", "ohm", "Ω", "crcw", "rl", "rc"],
    "capacitor": ["capacitor", "cap ", "ceramic", "electrolytic", "tantalum", "farad", "mlcc"],
    "inductor": ["inductor", "inductance", "ferrite", "choke", "coil", "henry"],
    "diode": ["diode", "schottky", "zener", "tvs", "rectifier"],
    "led": ["led", "light emit"],
    "transistor": ["transistor", "bjt", "npn", "pnp", "2n", "bc5", "bc4", "bc3"],
    "mosfet": ["mosfet", "nmos", "pmos", "irf", "ao32", "nch", "pch", "n-ch", "p-ch"],
    "ic": ["ic ", " ic", "chip", "controller", "microcontroller", "mcu", "processor",
           "amplifier", "op-amp", "opamp", "regulator", "driver", "gate", "logic",
           "esp32", "esp8266", "stm32", "atmega", "attiny"],
    "crystal": ["crystal", "oscillator", "resonator", "xtal"],
    "connector": ["connector", "header", "terminal", "socket", "plug", "jack"],
    "relay": ["relay"],
    "fuse": ["fuse", "polyfuse", "resettable"],
    "module": ["module", "board", "breakout", "shield", "hat", "eval", "dev kit"],
    "sensor": ["sensor", "imu", "accelerometer", "gyro", "temperature", "pressure",
               "humidity", "proximity", "light sensor", "current sensor"],
}


def infer_type(text: str) -> str:
    """Infer component type from name/description text."""
    t = (text or "").lower()
    for type_name, keywords in TYPE_KEYWORDS.items():
        if any(k.lower() in t for k in keywords):
            return type_name
    return "default"
