"""
DigiKey Product Information V4 API — corrected field mapping from actual API response schema.
Client credentials (machine-to-machine), token cached in memory.
"""
import httpx, os, time, logging, re
from typing import Optional

log = logging.getLogger("digikey")

CLIENT_ID = os.getenv("DIGIKEY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DIGIKEY_CLIENT_SECRET", "")
TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
BASE_URL = "https://api.digikey.com/products/v4"

_token: Optional[str] = None
_token_expiry: float = 0


async def _get_token() -> str:
    global _token, _token_expiry
    if _token and time.time() < _token_expiry - 60:
        return _token
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET not configured")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        if r.status_code != 200:
            raise RuntimeError(f"DigiKey token error {r.status_code}: {r.text[:200]}")
        data = r.json()
        _token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 1800)
        log.info("DigiKey token acquired")
        return _token


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": CLIENT_ID,
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "USD",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def search(query: str, limit: int = 10) -> list[dict]:
    try:
        token = await _get_token()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{BASE_URL}/search/keyword",
                headers=_headers(token),
                json={
                    "Keywords": query,
                    "Limit": limit,
                    "Offset": 0,
                },
            )
            if r.status_code != 200:
                log.error(f"DigiKey search {r.status_code}: {r.text[:300]}")
                return []
            data = r.json()
            # v4 returns Products array
            products = data.get("Products", [])
            return [_simplify(p) for p in products if p]
    except Exception as e:
        log.error(f"DigiKey search failed: {e}")
        return []


async def get_part(digikey_pn: str) -> Optional[dict]:
    try:
        token = await _get_token()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{BASE_URL}/search/{digikey_pn}/productdetails",
                headers=_headers(token),
            )
            if r.status_code != 200:
                return None
            data = r.json()
            p = data.get("Product", data)
            return _simplify(p)
    except Exception as e:
        log.error(f"DigiKey get_part failed: {e}")
        return None


def _simplify(p: dict) -> dict:
    """Map v4 API response to our internal format."""
    # Description is nested
    desc_obj = p.get("Description", {}) or {}
    product_desc = desc_obj.get("ProductDescription", "") if isinstance(desc_obj, dict) else ""
    detailed_desc = desc_obj.get("DetailedDescription", "") if isinstance(desc_obj, dict) else ""

    # Manufacturer is nested
    mfr_obj = p.get("Manufacturer", {}) or {}
    manufacturer = mfr_obj.get("Name", "") if isinstance(mfr_obj, dict) else str(mfr_obj)

    # DigiKey PN is in ProductVariations[0]
    variations = p.get("ProductVariations", []) or []
    digikey_pn = ""
    package = ""
    if variations:
        v0 = variations[0]
        digikey_pn = v0.get("DigiKeyProductNumber", "")
        pkg = v0.get("PackageType", {}) or {}
        package = pkg.get("Name", "") if isinstance(pkg, dict) else ""

    # Parameters — list of {ParameterId, ParameterText, ValueId, ValueText}
    params = {}
    for param in (p.get("Parameters", []) or []):
        key = param.get("ParameterText", "")
        val = param.get("ValueText", "")
        if key and val:
            params[key.lower()] = val

    value = (
        params.get("resistance", "") or
        params.get("capacitance", "") or
        params.get("inductance", "") or
        params.get("current rating", "") or ""
    )
    voltage = _parse_float(params.get("voltage - rated", params.get("voltage rating", "")))
    tolerance = params.get("tolerance", "")
    if not package:
        package = params.get("package / case", params.get("supplier device package", ""))

    return {
        "name": product_desc or p.get("ManufacturerProductNumber", ""),
        "digikey_pn": digikey_pn or p.get("DigiKeyPartNumber", ""),
        "mpn": p.get("ManufacturerProductNumber", ""),
        "manufacturer": manufacturer,
        "description": detailed_desc or product_desc,
        "datasheet_url": p.get("DatasheetUrl", "") or "",
        "image_url": p.get("PhotoUrl", "") or "",
        "package": package,
        "value": value,
        "voltage_rating": voltage,
        "tolerance": tolerance,
        "unit_price": p.get("UnitPrice", None),
        "lcsc_pn": "",
        "source": "digikey",
    }


def _parse_float(s) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else None


async def debug_raw(query: str) -> dict:
    """Returns raw API response for debugging — call via /api/lookup/debug"""
    try:
        token = await _get_token()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{BASE_URL}/search/keyword",
                headers=_headers(token),
                json={"Keywords": query, "Limit": 2, "Offset": 0},
            )
            return {"status": r.status_code, "body": r.json()}
    except Exception as e:
        return {"error": str(e)}
