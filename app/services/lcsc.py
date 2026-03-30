"""
LCSC unofficial internal API — no key needed.
Reverse engineered, may break. Used as fallback.
"""
import httpx, logging, re
from typing import Optional

log = logging.getLogger("lcsc")

SEARCH_URL = "https://wmsc.lcsc.com/wmsc/search/global"
DETAIL_URL = "https://wmsc.lcsc.com/wmsc/product/detail"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.lcsc.com/",
    "Origin": "https://www.lcsc.com",
}


async def search(query: str, limit: int = 10) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10, headers=HEADERS) as client:
            r = await client.get(SEARCH_URL, params={
                "keyword": query,
                "currentPage": 1,
                "pageSize": limit,
            })
            if r.status_code != 200:
                log.warning(f"LCSC search {r.status_code}")
                return []
            data = r.json()
            products = data.get("result", {}).get("productSearchResultVO", {}).get("productList", []) or []
            return [_simplify(p) for p in products[:limit]]
    except Exception as e:
        log.warning(f"LCSC search failed: {e}")
        return []


async def get_part(lcsc_pn: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10, headers=HEADERS) as client:
            r = await client.get(DETAIL_URL, params={"productCode": lcsc_pn})
            if r.status_code != 200:
                return None
            data = r.json()
            p = data.get("result", {})
            if not p:
                return None
            return _simplify(p)
    except Exception as e:
        log.warning(f"LCSC get_part failed: {e}")
        return None


def _simplify(p: dict) -> dict:
    attrs = {a.get("paramNameEn", ""): a.get("paramValueEn", "") for a in p.get("paramVOList", [])}
    price_list = p.get("productArrangeList", []) or p.get("prices", []) or []
    unit_price = None
    if price_list:
        try:
            unit_price = float(price_list[0].get("productPrice", 0))
        except Exception:
            pass

    return {
        "name": p.get("productModel", p.get("productCode", "")),
        "digikey_pn": "",
        "mpn": p.get("productModel", ""),
        "manufacturer": p.get("brandNameEn", ""),
        "description": p.get("productIntroEn", p.get("productDescEn", "")),
        "datasheet_url": p.get("pdfUrl", ""),
        "image_url": p.get("productImageUrl", ""),
        "package": attrs.get("Package", "") or p.get("encapStandard", ""),
        "value": attrs.get("Resistance", "") or attrs.get("Capacitance", "") or attrs.get("Inductance", ""),
        "voltage_rating": _parse_float(attrs.get("Voltage - Rated", attrs.get("Voltage Rating", ""))),
        "tolerance": attrs.get("Tolerance", ""),
        "unit_price": unit_price,
        "lcsc_pn": p.get("productCode", ""),
        "source": "lcsc",
    }


def _parse_float(s: str):
    import re
    m = re.search(r"[\d.]+", str(s))
    return float(m.group()) if m else None
