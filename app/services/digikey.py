"""
DigiKey Product Information V4 API.
Field mapping verified against actual API response.
"""
import httpx, os, time, logging, re
from typing import Optional

log = logging.getLogger("digikey")

CLIENT_ID = os.getenv("DIGIKEY_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DIGIKEY_CLIENT_SECRET", "")
TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
BASE_URL = "https://api.digikey.com/products/v4"

# Refresh the token this many seconds before it actually expires to avoid
# using a token that expires mid-request.
TOKEN_EXPIRY_BUFFER = 60

# In-memory cache — used as fast path; DB is authoritative across restarts.
_token: Optional[str] = None
_token_expiry: float = 0

_SETTING_TOKEN = "digikey_token"
_SETTING_EXPIRY = "digikey_token_expiry"


async def _load_token_from_db(db) -> None:
    """Populate in-memory cache from system_settings if a valid token exists."""
    global _token, _token_expiry
    try:
        from sqlalchemy import text
        r = await db.execute(
            text("SELECT key, value FROM system_settings WHERE key IN (:tk, :te)"),
            {"tk": _SETTING_TOKEN, "te": _SETTING_EXPIRY},
        )
        rows = {row.key: row.value for row in r.fetchall()}
        stored_token = rows.get(_SETTING_TOKEN)
        stored_expiry = float(rows.get(_SETTING_EXPIRY, 0))
        if stored_token and stored_expiry and time.time() < stored_expiry - TOKEN_EXPIRY_BUFFER:
            _token = stored_token
            _token_expiry = stored_expiry
    except Exception as e:
        log.debug(f"Could not load DigiKey token from DB: {e}")


async def _save_token_to_db(db) -> None:
    """Persist the current in-memory token to system_settings."""
    if not _token:
        return
    try:
        from sqlalchemy import text
        for key, val in ((_SETTING_TOKEN, _token), (_SETTING_EXPIRY, str(_token_expiry))):
            await db.execute(
                text(
                    "INSERT INTO system_settings (key, value, updated_at) VALUES (:k, :v, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
                ),
                {"k": key, "v": val},
            )
    except Exception as e:
        log.debug(f"Could not save DigiKey token to DB: {e}")


async def _get_token(db=None) -> str:
    global _token, _token_expiry
    # Fast path: valid in-memory token
    if _token and time.time() < _token_expiry - TOKEN_EXPIRY_BUFFER:
        return _token
    # Try restoring from DB before doing a network round-trip
    if db is not None:
        await _load_token_from_db(db)
        if _token and time.time() < _token_expiry - TOKEN_EXPIRY_BUFFER:
            return _token
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET not configured")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        r.raise_for_status()
        data = r.json()
        _token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 1800)
        if db is not None:
            await _save_token_to_db(db)
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


async def search(query: str, limit: int = 10, db=None) -> list[dict]:
    try:
        token = await _get_token(db)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{BASE_URL}/search/keyword",
                headers=_headers(token),
                json={"Keywords": query, "Limit": limit, "Offset": 0},
            )
            if r.status_code != 200:
                log.error(f"DigiKey search {r.status_code}: {r.text[:300]}")
                return []
            data = r.json()
            return [_simplify(p) for p in (data.get("Products") or []) if p]
    except Exception as e:
        log.error(f"DigiKey search failed: {e}")
        return []


async def get_part(digikey_pn: str, db=None) -> Optional[dict]:
    try:
        token = await _get_token(db)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{BASE_URL}/search/{digikey_pn}/productdetails",
                headers=_headers(token),
            )
            if r.status_code != 200:
                return None
            data = r.json()
            return _simplify(data.get("Product") or data)
    except Exception as e:
        log.error(f"DigiKey get_part failed: {e}")
        return None


async def debug_raw(query: str, db=None) -> dict:
    try:
        token = await _get_token(db)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{BASE_URL}/search/keyword",
                headers=_headers(token),
                json={"Keywords": query, "Limit": 2, "Offset": 0},
            )
            return {"status": r.status_code, "body": r.json()}
    except Exception as e:
        return {"error": str(e)}


def _best_variation(variations: list) -> dict:
    """Pick cut tape (qty=1) over tape & reel. Fall back to first."""
    if not variations:
        return {}
    # prefer cut tape
    for v in variations:
        pkg = (v.get("PackageType") or {}).get("Name", "")
        if "cut" in pkg.lower():
            return v
    # prefer lowest MOQ
    try:
        return min(variations, key=lambda v: v.get("MinimumOrderQuantity", 9999))
    except Exception:
        return variations[0]


def _unit_price_from_variation(v: dict) -> Optional[float]:
    pricing = v.get("StandardPricing") or []
    if not pricing:
        return None
    # qty=1 break if available
    for p in pricing:
        if p.get("BreakQuantity", 999) <= 1:
            return float(p.get("UnitPrice", 0)) or None
    # otherwise lowest break
    try:
        return float(min(pricing, key=lambda p: p.get("BreakQuantity", 9999)).get("UnitPrice", 0)) or None
    except Exception:
        return None


def _simplify(p: dict) -> dict:
    desc_obj = p.get("Description") or {}
    product_desc = desc_obj.get("ProductDescription", "") if isinstance(desc_obj, dict) else ""
    detailed_desc = desc_obj.get("DetailedDescription", "") if isinstance(desc_obj, dict) else ""

    mfr_obj = p.get("Manufacturer") or {}
    manufacturer = mfr_obj.get("Name", "") if isinstance(mfr_obj, dict) else str(mfr_obj or "")

    mpn = p.get("ManufacturerProductNumber", "") or ""

    variations = p.get("ProductVariations") or []
    best_var = _best_variation(variations)
    digikey_pn = best_var.get("DigiKeyProductNumber", "") or p.get("DigiKeyPartNumber", "") or ""
    pkg_obj = best_var.get("PackageType") or {}
    package = pkg_obj.get("Name", "") if isinstance(pkg_obj, dict) else ""

    # unit price: top-level first (keyword search), then from variation
    unit_price = p.get("UnitPrice") or _unit_price_from_variation(best_var)

    # Parameters — present in detail calls, not keyword search
    params = {}
    for param in (p.get("Parameters") or []):
        key = (param.get("ParameterText", "") or "").lower()
        val = param.get("ValueText", "") or ""
        if key and val:
            params[key] = val

    value = (
        params.get("resistance", "") or
        params.get("capacitance", "") or
        params.get("inductance", "") or
        params.get("current - supply", "") or ""
    )
    voltage = _parse_float(params.get("voltage - rated", params.get("voltage rating", "")))
    tolerance = params.get("tolerance", "")
    if not package:
        package = params.get("package / case", params.get("supplier device package", ""))

    return {
        "name": product_desc or mpn or "",
        "digikey_pn": digikey_pn,
        "mpn": mpn,
        "manufacturer": manufacturer,
        "description": detailed_desc or product_desc or "",
        "datasheet_url": p.get("DatasheetUrl", "") or "",
        "image_url": p.get("PhotoUrl", "") or "",
        "package": package or "",
        "value": value or "",
        "voltage_rating": voltage,
        "tolerance": tolerance or "",
        "unit_price": unit_price,
        "product_url": p.get("ProductUrl", "") or "",
        "lcsc_pn": "",
        "source": "digikey",
    }


def _parse_float(s) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else None
