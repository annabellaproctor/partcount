"""
Mouser Electronics API v1 — free tier, 1000 calls/day.
Auth: API key as query param on every request.
Endpoint: POST https://api.mouser.com/api/v1/search/keyword?apiKey={key}
Response: SearchResults.Parts[] with fields confirmed from live API.
"""
import httpx, os, logging
from typing import Optional

log = logging.getLogger("mouser")
BASE_URL = "https://api.mouser.com/api/v1"


def _key() -> str:
    """Read key lazily so env changes after import are picked up."""
    return os.getenv("MOUSER_API_KEY", "")


async def search(query: str, limit: int = 10) -> list[dict]:
    key = _key()
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{BASE_URL}/search/keyword?apiKey={key}",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
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
            errors = data.get("Errors") or []
            if errors:
                log.warning(f"Mouser errors: {errors}")
                return []
            parts = (data.get("SearchResults") or {}).get("Parts") or []
            results = [_simplify(p) for p in parts[:limit] if p]
            log.info(f"Mouser returned {len(results)} results for '{query}'")
            return results
    except Exception as e:
        log.warning(f"Mouser search failed: {e}")
        return []


async def get_part(mouser_pn: str) -> Optional[dict]:
    key = _key()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{BASE_URL}/search/partnumber?apiKey={key}",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json={"SearchByPartNumberRequest": {
                    "mouserPartNumber": mouser_pn,
                    "partSearchOptions": "None",
                }},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            parts = (data.get("SearchResults") or {}).get("Parts") or []
            return _simplify(parts[0]) if parts else None
    except Exception as e:
        log.warning(f"Mouser get_part failed: {e}")
        return None


def _simplify(p: dict) -> dict:
    """
    Map confirmed Mouser API v1 fields to internal format.
    Confirmed fields from live API response:
      Availability, DataSheetUrl, Description, FactoryStock,
      ImagePath, Manufacturer, ManufacturerPartNumber,
      MouserPartNumber, PriceBreaks[].Price, ProductDetailUrl,
      Min, Mult, ROHSStatus, Category
    """
    # Price — first break is qty=1 price, already formatted as "$1.23"
    price_breaks = p.get("PriceBreaks") or []
    unit_price = None
    for pb in price_breaks:
        raw = (pb.get("Price") or "").replace("$", "").replace(",", "").strip()
        try:
            unit_price = float(raw)
            break
        except (ValueError, TypeError):
            continue

    # ImagePath is a relative Mouser URL — make absolute
    image_url = p.get("ImagePath", "") or ""
    if image_url and not image_url.startswith("http"):
        image_url = f"https://www.mouser.com{image_url}"

    # ProductDetailUrl — same
    product_url = p.get("ProductDetailUrl", "") or ""
    if product_url and not product_url.startswith("http"):
        product_url = f"https://www.mouser.com{product_url}"

    return {
        "name": p.get("ManufacturerPartNumber") or p.get("Description", "")[:60],
        "digikey_pn": "",
        "mouser_pn": p.get("MouserPartNumber", ""),
        "mpn": p.get("ManufacturerPartNumber", ""),
        "manufacturer": p.get("Manufacturer", ""),
        "description": p.get("Description", ""),
        "datasheet_url": p.get("DataSheetUrl", "") or "",
        "image_url": image_url,
        "package": p.get("Category", ""),
        "value": "",
        "voltage_rating": None,
        "tolerance": "",
        "unit_price": unit_price,
        "product_url": product_url,
        "availability": p.get("Availability", ""),
        "lcsc_pn": "",
        "source": "mouser",
    }
