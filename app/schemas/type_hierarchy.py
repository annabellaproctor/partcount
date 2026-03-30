"""Component type hierarchy and type-specific field definitions."""

TYPE_HIERARCHY = {
    "passives": {
        "resistor": {
            "fields": ["value", "unit", "tolerance", "power_rating", "voltage_rating", "temperature_coefficient"],
            "required": ["value", "unit"],
            "defaults": {"unit": "Ω"},
            "subcategories": {
                "film": {},
                "wirewound": {},
                "smd": {},
            },
        },
        "capacitor": {
            "fields": ["value", "unit", "tolerance", "voltage_rating", "dielectric_type", "esr"],
            "required": ["value", "unit", "voltage_rating"],
            "defaults": {"unit": "F"},
            "subcategories": {
                "ceramic": {"fields": ["dielectric_type"]},
                "electrolytic": {"fields": ["polarity"]},
                "film": {},
                "supercapacitor": {"fields": ["capacitance", "esr"]},
            },
        },
        "inductor": {
            "fields": ["value", "unit", "tolerance", "current_rating", "saturation_current"],
            "required": ["value", "unit"],
            "defaults": {"unit": "H"},
            "subcategories": {
                "coil": {},
                "ferrite": {},
                "toroid": {},
            },
        },
    },
    "actives": {
        "diode": {
            "fields": ["type", "voltage_rating", "current_rating", "forward_voltage"],
            "subcategories": {
                "rectifier": {},
                "schottky": {},
                "zener": {"fields": ["zener_voltage"]},
                "led": {
                    "fields": ["color", "forward_voltage", "forward_current", "wavelength", "viewing_angle", "color_temperature", "luminous_intensity"],
                    "required": ["color", "forward_voltage"],
                    "subcategories": {
                        "standard": {},
                        "rgb": {"fields": ["red_wavelength", "green_wavelength", "blue_wavelength"]},
                        "addressable": {"fields": ["protocol", "ic_model"]},
                    },
                },
            },
        },
        "transistor": {
            "fields": ["type", "voltage_rating", "current_rating", "power_rating", "gain"],
            "subcategories": {
                "bjt": {
                    "subcategories": {
                        "npn": {},
                        "pnp": {},
                    },
                },
                "mosfet": {
                    "fields": ["gate_type", "rds_on", "vgs_threshold"],
                    "subcategories": {
                        "n-channel": {},
                        "p-channel": {},
                    },
                },
            },
        },
        "ic": {
            "fields": ["pin_count", "logic_family", "supply_voltage_min", "supply_voltage_max", "interface"],
            "subcategories": {
                "logic": {"fields": ["logic_family"]},
                "microcontroller": {"fields": ["architecture", "flash_size", "ram_size", "clock_speed"]},
                "memory": {"fields": ["memory_type", "capacity", "interface"]},
                "opamp": {"fields": ["supply_voltage", "slew_rate", "gain_bandwidth"]},
                "regulator": {
                    "fields": ["output_voltage", "current_rating", "dropout_voltage"],
                    "subcategories": {
                        "linear": {},
                        "switching": {"fields": ["efficiency", "switching_frequency"]},
                    },
                },
            },
        },
    },
    "connectors": {
        "_fields": ["pin_count", "pitch", "gender", "mounting_type", "current_rating"],
        "header": {
            "subcategories": {
                "pin": {},
                "socket": {},
                "shrouded": {},
            },
        },
        "terminal-block": {},
        "usb": {"fields": ["usb_version"]},
        "hdmi": {},
        "rf": {"fields": ["impedance", "frequency_range"]},
    },
    "electromechanical": {
        "switch": {
            "fields": ["contact_configuration", "voltage_rating", "current_rating", "actuation_type"],
            "subcategories": {
                "tactile": {},
                "toggle": {},
                "slide": {},
                "dip": {},
            },
        },
        "relay": {"fields": ["coil_voltage", "contact_configuration", "switching_current"]},
        "button": {"fields": ["actuation_force"]},
        "potentiometer": {"fields": ["resistance", "taper", "power_rating"]},
    },
    "modules": {
        "development-board": {
            "fields": [
                "mcu_family",
                "core_count",
                "clock_speed_mhz",
                "ram_size_kb",
                "flash_size_mb",
                "pin_count",
                "wireless",
                "usb_bridge",
                "interface",
                "form_factor",
            ],
            "subcategories": {
                "microcontroller": {
                    "fields": ["architecture", "wireless", "peripherals"],
                },
                "single-board-computer": {
                    "fields": ["cpu", "ram_size_gb", "storage", "os_support"],
                },
            },
        },
        "mcu-module": {
            "fields": [
                "mcu_family",
                "core_count",
                "clock_speed_mhz",
                "ram_size_kb",
                "flash_size_mb",
                "wireless",
                "antenna_type",
                "interface",
            ],
            "subcategories": {
                "esp32": {
                    "fields": ["variant", "wifi", "bluetooth", "adc_channels", "dac_channels"],
                },
                "stm32": {
                    "fields": ["variant", "series", "package", "io_count"],
                },
                "nrf": {
                    "fields": ["variant", "bluetooth", "radio_protocols"],
                },
            },
        },
        "sensor": {
            "fields": ["measurement_type", "interface", "voltage_range", "accuracy"],
            "subcategories": {
                "temperature": {"fields": ["temperature_range", "resolution"]},
                "humidity": {},
                "pressure": {},
                "motion": {},
                "light": {},
            },
        },
        "display": {
            "subcategories": {
                "lcd": {},
                "oled": {},
                "led-matrix": {},
            },
        },
        "communication": {
            "fields": ["interface", "frequency_range", "data_rate"],
            "subcategories": {
                "wifi": {},
                "bluetooth": {},
                "lora": {},
            },
        },
        "power": {
            "subcategories": {
                "converter": {},
                "charger": {},
                "battery-management": {},
            },
        },
    },
}


def get_fields_for_type(type_path: str) -> dict:
    """Return all fields, required keys, and defaults for a type path."""
    parts = [p for p in (type_path or "").split("/") if p]
    current = TYPE_HIERARCHY
    all_fields: list[str] = []
    required: list[str] = []
    defaults: dict = {}

    for part in parts:
        if part not in current:
            break

        current = current[part]
        if isinstance(current, dict):
            all_fields.extend(current.get("fields", []))
            required.extend(current.get("required", []))
            defaults.update(current.get("defaults", {}))
            all_fields.extend(current.get("_fields", []))
            if "subcategories" in current and isinstance(current["subcategories"], dict):
                current = current["subcategories"]

    # Preserve order while removing duplicates.
    dedup_fields = list(dict.fromkeys(all_fields))
    dedup_required = list(dict.fromkeys(required))
    return {
        "fields": dedup_fields,
        "required": dedup_required,
        "defaults": defaults,
    }


def flatten_type_paths(tree: dict | None = None, prefix: str = "") -> list[str]:
    """Flatten hierarchy into supported type paths for prompts/UI dropdowns."""
    tree = tree or TYPE_HIERARCHY
    paths: list[str] = []

    for key, node in tree.items():
        if key.startswith("_"):
            continue
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(node, dict) and "subcategories" in node and isinstance(node["subcategories"], dict):
            subpaths = flatten_type_paths(node["subcategories"], path)
            if subpaths:
                paths.extend(subpaths)
            else:
                paths.append(path)
        else:
            paths.append(path)

    return paths
