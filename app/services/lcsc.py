"""
LCSC API — all known endpoints are currently returning 500/dead as of March 2026.
This module is a stub that returns empty results.
Replacement distributors pending API approval: Mouser, TrustedParts.
When keys arrive, implement here and add to lookup_engine.py.
"""
import logging
log = logging.getLogger("lcsc")


async def search(query: str, limit: int = 10) -> list[dict]:
    log.debug("LCSC search called but API is currently unavailable")
    return []


async def get_part(lcsc_pn: str):
    return None


async def debug_raw(query: str) -> dict:
    return {
        "status": "unavailable",
        "message": "LCSC API endpoints returning 500 as of March 2026. Pending replacement with Mouser/TrustedParts.",
        "endpoints_tried": [
            "https://wwwapi.lcsc.com/v1/search/global-search → connection refused",
            "https://wmsc.lcsc.com/ftapi/api/v1/search/global-search → HTTP 500",
        ]
    }
