"""
Mouser Electronics API — free tier, 1000 calls/day.
Register at: https://www.mouser.com/api-hub/
Set MOUSER_API_KEY in .env when key arrives.

Mouser API docs: https://api.mouser.com/api/docs/index
Endpoint: POST https://api.mouser.com/api/v1/search/keyword
"""
import httpx, os, logging, re
from typing import Optional

log = logging.getLogger("mouser")
API_KEY = os.getenv("MOUSER_API_KEY", "")
BASE_URL = "https://api.mouser.com/api/v1"


async def search(query: str, limit: int = 10) -> list[dict]:
    if not API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{BASE_URL}/search/keyword?apiKey={API_KEY}",
                json={
                    "SearchByKeywordRequest": {
                        "keyword": query,
                        "records": limit,
                        "startingRecord": 0,
                        "searchOptions": "None",
                        "searchWithYourSignUpLanguage": "false",
                    }
                },
            )
            if r.status_code != 200:
                log.warning(f"Mouser search {r.status_code}: {r.text[:200]}")
                return []
            data = r.json()
            parts = (data.get("SearchResults") or {}).get("Parts") or []
            return [_simplify(p) for p in parts[:limit] if p]
    except Exception as e:
        log.warning(f"Mouser search failed: {e}")
        return []


def _simplify(p: dict) -> dict:
    # Mouser price breaks
    price_breaks = p.get("PriceBreaks") or []
    unit_price = None
    if price_breaks:
        try:
            unit_price = float(
                price_breaks[0].get("Price", "0").replace("$", "").replace(",", "") or 0
            ) or None
        except Exception:
            pass

    return {
        "name": p.get("ManufacturerPartNumber") or p.get("Description", ""),
        "digikey_pn": "",
        "mpn": p.get("ManufacturerPartNumber", ""),
        "manufacturer": p.get("Manufacturer", ""),
        "description": p.get("Description", ""),
        "datasheet_url": p.get("DataSheetUrl", ""),
        "image_url": p.get("ImagePath", ""),
        "package": "",
        "value": "",
        "voltage_rating": None,
        "tolerance": "",
        "unit_price": unit_price,
        "product_url": p.get("ProductDetailUrl", ""),
        "lcsc_pn": "",
        "mouser_pn": p.get("MouserPartNumber", ""),
        "source": "mouser",
    }


async def get_part(mouser_pn: str) -> Optional[dict]:
    if not API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{BASE_URL}/search/partnumber?apiKey={API_KEY}",
                json={"SearchByPartNumberRequest": {"mouserPartNumber": mouser_pn, "partSearchOptions": "None"}},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            parts = (data.get("SearchResults") or {}).get("Parts") or []
            return _simplify(parts[0]) if parts else None
    except Exception as e:
        log.warning(f"Mouser get_part failed: {e}")
        return None
