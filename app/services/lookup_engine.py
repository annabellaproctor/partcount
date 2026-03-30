"""
Lookup engine — queries DigiKey + LCSC in parallel, merges, scores, ranks.
Never caches results with empty primary fields.
Weights toward previously-used manufacturers.
"""
import asyncio, hashlib, json, os, re, uuid, logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.services import digikey, lcsc, mouser
from app.services.generic_icons import get_icon_data_url, infer_type

log = logging.getLogger("lookup_engine")

CACHE_TTL = timedelta(hours=24)
# Maximum characters of MPN/name used when building the Gemini merge cache key.
_MERGE_KEY_MPN_CHARS = 40
# Minimum Gemini confidence to inject the AI-merged result as the top result.
MERGE_CONFIDENCE_THRESHOLD = 0.7

# ---------------------------------------------------------------------------
# Common passive patterns for generic component detection
# ---------------------------------------------------------------------------

# Resistor: "10k", "4.7k", "100", "1M", "10R", optionally with package
_RESISTOR_VALUE_RE = re.compile(
    r"^(\d+\.?\d*)\s*([kKmMrRΩ]|ohm)?$"
)
# Capacitor: "100nF", "10uF", "1pF", "0.1uF"
_CAPACITOR_VALUE_RE = re.compile(
    r"^(\d+\.?\d*)\s*(p|n|u|µ|m)?f$",
    re.IGNORECASE,
)
# Common SMD/through-hole packages
_PASSIVE_PACKAGES = {
    "0201", "0402", "0603", "0805", "1206", "1210", "2512",
    "sod-123", "sod123", "do-35", "do35",
    "axial", "radial",
}

# Keywords that indicate a resistor or capacitor in MPN / name
_RESISTOR_KEYWORDS = {"resistor", "res", " r ", "ohm"}
_CAPACITOR_KEYWORDS = {"capacitor", "cap", "ceramic", "electrolytic", "mlcc", "tant"}


def _is_common_passive(query: str) -> Optional[str]:
    """
    Returns 'resistor', 'capacitor', or None.
    Detects queries like: '10k 0603', '100nF', '4.7k resistor', '10uF 1206 capacitor'
    """
    q = query.lower().strip()
    tokens = q.split()

    has_resistor_kw = any(kw in q for kw in _RESISTOR_KEYWORDS)
    has_capacitor_kw = any(kw in q for kw in _CAPACITOR_KEYWORDS)
    has_cap_value = any(_CAPACITOR_VALUE_RE.match(t) for t in tokens)
    has_res_value = any(_RESISTOR_VALUE_RE.match(t) for t in tokens)

    if has_cap_value or has_capacitor_kw:
        if has_cap_value:
            return "capacitor"
    if has_res_value or has_resistor_kw:
        if has_res_value:
            return "resistor"

    return None


async def suggest_generic(query: str, db: Optional[AsyncSession]) -> Optional[dict]:
    """
    If the query matches a common passive pattern, look for an existing
    generic component in the database (by value + package).  Returns the
    generic component record dict if found, or a stub suggestion dict if
    the pattern matches but no record exists yet.
    """
    passive_type = _is_common_passive(query)
    if not passive_type or not db:
        return None

    tokens = query.lower().split()
    # Extract value and package from tokens
    value_token = None
    package_token = None
    for t in tokens:
        if _RESISTOR_VALUE_RE.match(t) or _CAPACITOR_VALUE_RE.match(t):
            if value_token is None:
                value_token = t
        if t in _PASSIVE_PACKAGES:
            package_token = t

    if not value_token:
        return None

    # Look for an existing generic component matching value + optional package
    try:
        sql = text(
            "SELECT id, barcode_id, name, value, package FROM components "
            "WHERE is_generic = TRUE AND LOWER(value) = :val"
            + (" AND LOWER(package) = :pkg" if package_token else "")
            + " LIMIT 1"
        )
        params: dict = {"val": value_token}
        if package_token:
            params["pkg"] = package_token

        result = await db.execute(sql, params)
        row = result.fetchone()
        if row:
            return {
                "found": True,
                "component_id": row.id,
                "barcode_id": row.barcode_id,
                "name": row.name,
                "value": row.value,
                "package": row.package,
                "passive_type": passive_type,
            }
    except Exception as exc:
        log.warning(f"suggest_generic db query failed: {exc}")

    # No existing generic — return a creation suggestion
    label_parts = [value_token.upper()]
    if package_token:
        label_parts.append(package_token.upper())
    label_parts.append(passive_type.capitalize())

    return {
        "found": False,
        "suggested_name": " ".join(label_parts),
        "suggested_value": value_token,
        "suggested_package": package_token or "",
        "passive_type": passive_type,
    }


def _is_valid_result(r: dict) -> bool:
    """Returns False if any primary field is empty/unknown — never cache these."""
    bad = {"", "unknown", "n/a", "none", "null"}
    name = (r.get("name") or "").strip().lower()
    mfr = (r.get("manufacturer") or "").strip().lower()
    mpn = (r.get("mpn") or "").strip().lower()
    desc = (r.get("description") or "").strip().lower()
    # need at least name + (manufacturer OR mpn)
    if name in bad:
        return False
    if mfr in bad and mpn in bad:
        return False
    return True


def _enrich(r: dict) -> dict:
    """Add inferred type, generic icon if no image, confidence score placeholder."""
    r = dict(r)
    # infer type from name + description
    if not r.get("inferred_type"):
        r["inferred_type"] = infer_type((r.get("name", "") + " " + r.get("description", "")))
    # add generic icon if no real image
    if not r.get("image_url"):
        r["generic_icon"] = get_icon_data_url(r["inferred_type"])
    else:
        r["generic_icon"] = ""
    return r


def _score(r: dict, query: str, preferred_manufacturers: set) -> float:
    """
    Relevance score 0.0–1.0.
    Factors:
    - Exact MPN match: +0.5
    - Exact name match: +0.3
    - MPN starts with query: +0.2
    - Name contains query: +0.15
    - Has real image: +0.1
    - Has datasheet: +0.05
    - Known/preferred manufacturer: +0.15
    - Major known brand (Ti, ST, Espressif, etc): +0.1
    - Source = digikey (generally higher quality data): +0.05
    - Generic/unknown manufacturer: -0.2
    """
    score = 0.0
    q = query.lower().strip()
    name = (r.get("name") or "").lower()
    mpn = (r.get("mpn") or "").lower()
    mfr = (r.get("manufacturer") or "").lower()

    # exact matches
    if mpn == q: score += 0.5
    elif mpn.startswith(q): score += 0.2
    if name == q: score += 0.3
    elif q in name: score += 0.15

    # quality signals
    if r.get("image_url"): score += 0.1
    if r.get("datasheet_url"): score += 0.05

    # manufacturer signals
    MAJOR_BRANDS = {
        "espressif", "texas instruments", "ti", "stmicroelectronics", "st",
        "microchip", "nordic semiconductor", "nxp", "analog devices", "infineon",
        "on semiconductor", "vishay", "yageo", "murata", "tdk", "samsung",
        "wurth", "molex", "jst", "panasonic", "rohm", "diodes",
    }
    if mfr in preferred_manufacturers:
        score += 0.15
    if any(b in mfr for b in MAJOR_BRANDS):
        score += 0.1
    if mfr in ("", "generic", "unknown", "n/a"):
        score -= 0.2

    # data source
    if r.get("source") == "digikey":
        score += 0.05

    return max(0.0, min(1.0, score))


async def _get_preferred_manufacturers(db: AsyncSession) -> set:
    """Returns set of manufacturer names from existing components."""
    try:
        result = await db.execute(text(
            "SELECT DISTINCT m.name FROM manufacturers m "
            "JOIN components c ON c.manufacturer_id = m.id "
            "WHERE c.manufacturer_id IS NOT NULL LIMIT 50"
        ))
        rows = result.fetchall()
        return {r[0].lower() for r in rows if r[0]}
    except Exception:
        return set()


async def search(
    query: str,
    source: str = "auto",
    force: bool = False,
    db: Optional[AsyncSession] = None,
    limit: int = 12,
) -> dict:
    cache_key_dk = f"digikey:{query.lower().strip()}"
    cache_key_lc = f"lcsc:{query.lower().strip()}"
    cache_key_auto = f"auto:{query.lower().strip()}"

    preferred_mfrs: set = set()
    if db:
        preferred_mfrs = await _get_preferred_manufacturers(db)

    # clear cache on force
    if force and db:
        try:
            for key in [cache_key_dk, cache_key_lc, cache_key_auto]:
                await db.execute(
                    text("DELETE FROM component_lookups WHERE query = :q"),
                    {"q": key}
                )
        except Exception:
            pass

    # try cache for each source unless force
    dk_results, lc_results = [], []
    dk_cached, lc_cached = False, False

    if not force and db and source in ("auto", "digikey"):
        try:
            r = await db.execute(
                text("SELECT result_json, fetched_at FROM component_lookups WHERE query = :q ORDER BY fetched_at DESC LIMIT 1"),
                {"q": cache_key_dk}
            )
            row = r.fetchone()
            if row and row.fetched_at and (datetime.utcnow() - row.fetched_at) < CACHE_TTL:
                dk_results = json.loads(row.result_json)
                dk_cached = True
        except Exception:
            pass

    if not force and db and source in ("auto", "lcsc"):
        try:
            r = await db.execute(
                text("SELECT result_json, fetched_at FROM component_lookups WHERE query = :q ORDER BY fetched_at DESC LIMIT 1"),
                {"q": cache_key_lc}
            )
            row = r.fetchone()
            if row and row.fetched_at and (datetime.utcnow() - row.fetched_at) < CACHE_TTL:
                lc_results = json.loads(row.result_json)
                lc_cached = True
        except Exception:
            pass

    # fetch uncached sources in parallel
    fetch_tasks = []
    fetch_labels = []

    if not dk_cached and source in ("auto", "digikey"):
        fetch_tasks.append(digikey.search(query, limit=10, db=db))
        fetch_labels.append("digikey")

    if not lc_cached and source in ("auto", "lcsc"):
        fetch_tasks.append(lcsc.search(query, limit=10))
        fetch_labels.append("lcsc")

    if fetch_tasks:
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        for label, res in zip(fetch_labels, results):
            if isinstance(res, Exception):
                log.warning(f"{label} search error: {res}")
                continue
            valid = [r for r in (res or []) if _is_valid_result(r)]
            if label == "digikey":
                dk_results = valid
            else:
                lc_results = valid

            # cache valid results — upsert so repeated searches replace stale data
            if valid and db:
                cache_key = cache_key_dk if label == "digikey" else cache_key_lc
                try:
                    await db.execute(
                        text(
                            "INSERT INTO component_lookups "
                            "(id, query, source, result_json, full_text, fetched_at) "
                            "VALUES (:id, :q, :s, :r, :ft, :t) "
                            "ON CONFLICT (query) DO UPDATE SET "
                            "result_json = EXCLUDED.result_json, "
                            "full_text = EXCLUDED.full_text, "
                            "fetched_at = EXCLUDED.fetched_at"
                        ),
                        {
                            "id": str(uuid.uuid4()), "q": cache_key, "s": label,
                            "r": json.dumps(valid), "ft": json.dumps(valid),
                            "t": datetime.utcnow(),
                        },
                    )
                except Exception:
                    pass

    # merge — deduplicate by MPN
    seen_mpns = set()
    merged = []
    for r in dk_results + lc_results:
        mpn = (r.get("mpn") or "").strip().upper()
        key = mpn or r.get("name", "")[:30]
        if key and key in seen_mpns:
            # if same MPN from both sources, keep digikey but add lcsc_pn
            continue
        seen_mpns.add(key)
        merged.append(_enrich(r))

    # score and sort
    for r in merged:
        r["_score"] = _score(r, query, preferred_mfrs)
    merged.sort(key=lambda r: r["_score"], reverse=True)

    # high confidence flag: score >= 0.6 + has image + has datasheet
    for r in merged:
        r["high_confidence"] = (
            r["_score"] >= 0.6 and
            bool(r.get("image_url") or r.get("generic_icon")) and
            bool(r.get("datasheet_url") or r.get("mpn"))
        )

    # If we got results from both sources, use Gemini to merge the top result
    # for maximum data quality on the best match
    final_source = "merged" if (dk_results and lc_results) else ("digikey" if dk_results else "lcsc")
    
    if dk_results and lc_results and merged and os.getenv("GEMINI_API_KEY"):
        # Guard: skip merge if top results are clearly different components.
        # Compare inferred types — if all top-3 share the same type, merge is meaningful.
        top = merged[:3]
        top_types = {r.get("inferred_type", "default") for r in top}
        top_mpns = sorted(
            (r.get("mpn") or r.get("name", ""))[:_MERGE_KEY_MPN_CHARS]
            for r in top
            if r.get("mpn") or r.get("name")
        )
        if len(top_types) > 1 and "default" not in top_types:
            log.debug(f"Skipping Gemini merge — mixed component types: {top_types}")
        else:
            # Build a stable cache key from the sorted MPNs of the top candidates.
            merge_key = "gemini_merge:" + hashlib.md5(
                json.dumps(top_mpns).encode()
            ).hexdigest()

            merged_top = None

            # Check cache first
            if db:
                try:
                    r = await db.execute(
                        text(
                            "SELECT result_json, fetched_at FROM component_lookups "
                            "WHERE query = :q ORDER BY fetched_at DESC LIMIT 1"
                        ),
                        {"q": merge_key},
                    )
                    row = r.fetchone()
                    if row and row.fetched_at and (datetime.utcnow() - row.fetched_at) < CACHE_TTL:
                        merged_top = json.loads(row.result_json)
                        log.debug("Gemini merge result served from cache")
                except Exception:
                    pass

            if merged_top is None:
                try:
                    from app.routers.ai_parse import merge_results, MergeRequest
                    merged_top = await merge_results(MergeRequest(results=top, query=query))
                    # Cache the merge result
                    if merged_top and db:
                        try:
                            await db.execute(
                                text(
                                    "INSERT INTO component_lookups "
                                    "(id, query, source, result_json, full_text, fetched_at) "
                                    "VALUES (:id, :q, :s, :r, :ft, :t) "
                                    "ON CONFLICT (query) DO UPDATE SET "
                                    "result_json = EXCLUDED.result_json, "
                                    "full_text = EXCLUDED.full_text, "
                                    "fetched_at = EXCLUDED.fetched_at"
                                ),
                                {
                                    "id": str(uuid.uuid4()), "q": merge_key,
                                    "s": "gemini_merged",
                                    "r": json.dumps(merged_top),
                                    "ft": json.dumps(merged_top),
                                    "t": datetime.utcnow(),
                                },
                            )
                        except Exception:
                            pass
                except Exception as e:
                    log.warning(f"Gemini merge failed (non-fatal): {e}")

            if merged_top and merged_top.get("confidence", 0) > MERGE_CONFIDENCE_THRESHOLD:
                merged_top["_ai_merged"] = True
                merged_top["source"] = "gemini_merged"
                merged_top = _enrich(merged_top)
                merged_top["_score"] = 1.0
                merged_top["high_confidence"] = True
                merged = [merged_top] + [r for r in merged if r.get("mpn") != merged_top.get("mpn")]

    return {
        "results": merged[:limit],
        "source": final_source,
        "cached": dk_cached or lc_cached,
        "dk_count": len(dk_results),
        "lc_count": len(lc_results),
        "generic_suggestion": await suggest_generic(query, db),
    }
