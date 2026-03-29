import httpx
import time
import os

INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8181")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_DB = os.getenv("INFLUX_DB", "labinventory")

HEADERS = {
    "Authorization": f"Bearer {INFLUX_TOKEN}",
    "Content-Type": "text/plain; charset=utf-8",
}


def _write(line_protocol: str):
    """Write line protocol to InfluxDB3 v3 API"""
    try:
        url = f"{INFLUX_URL}/api/v3/write_lp?db={INFLUX_DB}&precision=nanosecond"
        r = httpx.post(url, content=line_protocol, headers={
            "Authorization": f"Bearer {INFLUX_TOKEN}",
            "Content-Type": "text/plain; charset=utf-8",
        }, timeout=5)
        if r.status_code not in (200, 204):
            print(f"[influx] write failed {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[influx] write error: {e}")


def _ns() -> int:
    return time.time_ns()


def _escape_tag(v: str) -> str:
    return v.replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _escape_field_str(v: str) -> str:
    return f'"{v.replace(chr(34), chr(92)+chr(34))}"'


def write_scan_event(barcode_id: str, component_name: str, box_label: str, cell_id: str):
    line = (
        f"scan_event,"
        f"barcode_id={_escape_tag(barcode_id)},"
        f"box={_escape_tag(box_label)},"
        f"cell={_escape_tag(cell_id)} "
        f"component_name={_escape_field_str(component_name)},"
        f"scanned=1i "
        f"{_ns()}"
    )
    _write(line)


def write_stock_change(barcode_id: str, component_name: str, delta: int, new_quantity: int, footprint_id: str):
    line = (
        f"stock_change,"
        f"barcode_id={_escape_tag(barcode_id)},"
        f"footprint_id={_escape_tag(footprint_id)} "
        f"component_name={_escape_field_str(component_name)},"
        f"delta={delta}i,"
        f"quantity={new_quantity}i "
        f"{_ns()}"
    )
    _write(line)
