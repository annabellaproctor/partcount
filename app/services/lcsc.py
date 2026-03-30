"""
LCSC API — uses wwwapi.lcsc.com/v1 endpoint (confirmed working 2022-2025).
No key required. Unofficial but stable.
"""
import httpx, logging, re
from typing import Optional

log = logging.getLogger("lcsc")

SEARCH_URL = "https://wwwapi.lcsc.com/v1/search/global-search"
DETAIL_URL = "https://wwwapi.lcsc.com/v1/product/detail"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.lcsc.com/",
    "Accept": "application/json, text/plain, */*",
}


async def search(query: str, limit: int = 10) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=12, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(SEARCH_URL, params={"keyword": query})
            if r.status_code != 200:
                log.warning(f"LCSC search {r.status_code}: {r.text[:100]}")
                return []
            data = r.json()
            # response: {productSearchResultVO: {productList: [...]}}
            result = data.get("productSearchResultVO", {}) or {}
            products = result.get("productList", []) or []
            if not products:
                # alternate path
                products = data.get("productList", []) or []
            return [_simplify(p) for p in products[:limit] if p]
    except Exception as e:
        log.warning(f"LCSC search failed: {e}")
        return []


async def get_part(lcsc_pn: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=12, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(DETAIL_URL, params={"productCode": lcsc_pn})
            if r.status_code != 200:
                return None
            data = r.json()
            p = data.get("result", data)
            return _simplify(p) if p else None
    except Exception as e:
        log.warning(f"LCSC get_part failed: {e}")
        return None


async def debug_raw(query: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=12, headers=HEADERS) as client:
            r = await client.get(SEARCH_URL, params={"keyword": query})
            return {"status": r.status_code, "body": r.json()}
    except Exception as e:
        return {"error": str(e)}


def _simplify(p: dict) -> dict:
    # params may be a list of dicts or a nested dict
    params = {}
    for attr in (p.get("paramVOList", []) or p.get("attributes", []) or []):
        key = (attr.get("paramNameEn", "") or attr.get("name", "")).lower()
        val = attr.get("paramValueEn", "") or attr.get("value", "")
        if key and val:
            params[key] = val

    # price from price breaks
    price_list = p.get("productArrangeList", []) or p.get("prices", []) or []
    unit_price = None
    if price_list:
        try:
            unit_price = float(price_list[0].get("productPrice", 0) or 0) or None
        except Exception:
            pass

    return {
        "name": p.get("productModel", "") or p.get("productCode", "") or "",
        "digikey_pn": "",
        "mpn": p.get("productModel", "") or "",
        "manufacturer": p.get("brandNameEn", "") or p.get("brandName", "") or "",
        "description": p.get("productIntroEn", "") or p.get("productDescEn", "") or p.get("productDesc", "") or "",
        "datasheet_url": p.get("pdfUrl", "") or "",
        "image_url": p.get("productImageUrl", "") or p.get("images", [None])[0] if p.get("images") else "",
        "package": params.get("package", "") or p.get("encapStandard", "") or "",
        "value": (
            params.get("resistance", "") or params.get("capacitance", "") or
            params.get("inductance", "") or ""
        ),
        "voltage_rating": _parse_float(params.get("voltage - rated", params.get("voltage rating", ""))),
        "tolerance": params.get("tolerance", ""),
        "unit_price": unit_price,
        "lcsc_pn": p.get("productCode", "") or "",
        "source": "lcsc",
    }


def _parse_float(s):
    if not s:
        return None
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else None
