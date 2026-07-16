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
ITEM_CACHE_TTL = timedelta(days=45)
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


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]{2,}", (text or "").lower()) if t]


def _catalog_policy(source: str, item: dict) -> dict:
    """
    Conservative storage policy:
    - mouser: keep minimal index fields only (no full payload/description/image/product URL)
    - others: keep richer fields to improve local search quality
    """
    src = (source or "").lower()
    base = {
        "source": src,
        "source_item_id": (item.get("source_item_id") or item.get("mpn") or item.get("name") or "")[:120],
        "mpn": (item.get("mpn") or "")[:120],
        "manufacturer": (item.get("manufacturer") or "")[:160],
        "name": (item.get("name") or "")[:300],
        "package": (item.get("package") or "")[:120],
        "datasheet_url": (item.get("datasheet_url") or "")[:500],
        "importance_score": float(item.get("_score") or 0.0),
    }

    if src == "mouser":
        # Restrictive mode until Mouser policy review is explicitly confirmed.
        base.update({
            "description": "",
            "image_url": "",
            "product_url": "",
            "payload_json": "",
            "retention_policy": "minimal",
        })
    else:
        safe_payload = {
            "mpn": item.get("mpn"),
            "manufacturer": item.get("manufacturer"),
            "name": item.get("name"),
            "value": item.get("value"),
            "unit": item.get("unit"),
            "package": item.get("package"),
            "datasheet_url": item.get("datasheet_url"),
            "image_url": item.get("image_url"),
            "source": item.get("source") or src,
        }
        base.update({
            "description": (item.get("description") or "")[:2000],
            "image_url": (item.get("image_url") or "")[:600],
            "product_url": (item.get("url") or item.get("product_url") or "")[:600],
            "payload_json": json.dumps(safe_payload, ensure_ascii=False),
            "retention_policy": "full",
        })
    return base


def _search_text_for_item(policy_item: dict) -> str:
    parts = [
        policy_item.get("source", ""),
        policy_item.get("source_item_id", ""),
        policy_item.get("mpn", ""),
        policy_item.get("manufacturer", ""),
        policy_item.get("name", ""),
        policy_item.get("description", ""),
        policy_item.get("package", ""),
    ]
    return " ".join(p for p in parts if p).strip()[:4000]


async def _cache_catalog_items(db: Optional[AsyncSession], source: str, items: list[dict]):
    if not db or not items:
        return
    now = datetime.utcnow()
    for raw in items:
        try:
            item = _catalog_policy(source, raw)
            key_parts = [item.get("source", ""), item.get("source_item_id", ""), item.get("mpn", ""), item.get("manufacturer", ""), item.get("name", "")]
            item_key = hashlib.sha1("|".join(key_parts).encode("utf-8", "ignore")).hexdigest()
            search_text = _search_text_for_item(item)
            await db.execute(
                text(
                    "INSERT INTO external_catalog_items "
                    "(id, item_key, source, source_item_id, mpn, manufacturer, name, description, package, datasheet_url, image_url, product_url, search_text, payload_json, retention_policy, importance_score, first_seen, last_seen, seen_count) "
                    "VALUES (:id, :item_key, :source, :source_item_id, :mpn, :manufacturer, :name, :description, :package, :datasheet_url, :image_url, :product_url, :search_text, :payload_json, :retention_policy, :importance_score, :first_seen, :last_seen, 1) "
                    "ON CONFLICT (item_key) DO UPDATE SET "
                    "mpn = COALESCE(NULLIF(EXCLUDED.mpn, ''), external_catalog_items.mpn), "
                    "manufacturer = COALESCE(NULLIF(EXCLUDED.manufacturer, ''), external_catalog_items.manufacturer), "
                    "name = COALESCE(NULLIF(EXCLUDED.name, ''), external_catalog_items.name), "
                    "description = CASE WHEN external_catalog_items.retention_policy = 'minimal' THEN external_catalog_items.description ELSE COALESCE(NULLIF(EXCLUDED.description, ''), external_catalog_items.description) END, "
                    "package = COALESCE(NULLIF(EXCLUDED.package, ''), external_catalog_items.package), "
                    "datasheet_url = COALESCE(NULLIF(EXCLUDED.datasheet_url, ''), external_catalog_items.datasheet_url), "
                    "image_url = CASE WHEN external_catalog_items.retention_policy = 'minimal' THEN external_catalog_items.image_url ELSE COALESCE(NULLIF(EXCLUDED.image_url, ''), external_catalog_items.image_url) END, "
                    "product_url = CASE WHEN external_catalog_items.retention_policy = 'minimal' THEN external_catalog_items.product_url ELSE COALESCE(NULLIF(EXCLUDED.product_url, ''), external_catalog_items.product_url) END, "
                    "search_text = COALESCE(NULLIF(EXCLUDED.search_text, ''), external_catalog_items.search_text), "
                    "payload_json = CASE WHEN external_catalog_items.retention_policy = 'minimal' THEN external_catalog_items.payload_json ELSE COALESCE(NULLIF(EXCLUDED.payload_json, ''), external_catalog_items.payload_json) END, "
                    "importance_score = GREATEST(COALESCE(external_catalog_items.importance_score, 0), COALESCE(EXCLUDED.importance_score, 0)), "
                    "retention_policy = CASE WHEN external_catalog_items.retention_policy = 'minimal' THEN 'minimal' ELSE EXCLUDED.retention_policy END, "
                    "last_seen = EXCLUDED.last_seen, "
                    "seen_count = external_catalog_items.seen_count + 1"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "item_key": item_key,
                    "source": item.get("source") or source,
                    "source_item_id": item.get("source_item_id") or "",
                    "mpn": item.get("mpn") or "",
                    "manufacturer": item.get("manufacturer") or "",
                    "name": item.get("name") or "",
                    "description": item.get("description") or "",
                    "package": item.get("package") or "",
                    "datasheet_url": item.get("datasheet_url") or "",
                    "image_url": item.get("image_url") or "",
                    "product_url": item.get("product_url") or "",
                    "search_text": search_text,
                    "payload_json": item.get("payload_json") or "",
                    "retention_policy": item.get("retention_policy") or "full",
                    "importance_score": float(item.get("importance_score") or 0.0),
                    "first_seen": now,
                    "last_seen": now,
                },
            )
        except Exception as exc:
            log.debug(f"catalog cache upsert skipped: {exc}")


def _cached_item_score(row: dict, query: str, preferred_mfrs: set) -> float:
    q_tokens = set(_tokenize(query))
    name_tokens = set(_tokenize(row.get("name") or ""))
    text_tokens = set(_tokenize(row.get("search_text") or ""))
    overlap = len(q_tokens.intersection(text_tokens))
    if not q_tokens:
        overlap_ratio = 0
    else:
        overlap_ratio = overlap / max(1, len(q_tokens))

    score = 0.25 + overlap_ratio * 0.55
    if (row.get("mpn") or "").lower() == query.lower().strip():
        score += 0.2
    if any(t in name_tokens for t in q_tokens):
        score += 0.08
    if (row.get("manufacturer") or "").lower() in preferred_mfrs:
        score += 0.08
    score += min(0.1, (row.get("seen_count") or 0) * 0.01)
    age_hours = ((datetime.utcnow() - (row.get("last_seen") or datetime.utcnow())).total_seconds() / 3600.0)
    score += max(-0.08, 0.08 - age_hours / (24 * 14))
    score += min(0.08, float(row.get("importance_score") or 0.0) * 0.15)
    return max(0.0, min(1.0, score))


async def _search_cached_catalog(db: Optional[AsyncSession], query: str, limit: int, preferred_mfrs: set) -> list[dict]:
    if not db or not query.strip():
        return []
    tokens = _tokenize(query)[:6]
    if not tokens:
        return []
    params = {"ttl_cutoff": datetime.utcnow() - ITEM_CACHE_TTL}
    clauses = []
    for i, token in enumerate(tokens):
        key = f"t{i}"
        params[key] = f"%{token}%"
        clauses.append(f"search_text ILIKE :{key}")

    sql = (
        "SELECT source, source_item_id, mpn, manufacturer, name, description, package, datasheet_url, image_url, product_url, search_text, retention_policy, importance_score, seen_count, last_seen "
        "FROM external_catalog_items "
        "WHERE last_seen >= :ttl_cutoff AND (" + " OR ".join(clauses) + ") "
        "ORDER BY last_seen DESC, seen_count DESC "
        "LIMIT :take"
    )
    params["take"] = max(limit * 6, 60)
    rows = (await db.execute(text(sql), params)).mappings().all()
    out = []
    for row in rows:
        d = dict(row)
        candidate = {
            "name": d.get("name") or d.get("mpn") or d.get("source_item_id") or "Unknown",
            "manufacturer": d.get("manufacturer") or "",
            "mpn": d.get("mpn") or "",
            "package": d.get("package") or "",
            "description": d.get("description") or "",
            "datasheet_url": d.get("datasheet_url") or "",
            "image_url": d.get("image_url") or "",
            "url": d.get("product_url") or "",
            "source": f"catalog_cache:{d.get('source') or 'unknown'}",
            "retention_policy": d.get("retention_policy") or "full",
            "_catalog_cached": True,
        }
        candidate["inferred_type"] = infer_type((candidate.get("name", "") + " " + candidate.get("description", "")))
        candidate["generic_icon"] = get_icon_data_url(candidate["inferred_type"]) if not candidate.get("image_url") else ""
        candidate["_score"] = _cached_item_score(d, query, preferred_mfrs)
        candidate["high_confidence"] = candidate["_score"] >= 0.64 and bool(candidate.get("mpn") or candidate.get("datasheet_url"))
        out.append(candidate)

    out.sort(key=lambda x: x.get("_score", 0), reverse=True)
    # de-dupe by mpn/name
    seen = set()
    uniq = []
    for item in out:
        key = (item.get("mpn") or "").strip().upper() or (item.get("name") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(item)
        if len(uniq) >= limit:
            break
    return uniq


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
    cache_key_mo = f"mouser:{query.lower().strip()}"
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
    dk_results, mouser_results, lc_results = [], [], []
    dk_cached, mouser_cached, lc_cached = False, False, False

    cached_ranked_results = []
    if not force and db:
        try:
            cached_ranked_results = await _search_cached_catalog(db, query, max(limit, 12), preferred_mfrs)
        except Exception:
            cached_ranked_results = []

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

    if not force and db and source in ("auto", "mouser"):
        try:
            r = await db.execute(
                text("SELECT result_json, fetched_at FROM component_lookups WHERE query = :q ORDER BY fetched_at DESC LIMIT 1"),
                {"q": cache_key_mo}
            )
            row = r.fetchone()
            if row and row.fetched_at and (datetime.utcnow() - row.fetched_at) < CACHE_TTL:
                mouser_results = json.loads(row.result_json)
                mouser_cached = True
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

    # Partner-protect mode: when local item cache is already strong, skip live source calls.
    strong_cache = bool(cached_ranked_results and cached_ranked_results[0].get("_score", 0) >= 0.68 and len(cached_ranked_results) >= max(4, limit // 2))

    if not strong_cache and source in ("auto", "mouser") and not mouser_cached:
        import os as _os
        if _os.getenv("MOUSER_API_KEY"):
            fetch_tasks.append(mouser.search(query, limit=10))
            fetch_labels.append("mouser")

    if not strong_cache and source in ("lcsc",):
        # LCSC API offline — stub returns empty, kept for future
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
            elif label == "mouser":
                mouser_results = valid
            else:
                lc_results = valid

            if valid and db:
                await _cache_catalog_items(db, label, valid)

            # cache valid results — upsert so repeated searches replace stale data
            if valid and db:
                cache_key = cache_key_dk if label == "digikey" else (cache_key_mo if label == "mouser" else cache_key_lc)
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
    for r in cached_ranked_results + dk_results + mouser_results + lc_results:
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
    if strong_cache and not (dk_results or mouser_results or lc_results):
        final_source = "catalog_cache"
    elif dk_results and lc_results:
        final_source = "merged"
    elif dk_results:
        final_source = "digikey"
    elif mouser_results:
        final_source = "mouser"
    elif lc_results:
        final_source = "lcsc"
    else:
        final_source = "cache"
    
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
                    merged_top = await merge_results(MergeRequest(results=top, query=query), db=db)
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
        "cached": dk_cached or mouser_cached or lc_cached or bool(cached_ranked_results),
        "catalog_cached_count": len(cached_ranked_results),
        "dk_count": len(dk_results),
        "mouser_count": len(mouser_results),
        "lc_count": len(lc_results),
        "generic_suggestion": await suggest_generic(query, db),
    }
