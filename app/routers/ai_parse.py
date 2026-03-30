"""
AI parsing via Gemini using official google-genai SDK.
Smart model selection: Automatically picks best available model based on quota.
Falls back to alternatives when primary model quota is exhausted.
https://ai.google.dev/pricing
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import os, json, logging
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from app.schemas.type_hierarchy import flatten_type_paths, get_fields_for_type

log = logging.getLogger("ai_parse")
router = APIRouter(prefix="/api/ai", tags=["ai"])

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")

# Initialize client
client = None
if GEMINI_KEY:
    client = genai.Client(api_key=GEMINI_KEY)

# Failed request logging
_failed_requests = []
_max_failed_logs = 50

# Model selection
_current_model = None
_last_model_check = None
_model_check_interval = timedelta(minutes=30)

# Free tier models ranked by quota (RPD)
FREE_TIER_MODELS = [
    {"name": "gemini-3.1-flash-lite-preview", "rpd": 500, "rpm": 15, "tpm": 250000},
    {"name": "gemini-2.5-flash-lite", "rpd": 20, "rpm": 10, "tpm": 250000},
    {"name": "gemini-3-flash", "rpd": 20, "rpm": 5, "tpm": 250000},
]


def select_best_model():
    """
    Select best model based on quota.
    Strategy: Try models in order of daily quota (RPD).
    If we get 429 rate limit, move to next model.
    """
    global _current_model, _last_model_check
    
    # Check every 30 minutes
    now = datetime.utcnow()
    if _current_model and _last_model_check and (now - _last_model_check) < _model_check_interval:
        return _current_model
    
    _last_model_check = now
    
    # Default to highest quota model
    _current_model = FREE_TIER_MODELS[0]["name"]
    log.info(f"Selected model: {_current_model} (checked at {now.isoformat()})")
    
    return _current_model


def log_failed_request(error_type: str, details: dict):
    """Log failed API request for debugging"""
    global _failed_requests
    entry = {
        'timestamp': datetime.utcnow().isoformat(),
        'error_type': error_type,
        'details': details,
    }
    _failed_requests.append(entry)
    if len(_failed_requests) > _max_failed_logs:
        _failed_requests.pop(0)
    log.error(f"Failed Gemini request: {error_type} - {details}")


# JSON schemas
COMPONENT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_kit":         {"type": "boolean"},
        "name":           {"type": "string"},
        "type_path":      {"type": "string"},
        "manufacturer":   {"type": "string"},
        "mpn":            {"type": "string"},
        "value":          {"type": "string"},
        "unit":           {"type": "string"},
        "package":        {"type": "string"},
        "voltage_rating": {"type": "number"},
        "tolerance":      {"type": "string"},
        "description":    {"type": "string"},
        "type":           {"type": "string",
                           "enum": ["resistor","capacitor","diode","transistor","mosfet",
                                    "ic","inductor","connector","relay","led","module",
                                    "sensor","crystal","fuse","switch","default"]},
        "datasheet_url":  {"type": "string"},
        "image_url":      {"type": "string"},
        "confidence":     {"type": "number"},
        "notes":          {"type": "string"},
        "type_data":      {"type": "object"},
        "kit_components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type_path": {"type": "string"},
                    "value": {"type": "string"},
                    "unit": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "type_data": {"type": "object"},
                },
            },
        },
    },
    "required": ["name", "confidence"]
}

MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "name":           {"type": "string"},
        "manufacturer":   {"type": "string"},
        "mpn":            {"type": "string"},
        "value":          {"type": "string"},
        "unit":           {"type": "string"},
        "package":        {"type": "string"},
        "voltage_rating": {"type": "number"},
        "tolerance":      {"type": "string"},
        "description":    {"type": "string"},
        "type":           {"type": "string"},
        "datasheet_url":  {"type": "string"},
        "image_url":      {"type": "string"},
        "confidence":     {"type": "number"},
        "reasoning":      {"type": "string"},
    },
    "required": ["name", "confidence", "reasoning"]
}


async def _gemini(prompt: str, schema: dict, retry_count: int = 0, collapse_list: bool = True):
    """Call Gemini API with smart model selection and fallback"""
    if not client:
        raise HTTPException(
            503, 
            "Gemini AI is not configured. Add GEMINI_API_KEY to .env (free tier: no billing required)"
        )
    
    model = select_best_model()
    
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                temperature=0.1,
            )
        )
        
        # Parse JSON response
        result = json.loads(response.text)
        
        # Handle case where Gemini returns a list instead of object
        if isinstance(result, list) and collapse_list:
            if len(result) > 0:
                result = result[0]
            else:
                raise ValueError("Gemini returned empty list")
        
        return result
        
    except Exception as e:
        error_msg = str(e)
        log_failed_request("sdk_error", {
            "error": error_msg,
            "type": type(e).__name__,
            "model": model,
            "retry_count": retry_count,
        })
        
        # Handle 429 rate limit - try next model
        if ('429' in error_msg or 'RESOURCE_EXHAUSTED' in error_msg) and retry_count < len(FREE_TIER_MODELS) - 1:
            global _current_model
            next_model_idx = retry_count + 1
            _current_model = FREE_TIER_MODELS[next_model_idx]["name"]
            log.warning(f"Model {model} rate limited, switching to {_current_model}")
            return await _gemini(prompt, schema, retry_count + 1, collapse_list)
        
        # Better error message for 403
        if '403' in error_msg or 'Forbidden' in error_msg:
            raise HTTPException(
                403,
                "Gemini API key forbidden. Check API key permissions at https://aistudio.google.com/apikey"
            )
        
        # Handle 404 model not found
        if '404' in error_msg or 'not found' in error_msg:
            raise HTTPException(
                404,
                f"Gemini model not available: {model}. Error: {error_msg[:200]}"
            )
        
        # Rate limit exhausted all models
        if '429' in error_msg or 'RESOURCE_EXHAUSTED' in error_msg:
            raise HTTPException(
                429,
                f"All free tier models exhausted. Try again later. Models tried: {', '.join([m['name'] for m in FREE_TIER_MODELS])}"
            )
        
        raise HTTPException(502, f"Gemini API error: {error_msg[:200]}")


class ParseRequest(BaseModel):
    text: str


class MergeRequest(BaseModel):
    results: list[dict]
    query: str = ""


class EnrichRequest(BaseModel):
    mode: str = "component"  # component | kit | order
    action: str = "repair"   # add | repair | remove
    text: str
    existing_data: dict | None = None


class OrderParseRequest(BaseModel):
    text: str


def _normalize_order_parse_result(raw) -> dict:
    """Normalize AI output variations into canonical order parse payload."""
    if isinstance(raw, list):
        raw = {"items": raw}

    if not isinstance(raw, dict):
        return {"items": [], "confidence": 0.0}

    items = raw.get("items")

    # If model returned a single item object instead of {items:[...]}
    if items is None and any(k in raw for k in ["item_name", "name", "quantity", "price", "unit_price"]):
        items = [raw]

    if not isinstance(items, list):
        items = []

    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("item_name") or "").strip()
        qty = item.get("quantity")
        try:
            qty = int(qty) if qty is not None else 1
        except Exception:
            qty = 1
        if qty < 1:
            qty = 1

        unit_price = item.get("unit_price", item.get("price"))
        try:
            unit_price = float(unit_price) if unit_price is not None else None
        except Exception:
            unit_price = None

        line_total = item.get("line_total")
        try:
            line_total = float(line_total) if line_total is not None else None
        except Exception:
            line_total = None

        normalized_items.append({
            "name": name,
            "quantity": qty,
            "unit_price": unit_price,
            "line_total": line_total,
            "is_kit": bool(item.get("is_kit", False)),
            "type_path": item.get("type_path"),
            "notes": item.get("notes"),
        })

    return {
        "summary": raw.get("summary", ""),
        "supplier": raw.get("supplier", ""),
        "order_reference": raw.get("order_reference", raw.get("order_id", "")),
        "currency": raw.get("currency", "USD"),
        "items": normalized_items,
        "confidence": float(raw.get("confidence", 0.0) or 0.0),
    }


@router.post("/parse")
async def parse_component(req: ParseRequest):
    """Extract structured component data from any text."""
    if not req.text or len(req.text.strip()) < 5:
        raise HTTPException(400, "Text too short")

    available_paths = "\n".join(f"- {p}" for p in flatten_type_paths())

    prompt = f"""You are an electronics component database assistant.
Extract structured component information from the following text.
Be precise. If a field is not present, omit it or use an empty string.
Set confidence 0.0-1.0 based on how certain you are of the extracted data.
Use 'type_path' for hierarchical type classification.
For 'value', extract the electrical value (e.g. 10k, 100nF, 5V).
For 'unit', extract the SI unit (Ω, F, H, V, A, W, Hz).

IMPORTANT FOR NOISY WEBSITE COPY/PASTE:
- Ignore navigation text, keyboard shortcuts, footer links, ad blocks, recommendations, and unrelated products.
- Ignore reviews unless they contain concrete product specs.
- Extract only the most likely actual purchasable product(s) and specs.
- This must work for any website, not a single domain.

KIT DETECTION RULES:
- If text clearly contains multiple line-items or quantities, set is_kit=true.
- Example: "10x 10k resistors + 5x 100nF caps" should produce kit_components.
- For non-kit single parts, set is_kit=false.

SUPPORTED TYPE PATHS (pick the closest exact path):
{available_paths}

Text:
{req.text[:16000]}"""

    result = await _gemini(prompt, COMPONENT_SCHEMA)
    type_path = result.get("type_path")

    # Keep backwards compatibility for existing UI that expects flat "type".
    if type_path and not result.get("type"):
        parts = [p for p in type_path.split("/") if p]
        if len(parts) >= 2:
            result["type"] = parts[1]
        elif parts:
            result["type"] = parts[0]

    # Normalize type_data defaults and report missing required fields.
    if type_path:
        rules = get_fields_for_type(type_path)
        type_data = result.get("type_data") or {}
        for k, v in rules.get("defaults", {}).items():
            if k not in result and k not in type_data:
                result[k] = v

        missing = []
        for field in rules.get("required", []):
            val = result.get(field)
            if val is None and isinstance(type_data, dict):
                val = type_data.get(field)
            if val in (None, ""):
                missing.append(field)
        if missing:
            result["missing_required_fields"] = missing

    result["source"] = "gemini"
    return result


@router.post("/enrich-record")
async def enrich_record(req: EnrichRequest):
    """Suggest add/repair/remove field changes for component/kit/order from noisy pasted text."""
    if not req.text or len(req.text.strip()) < 10:
        raise HTTPException(400, "Text too short")

    existing_json = json.dumps(req.existing_data or {}, ensure_ascii=False)[:6000]
    available_paths = "\n".join(f"- {p}" for p in flatten_type_paths())

    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string"},
            "action": {"type": "string"},
            "summary": {"type": "string"},
            "confidence": {"type": "number"},
            "patch_fields": {"type": "object"},
            "remove_fields": {"type": "array", "items": {"type": "string"}},
            "supplier_hints": {"type": "array", "items": {"type": "object"}},
            "order_hints": {"type": "array", "items": {"type": "object"}},
            "component_candidates": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["mode", "action", "summary", "confidence", "patch_fields", "remove_fields"],
    }

    prompt = f"""You are a data-repair assistant for an electronics inventory database.
Task mode: {req.mode}
Requested action: {req.action}

You receive noisy copy-pasted webpage text from arbitrary sites (shopping, docs, blogs, etc).
Ignore unrelated UI and boilerplate text such as:
- nav menus, shortcuts, cart labels, footer links, recommendations, ad blocks, account sections.

Return JSON with:
1) patch_fields: only high-confidence fields to add/update.
2) remove_fields: fields that look wrong or should be cleared.
3) supplier_hints: optional seller, marketplace, store, sku/mpn, price, url.
4) order_hints: optional order number, quantities, line-items, totals, dates.
5) component_candidates: when mode=kit/order, candidate components with quantity/type_path.

Supported type paths:
{available_paths}

Existing record:
{existing_json}

Noisy text:
{req.text[:16000]}"""

    result = await _gemini(prompt, schema)
    result["source"] = "gemini_enrich"
    return result


@router.post("/parse-order")
async def parse_order_text(req: OrderParseRequest):
    """Extract order-centric line items from noisy page text (components + kits)."""
    if not req.text or len(req.text.strip()) < 10:
        raise HTTPException(400, "Text too short")

    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "supplier": {"type": "string"},
            "order_reference": {"type": "string"},
            "currency": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": "integer"},
                        "unit_price": {"type": "number"},
                        "line_total": {"type": "number"},
                        "is_kit": {"type": "boolean"},
                        "type_path": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                },
            },
            "confidence": {"type": "number"},
        },
        "required": ["items", "confidence"],
    }

    prompt = f"""Extract order line-items from noisy copy-pasted website text.
Rules:
- Keep only likely purchased items.
- Ignore site chrome, recommendations, reviews, keyboard shortcuts, and footer.
- Detect if an item appears to be a kit/assortment (is_kit=true).
- Quantity defaults to 1 if unknown.
- Use formatting clues as signals: headers, breadcrumbs, repeated title lines, bullet/line breaks, links, section labels.
- Input may include plaintext, markdown-like structure, copied table text, or HTML-like fragments.

Return strict JSON object with top-level keys: summary, supplier, order_reference, currency, items[], confidence.
Each item must include: name, quantity, unit_price (optional), line_total (optional), is_kit, type_path(optional), notes(optional).

Text:
{req.text[:18000]}"""

    raw = await _gemini(prompt, schema, collapse_list=False)
    result = _normalize_order_parse_result(raw)
    result["source"] = "gemini_order_parse"
    return result


@router.post("/merge")
async def merge_results(req: MergeRequest):
    """Merge multiple lookup results into best single record."""
    if not req.results:
        raise HTTPException(400, "No results to merge")

    results_json = json.dumps(req.results[:5], indent=2)
    prompt = f"""You are an electronics component database assistant.
I have {len(req.results)} search results for the query "{req.query}" from different sources (DigiKey, LCSC).
Merge them into the single best component record by:
1. Preferring DigiKey for MPN, package, datasheet, manufacturer
2. Using the most complete description
3. Taking the lowest unit price
4. Picking the best image URL (real product photo over generic)
5. Resolving any conflicting values by choosing the most specific/authoritative one

Set confidence 0.0-1.0. In 'reasoning', briefly explain key decisions made.

Results to merge:
{results_json}"""

    result = await _gemini(prompt, MERGE_SCHEMA)
    result["source"] = "gemini_merged"
    return result


@router.post("/classify")
async def classify_component(req: ParseRequest):
    """Classify component and suggest barcode prefix."""
    CLASSIFY_SCHEMA = {
        "type": "object",
        "properties": {
            "type":        {"type": "string"},
            "prefix":      {"type": "string"},
            "confidence":  {"type": "number"},
            "reasoning":   {"type": "string"},
        },
        "required": ["type", "prefix", "confidence"]
    }
    prompt = f"""Classify this electronic component and suggest a single-letter barcode prefix.
Common prefixes: R=resistor, C=capacitor, D=diode, Q=transistor/mosfet, U=IC, L=inductor, 
J=connector, K=relay, LED=led, M=module, S=sensor, Y=crystal, F=fuse, SW=switch.

Component: {req.text[:500]}"""

    return await _gemini(prompt, CLASSIFY_SCHEMA)


@router.get("/failed-requests")
async def get_failed_requests(limit: int = 50):
    """Get recent failed Gemini API requests for debugging"""
    return {
        "failed_requests": _failed_requests[-limit:],
        "total_failures": len(_failed_requests),
        "current_model": _current_model or FREE_TIER_MODELS[0]["name"],
        "fallback_chain": [f"{m['name']} ({m['rpd']} RPD)" for m in FREE_TIER_MODELS],
        "tier": "free (no billing)",
        "sdk": "google-genai",
        "last_model_check": _last_model_check.isoformat() if _last_model_check else None,
    }


@router.get("/model-status")
async def get_model_status():
    """Get current model selection and quota info"""
    return {
        "current_model": _current_model or FREE_TIER_MODELS[0]["name"],
        "available_models": FREE_TIER_MODELS,
        "check_interval_minutes": _model_check_interval.total_seconds() / 60,
        "last_check": _last_model_check.isoformat() if _last_model_check else None,
        "strategy": "Auto-fallback on 429 rate limit errors",
    }
