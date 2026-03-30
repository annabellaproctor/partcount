"""
DigiKey Product Information V4 API
Client credentials flow — no user OAuth needed.
Token cached in memory, refreshed on expiry.
"""
import httpx, os, time, logging
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
    if _token and time.time() < _token_expiry - 30:
        return _token
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET not set")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        r.raise_for_status()
        data = r.json()
        _token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 1800)
        log.info("DigiKey token refreshed")
        return _token


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": CLIENT_ID,
        "X-DIGIKEY-Locale-Site": "US",
        "X-DIGIKEY-Locale-Language": "en",
        "X-DIGIKEY-Locale-Currency": "USD",
        "Content-Type": "application/json",
    }


async def search(query: str, limit: int = 10) -> list[dict]:
    """Keyword search — returns simplified component list"""
    try:
        token = await _get_token()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{BASE_URL}/search/keyword",
                headers=_headers(token),
                json={
                    "Keywords": query,
                    "Limit": limit,
                    "Offset": 0,
                    "FilterOptionsRequest": {"MinimumQuantity": 0},
                },
            )
            if r.status_code != 200:
                log.error(f"DigiKey search error {r.status_code}: {r.text[:200]}")
                return []
            data = r.json()
            products = data.get("Products", [])
            return [_simplify(p) for p in products]
    except Exception as e:
        log.error(f"DigiKey search failed: {e}")
        return []


async def get_part(digikey_pn: str) -> Optional[dict]:
    """Get full details for a specific DigiKey part number"""
    try:
        token = await _get_token()
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{BASE_URL}/search/{digikey_pn}/productdetails",
                headers=_headers(token),
            )
            if r.status_code != 200:
                return None
            return _simplify(r.json().get("Product", {}))
    except Exception as e:
        log.error(f"DigiKey get_part failed: {e}")
        return None


def _simplify(p: dict) -> dict:
    """Flatten a DigiKey product to our fields"""
    params = {param.get("ParameterId", 0): param.get("ValueText", "") for param in p.get("Parameters", [])}
    # Common parameter IDs: 1=Resistance, 2=Capacitance, 25=Voltage Rating, 69=Tolerance, 16=Package/Case
    return {
        "name": p.get("ProductDescription", ""),
        "digikey_pn": p.get("DigiKeyPartNumber", ""),
        "mpn": p.get("ManufacturerPartNumber", ""),
        "manufacturer": p.get("Manufacturer", {}).get("Name", "") if isinstance(p.get("Manufacturer"), dict) else p.get("Manufacturer", ""),
        "description": p.get("DetailedDescription", p.get("ProductDescription", "")),
        "datasheet_url": p.get("PrimaryDatasheet", ""),
        "image_url": p.get("PrimaryPhoto", ""),
        "package": params.get(16, "") or p.get("PackageType", {}).get("Name", "") if isinstance(p.get("PackageType"), dict) else "",
        "value": params.get(1, "") or params.get(2, ""),
        "voltage_rating": _parse_float(params.get(25, "")),
        "tolerance": params.get(69, ""),
        "unit_price": p.get("UnitPrice", None),
        "lcsc_pn": "",
        "source": "digikey",
    }


def _parse_float(s: str) -> Optional[float]:
    import re
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else None
