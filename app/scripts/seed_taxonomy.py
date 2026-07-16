"""
Seeds the component_taxonomy table with a standard electronics hierarchy.

Run inside the container:
    docker compose exec app python -m app.scripts.seed_taxonomy

Safe to re-run — uses INSERT ... ON CONFLICT DO NOTHING, so existing rows
are left untouched.  Any taxonomy node already in the DB keeps its current
spec_schema and parent linkage.
"""
import asyncio
import uuid

from sqlalchemy import text

from app.models.database import AsyncSessionLocal


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Tree definition
# Each entry: (path, name, parent_path_or_None, spec_schema_or_None)
# ---------------------------------------------------------------------------
TAXONOMY: list[tuple[str, str, str | None, dict | None]] = [
    # ── Top-level categories ─────────────────────────────────────────────────
    ("passives",    "Passives",    None,          None),
    ("ics",         "ICs",         None,          None),
    ("modules",     "Modules",     None,          None),
    ("connectors",  "Connectors",  None,          None),
    ("mechanical",  "Mechanical",  None,          None),

    # ── Passives ─────────────────────────────────────────────────────────────
    ("passives/resistor",                  "Resistor",            "passives",
     {"fields": ["value", "unit", "tolerance", "power_rating", "package"]}),
    ("passives/resistor/fixed",            "Fixed Resistor",      "passives/resistor",
     {"fields": ["value", "unit", "tolerance", "power_rating", "package"]}),
    ("passives/resistor/variable",         "Variable Resistor",   "passives/resistor",
     {"fields": ["value_max", "unit", "taper", "package"]}),
    ("passives/resistor/network",          "Resistor Network",    "passives/resistor",
     {"fields": ["value", "unit", "count", "package"]}),

    ("passives/capacitor",                 "Capacitor",           "passives",
     {"fields": ["value", "unit", "voltage_rating", "tolerance", "package"]}),
    ("passives/capacitor/ceramic",         "Ceramic Capacitor",   "passives/capacitor",
     {"fields": ["value", "unit", "voltage_rating", "tolerance", "dielectric", "package"]}),
    ("passives/capacitor/electrolytic",    "Electrolytic Cap",    "passives/capacitor",
     {"fields": ["value", "unit", "voltage_rating", "esr_mohm", "package"]}),
    ("passives/capacitor/film",            "Film Capacitor",      "passives/capacitor",
     {"fields": ["value", "unit", "voltage_rating", "tolerance", "package"]}),
    ("passives/capacitor/tantalum",        "Tantalum Capacitor",  "passives/capacitor",
     {"fields": ["value", "unit", "voltage_rating", "esr_mohm", "package"]}),
    ("passives/capacitor/supercapacitor",  "Supercapacitor",      "passives/capacitor",
     {"fields": ["value", "unit", "voltage_rating", "package"]}),

    ("passives/inductor",                  "Inductor",            "passives",
     {"fields": ["value", "unit", "current_rating", "dcr_mohm", "package"]}),
    ("passives/inductor/power",            "Power Inductor",      "passives/inductor",
     {"fields": ["value", "unit", "current_rating", "dcr_mohm", "saturation_current", "package"]}),
    ("passives/inductor/choke",            "Common Mode Choke",   "passives/inductor",
     {"fields": ["value", "unit", "current_rating", "package"]}),

    ("passives/ferrite",                   "Ferrite Bead",        "passives",
     {"fields": ["impedance_100mhz", "current_rating", "package"]}),
    ("passives/crystal",                   "Crystal / Oscillator","passives",
     {"fields": ["frequency_mhz", "load_capacitance_pf", "package"]}),
    ("passives/fuse",                      "Fuse",                "passives",
     {"fields": ["current_rating", "voltage_rating", "response", "package"]}),
    ("passives/varistor",                  "Varistor / MOV",      "passives",
     {"fields": ["clamping_voltage", "energy_j", "package"]}),

    # ── ICs ──────────────────────────────────────────────────────────────────
    ("ics/mcu",                "Microcontroller",      "ics",
     {"fields": ["cpu_arch", "flash_kb", "ram_kb", "io_count", "package"]}),
    ("ics/mcu/esp32",          "ESP32 Family",         "ics/mcu",
     {"fields": ["variant", "flash_mb", "psram_mb", "wifi", "ble", "package"]}),
    ("ics/mcu/stm32",          "STM32 Family",         "ics/mcu",
     {"fields": ["variant", "flash_kb", "ram_kb", "package"]}),
    ("ics/mcu/atmega",         "ATmega Family",        "ics/mcu",
     {"fields": ["variant", "flash_kb", "ram_kb", "package"]}),
    ("ics/mcu/rp2040",         "RP2040",               "ics/mcu",
     {"fields": ["flash_mb", "package"]}),

    ("ics/opamp",              "Op-Amp",               "ics",
     {"fields": ["channel_count", "gbw_mhz", "supply_voltage_max", "package"]}),
    ("ics/comparator",         "Comparator",           "ics",
     {"fields": ["channel_count", "supply_voltage_max", "package"]}),

    ("ics/regulator",          "Voltage Regulator",    "ics",
     {"fields": ["output_voltage", "current_rating", "dropout_mv", "package"]}),
    ("ics/regulator/linear",   "LDO Linear",           "ics/regulator",
     {"fields": ["output_voltage", "current_rating", "dropout_mv", "package"]}),
    ("ics/regulator/switching","Switching Regulator",  "ics/regulator",
     {"fields": ["topology", "output_voltage", "current_rating", "frequency_khz", "package"]}),

    ("ics/gate",               "Logic Gate",           "ics",
     {"fields": ["family", "function", "channel_count", "package"]}),
    ("ics/flipflop",           "Flip-Flop / Latch",    "ics",
     {"fields": ["family", "type", "channel_count", "package"]}),
    ("ics/shiftreg",           "Shift Register",       "ics",
     {"fields": ["family", "bits", "package"]}),
    ("ics/mux",                "Multiplexer",          "ics",
     {"fields": ["channels", "supply_voltage_max", "package"]}),

    ("ics/driver",             "Driver IC",            "ics",
     {"fields": ["function", "current_rating", "package"]}),
    ("ics/driver/motor",       "Motor Driver",         "ics/driver",
     {"fields": ["channels", "current_rating", "voltage_rating", "package"]}),
    ("ics/driver/led",         "LED Driver",           "ics/driver",
     {"fields": ["channels", "current_max_ma", "package"]}),
    ("ics/driver/gate",        "Gate Driver",          "ics/driver",
     {"fields": ["channels", "peak_current_a", "package"]}),

    ("ics/interface",          "Interface IC",         "ics",
     {"fields": ["protocol", "package"]}),
    ("ics/interface/usb",      "USB Interface",        "ics/interface",
     {"fields": ["usb_version", "function", "package"]}),
    ("ics/interface/uart",     "UART / RS-232",        "ics/interface",
     {"fields": ["channels", "package"]}),
    ("ics/interface/can",      "CAN Transceiver",      "ics/interface",
     {"fields": ["package"]}),
    ("ics/interface/i2c_gpio", "I2C GPIO Expander",    "ics/interface",
     {"fields": ["io_count", "address_bits", "package"]}),

    ("ics/memory",             "Memory IC",            "ics",
     {"fields": ["type", "capacity_kb", "interface", "package"]}),
    ("ics/memory/flash",       "SPI Flash",            "ics/memory",
     {"fields": ["capacity_mb", "package"]}),
    ("ics/memory/eeprom",      "EEPROM",               "ics/memory",
     {"fields": ["capacity_kb", "interface", "package"]}),

    ("ics/sensor",             "Sensor IC",            "ics",
     {"fields": ["measurand", "interface", "range", "package"]}),
    ("ics/sensor/temp",        "Temperature Sensor",   "ics/sensor",
     {"fields": ["range_c", "accuracy_c", "interface", "package"]}),
    ("ics/sensor/imu",         "IMU",                  "ics/sensor",
     {"fields": ["axes", "interface", "package"]}),

    # ── Discretes (diodes, transistors, FETs) ────────────────────────────────
    ("ics/diode",              "Diode",                "ics",
     {"fields": ["type", "voltage_rating", "current_rating", "package"]}),
    ("ics/diode/rectifier",    "Rectifier Diode",      "ics/diode",
     {"fields": ["voltage_rating", "current_rating", "package"]}),
    ("ics/diode/schottky",     "Schottky Diode",       "ics/diode",
     {"fields": ["voltage_rating", "current_rating", "vf_mv", "package"]}),
    ("ics/diode/zener",        "Zener Diode",          "ics/diode",
     {"fields": ["zener_voltage", "power_rating", "package"]}),
    ("ics/diode/tvs",          "TVS Diode",            "ics/diode",
     {"fields": ["breakdown_voltage", "peak_power_w", "package"]}),
    ("ics/diode/led",          "LED",                  "ics/diode",
     {"fields": ["led_color", "wavelength_nm", "vf_mv", "current_ma", "package"]}),

    ("ics/transistor",         "Transistor",           "ics",
     {"fields": ["type", "voltage_rating", "current_rating", "hfe", "package"]}),
    ("ics/transistor/bjt_npn", "BJT NPN",              "ics/transistor",
     {"fields": ["vceo", "ic_max", "hfe_min", "package"]}),
    ("ics/transistor/bjt_pnp", "BJT PNP",              "ics/transistor",
     {"fields": ["vceo", "ic_max", "hfe_min", "package"]}),
    ("ics/transistor/mosfet_n","MOSFET N-Chan",        "ics/transistor",
     {"fields": ["vds_max", "id_max", "rds_on_mohm", "vgs_th", "package"]}),
    ("ics/transistor/mosfet_p","MOSFET P-Chan",        "ics/transistor",
     {"fields": ["vds_max", "id_max", "rds_on_mohm", "vgs_th", "package"]}),

    # ── Modules ──────────────────────────────────────────────────────────────
    ("modules/wireless",       "Wireless Module",      "modules",
     {"fields": ["protocol", "frequency_mhz", "form_factor"]}),
    ("modules/wireless/wifi",  "WiFi Module",          "modules/wireless",
     {"fields": ["wifi_standard", "antenna_type", "form_factor"]}),
    ("modules/wireless/ble",   "BLE Module",           "modules/wireless",
     {"fields": ["ble_version", "antenna_type", "form_factor"]}),
    ("modules/wireless/lora",  "LoRa Module",          "modules/wireless",
     {"fields": ["frequency_mhz", "output_dbm", "form_factor"]}),

    ("modules/display",        "Display Module",       "modules",
     {"fields": ["type", "resolution", "interface", "diagonal_in"]}),
    ("modules/sensor",         "Sensor Module",        "modules",
     {"fields": ["measurand", "interface"]}),
    ("modules/power",          "Power Module",         "modules",
     {"fields": ["function", "input_voltage", "output_voltage", "current_rating"]}),

    # ── Connectors ───────────────────────────────────────────────────────────
    ("connectors/usb",         "USB Connector",        "connectors",
     {"fields": ["usb_type", "orientation", "mounting", "current_rating"]}),
    ("connectors/usb/usb_a",   "USB-A",                "connectors/usb",
     {"fields": ["orientation", "mounting"]}),
    ("connectors/usb/usb_c",   "USB-C",                "connectors/usb",
     {"fields": ["current_rating", "mounting"]}),
    ("connectors/usb/micro",   "Micro USB",            "connectors/usb",
     {"fields": ["orientation", "mounting"]}),

    ("connectors/header",      "Pin Header",           "connectors",
     {"fields": ["pitch_mm", "pin_count", "rows", "gender", "mounting"]}),
    ("connectors/header/2_54", "2.54mm Header",        "connectors/header",
     {"fields": ["pin_count", "rows", "gender", "mounting"]}),
    ("connectors/header/1_27", "1.27mm Header",        "connectors/header",
     {"fields": ["pin_count", "rows", "gender", "mounting"]}),

    ("connectors/jst",         "JST Connector",        "connectors",
     {"fields": ["series", "pin_count", "pitch_mm", "gender"]}),
    ("connectors/terminal",    "Terminal Block",       "connectors",
     {"fields": ["pitch_mm", "pin_count", "current_rating", "mounting"]}),
    ("connectors/barrel",      "Barrel Jack",          "connectors",
     {"fields": ["inner_mm", "outer_mm", "current_rating", "mounting"]}),
    ("connectors/sma",         "SMA / RF Connector",   "connectors",
     {"fields": ["gender", "mounting", "frequency_ghz_max"]}),

    # ── Mechanical ───────────────────────────────────────────────────────────
    ("mechanical/fastener",    "Fastener",             "mechanical",
     {"fields": ["thread", "length_mm", "material", "head_type"]}),
    ("mechanical/standoff",    "Standoff / Spacer",    "mechanical",
     {"fields": ["thread", "length_mm", "material"]}),
    ("mechanical/heatsink",    "Heatsink",             "mechanical",
     {"fields": ["rth_c_w", "material", "mounting"]}),
]


async def seed_component_taxonomy() -> None:
    # ── First pass: collect all paths we intend to insert ────────────────────
    # We need parent IDs before children, but the table may have pre-existing
    # rows, so we fetch any existing rows first.
    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(text("SELECT path, id FROM component_taxonomy"))
        ).fetchall()
        path_to_id: dict[str, str] = {row.path: row.id for row in existing}

        inserted = 0
        skipped = 0

        # Process in definition order — parents are listed before children.
        import json
        for path, name, parent_path, spec_schema in TAXONOMY:
            parent_id = path_to_id.get(parent_path) if parent_path else None

            node_id = path_to_id.get(path)
            if node_id:
                skipped += 1
                continue

            node_id = _uid()
            schema_val = json.dumps(spec_schema) if spec_schema else None

            # asyncpg uses $N positional params — avoid ::jsonb cast syntax in
            # the SQL string; pass schema as text and let PG coerce via column type.
            await db.execute(text("""
                INSERT INTO component_taxonomy (id, path, name, parent_id, spec_schema)
                VALUES (:id, :path, :name, :parent_id, CAST(:spec_schema AS jsonb))
                ON CONFLICT (path) DO NOTHING
            """), {
                "id": node_id,
                "path": path,
                "name": name,
                "parent_id": parent_id,
                "spec_schema": schema_val,
            })
            path_to_id[path] = node_id
            inserted += 1

        await db.commit()
        print(f"Taxonomy seed complete: {inserted} inserted, {skipped} already existed.")

        # Print tree summary
        rows = (
            await db.execute(text(
                "SELECT path, name FROM component_taxonomy ORDER BY path"
            ))
        ).fetchall()
        print(f"\nComponent taxonomy ({len(rows)} nodes):")
        for row in rows:
            depth = row.path.count("/")
            indent = "  " * depth
            print(f"  {indent}{row.name}  [{row.path}]")


if __name__ == "__main__":
    asyncio.run(seed_component_taxonomy())
