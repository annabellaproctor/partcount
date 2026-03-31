"""
AI parsing via Gemini using official google-genai SDK.
Smart model selection: Automatically picks best available model based on quota.
Falls back to alternatives when primary model quota is exhausted.
https://ai.google.dev/pricing
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import os, json, logging, re
from datetime import datetime, timedelta
import asyncio
from concurrent.futures import ThreadPoolExecutor
from google import genai
from google.genai import types
from app.schemas.type_hierarchy import TYPE_HIERARCHY, flatten_type_paths, get_fields_for_type

log = logging.getLogger("ai_parse")
router = APIRouter(prefix="/api/ai", tags=["ai"])

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "10"))
GEMINI_MAX_WORKERS = int(os.getenv("GEMINI_MAX_WORKERS", "2"))

# Initialize client
client = None
if GEMINI_KEY:
    client = genai.Client(api_key=GEMINI_KEY)

# Run Gemini SDK work outside the async event loop so websocket/http I/O remains responsive.
_gemini_executor = ThreadPoolExecutor(max_workers=GEMINI_MAX_WORKERS, thread_name_prefix="gemini-worker")
_gemini_semaphore = asyncio.Semaphore(GEMINI_MAX_WORKERS)

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
GEMINI_RENAME_MODEL = os.getenv("GEMINI_RENAME_MODEL", "gemini-3-flash")

COMPONENT_EDITABLE_FIELDS = {
    "name", "value", "unit", "package", "voltage_rating", "tolerance", "notes",
    "datasheet_url", "mpn", "digikey_pn", "lcsc_pn", "description", "short_title",
    "short_title_manual", "type_path", "type_data", "sticker_tag_no", "search_alias",
}


def _to_snake(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def _collect_type_fields(tree: dict) -> set[str]:
    out: set[str] = set()

    def walk(node):
        if not isinstance(node, dict):
            return
        for k in node.get("fields", []) or []:
            if isinstance(k, str):
                out.add(_to_snake(k))
        for k in node.get("_fields", []) or []:
            if isinstance(k, str):
                out.add(_to_snake(k))
        subs = node.get("subcategories")
        if isinstance(subs, dict):
            for sub in subs.values():
                walk(sub)

    if isinstance(tree, dict):
        for v in tree.values():
            walk(v)
    return out


ALL_TYPE_FIELDS = _collect_type_fields(TYPE_HIERARCHY)


def _normalize_patch_fields(existing: dict, patch_fields: dict) -> dict:
    existing = existing or {}
    patch_fields = patch_fields if isinstance(patch_fields, dict) else {}

    normalized: dict = {}
    td_patch = patch_fields.get("type_data") if isinstance(patch_fields.get("type_data"), dict) else {}
    effective_type_path = (
        patch_fields.get("type_path")
        or existing.get("type_path")
        or ""
    )
    rule_fields = get_fields_for_type(effective_type_path).get("fields", []) if effective_type_path else []
    allowed_type_fields = { _to_snake(f) for f in (rule_fields or []) if isinstance(f, str) }
    if not allowed_type_fields:
        allowed_type_fields = set(ALL_TYPE_FIELDS)

    normalized_type_data = {}

    for k, v in td_patch.items():
        if v in (None, "", []):
            continue
        nk = _to_snake(str(k))
        normalized_type_data[nk] = v

    for k, v in patch_fields.items():
        if k == "type_data":
            continue
        if v in (None, "", []):
            continue

        if k in COMPONENT_EDITABLE_FIELDS:
            normalized[k] = v
            continue

        nk = _to_snake(str(k))
        if nk in COMPONENT_EDITABLE_FIELDS:
            normalized[nk] = v
            continue

        # Unknown top-level fields are preserved as archetype attributes.
        if nk in allowed_type_fields or nk:
            normalized_type_data[nk] = v

    if normalized_type_data:
        existing_td = existing.get("type_data") if isinstance(existing.get("type_data"), dict) else {}
        merged = dict(existing_td)
        merged.update(normalized_type_data)
        normalized["type_data"] = merged

    return normalized


def _normalize_remove_fields(remove_fields: list, effective_type_path: str) -> list[str]:
    fields = remove_fields if isinstance(remove_fields, list) else []
    rule_fields = get_fields_for_type(effective_type_path).get("fields", []) if effective_type_path else []
    allowed_type_fields = { _to_snake(f) for f in (rule_fields or []) if isinstance(f, str) }
    if not allowed_type_fields:
        allowed_type_fields = set(ALL_TYPE_FIELDS)

    out: list[str] = []
    for f in fields:
        if not isinstance(f, str):
            continue
        raw = f.strip()
        if not raw:
            continue
        if raw in COMPONENT_EDITABLE_FIELDS:
            out.append(raw)
            continue
        nf = _to_snake(raw)
        if nf in COMPONENT_EDITABLE_FIELDS:
            out.append(nf)
            continue
        if raw.startswith("type_data."):
            key = _to_snake(raw.split(".", 1)[1])
            if key:
                out.append(f"type_data.{key}")
            continue
        if nf in allowed_type_fields or nf:
            out.append(f"type_data.{nf}")
    return list(dict.fromkeys(out))


def _normalize_suggested_add_fields(existing: dict, patch_fields: dict, suggested: list) -> list[dict]:
    out: list[dict] = []

    # Always include deterministic suggestions from normalized patch.
    out.extend(_build_suggested_add_fields(existing or {}, patch_fields or {}))

    raw = suggested if isinstance(suggested, list) else []
    for item in raw:
        if isinstance(item, dict):
            field = item.get("field")
            value = item.get("value")
            reason = item.get("reason") or "inferred update"
            if field is None and len(item) == 1:
                k, v = next(iter(item.items()))
                field, value = k, v
            if field is not None and value not in (None, "", []):
                out.append({"field": str(field), "value": value, "reason": str(reason)})
        elif isinstance(item, str) and ":" in item:
            left, right = item.split(":", 1)
            if left.strip() and right.strip():
                out.append({"field": left.strip(), "value": right.strip(), "reason": "inferred update"})

    # de-dup by field+value
    seen = set()
    dedup: list[dict] = []
    for row in out:
        key = (str(row.get("field", "")).strip(), str(row.get("value", "")).strip())
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        dedup.append(row)
    return dedup[:30]


def _gemini_call_sync(prompt: str, schema: dict, model: str):
    """Blocking Gemini SDK call executed in dedicated worker threads."""
    sdk_client = genai.Client(api_key=GEMINI_KEY)
    response = sdk_client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.1,
        )
    )
    raw_text = (response.text or "").strip()
    if not raw_text:
        raise ValueError("Gemini returned empty response body")
    return json.loads(raw_text)


def _suggest_needed_details(mode: str, existing_data: dict | None) -> list[str]:
    existing = existing_data or {}
    out: list[str] = []

    if mode == "component":
        wanted = [
            ("exact manufacturer + MPN", existing.get("mpn")),
            ("electrical value + unit (e.g. 10k Ω, 100 nF)", existing.get("value") or existing.get("unit")),
            ("package / footprint (e.g. 0805, QFN32)", existing.get("package")),
            ("datasheet URL", existing.get("datasheet_url")),
            ("type path classification (module/...)", existing.get("type_path")),
        ]
    elif mode == "kit":
        wanted = [
            ("complete list of included components", None),
            ("quantity per included component", None),
            ("kit variant/model identifier", existing.get("mpn") or existing.get("barcode_id")),
            ("supplier/store URL for the exact kit", None),
        ]
    else:
        wanted = [
            ("supplier/store name", None),
            ("order reference / order number", None),
            ("line-item quantities", None),
            ("unit price or line total", None),
        ]

    for label, present in wanted:
        if present not in (None, "", []):
            continue
        out.append(label)

    # Keep response concise and deterministic.
    return out[:6]


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


async def _gemini(
    prompt: str,
    schema: dict,
    retry_count: int = 0,
    collapse_list: bool = True,
    preferred_model: str | None = None,
):
    """Call Gemini API with smart model selection and fallback"""
    if not client:
        raise HTTPException(
            503, 
            "Gemini AI is not configured. Add GEMINI_API_KEY to .env (free tier: no billing required)"
        )
    
    model_chain = [m["name"] for m in FREE_TIER_MODELS]
    if preferred_model and preferred_model not in model_chain:
        model_chain = [preferred_model] + model_chain
    elif preferred_model:
        model_chain = [preferred_model] + [m for m in model_chain if m != preferred_model]
    else:
        selected = select_best_model()
        model_chain = [selected] + [m for m in model_chain if m != selected]

    for attempt, model in enumerate(model_chain[retry_count:], start=retry_count):
        global _current_model
        _current_model = model

        try:
            async with _gemini_semaphore:
                loop = asyncio.get_running_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(_gemini_executor, _gemini_call_sync, prompt, schema, model),
                    timeout=GEMINI_TIMEOUT_SECONDS,
                )

            # Handle case where Gemini returns a list instead of object
            if isinstance(result, list) and collapse_list:
                if len(result) > 0:
                    result = result[0]
                else:
                    raise ValueError("Gemini returned empty list")

            return result

        except asyncio.TimeoutError:
            log_failed_request("timeout", {
                "timeout_seconds": GEMINI_TIMEOUT_SECONDS,
                "model": model,
                "attempt": attempt,
            })
            raise HTTPException(504, f"Gemini request timed out after {GEMINI_TIMEOUT_SECONDS:.0f}s")
        except Exception as e:
            error_msg = str(e)
            log_failed_request("sdk_error", {
                "error": error_msg,
                "type": type(e).__name__,
                "model": model,
                "attempt": attempt,
            })

            # Handle 429 rate limit - try next model
            if ('429' in error_msg or 'RESOURCE_EXHAUSTED' in error_msg) and attempt < len(model_chain) - 1:
                next_model = model_chain[attempt + 1]
                log.warning(f"Model {model} rate limited, switching to {next_model}")
                continue

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
                    f"All configured models exhausted. Try again later. Models tried: {', '.join(model_chain)}"
                )

            raise HTTPException(502, f"Gemini API error: {error_msg[:200]}")

    raise HTTPException(429, "Gemini models unavailable due to quota/rate limits")


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


class RenameWithRulesRequest(BaseModel):
    component_data: dict
    rules: str
    model: str | None = None


async def generate_component_title_with_rules(component_data: dict, rules: str, model: str | None = None) -> dict:
    if not rules or len(rules.strip()) < 4:
        raise HTTPException(400, "Rules text too short")

    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "confidence": {"type": "number"},
            "reasoning": {"type": "string"},
        },
        "required": ["name", "confidence"],
    }

    prompt = f"""Rename this electronics component into a compact canonical registry title.
Rules to apply (highest priority):
{rules}

Component data JSON:
{json.dumps(component_data or {}, ensure_ascii=False)}

Constraints:
- Return only a short final title in name.
- Avoid filler words like "part", "item", "component" unless required by rules.
- Preserve key technical identifiers (value/unit/package/variant) when available.
- Keep output under 64 chars when possible.
"""

    chosen_model = (model or "").strip() or GEMINI_RENAME_MODEL
    result = await _gemini(prompt, schema, preferred_model=chosen_model)
    title = (result.get("name") or "").strip()
    if not title:
        raise HTTPException(502, "AI rename returned empty title")
    return {
        "name": title,
        "confidence": float(result.get("confidence") or 0.0),
        "reasoning": (result.get("reasoning") or "").strip(),
        "model": chosen_model,
        "source": "gemini_rename",
    }


@router.post("/rename-component-title")
async def rename_component_title(req: RenameWithRulesRequest):
    return await generate_component_title_with_rules(req.component_data or {}, req.rules, req.model)


class OrderParseRequest(BaseModel):
    text: str
    html: str | None = None
    source_urls: list[str] | None = None


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


def _extract_urls(blob: str) -> list[str]:
    if not blob:
        return []
    urls = re.findall(r"https?://[^\s\"'<>]+", blob)
    out = []
    seen = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out[:40]


def _infer_component_type_path(text: str, existing: dict, patch_fields: dict) -> str:
    txt = (text or "").lower()
    current = (patch_fields.get("type_path") or existing.get("type_path") or "").strip()
    if current:
        return current

    if any(k in txt for k in ["esp32", "esp8266", "stm32", "nrf52", "nrf528"]):
        if any(k in txt for k in ["development board", "dev board", "devkit", "development kit"]):
            return "modules/development-board/microcontroller"
        return "modules/mcu-module"

    if "microcontroller" in txt:
        return "actives/ic/microcontroller"

    if "module" in txt and any(k in txt for k in ["wifi", "bluetooth", "lora"]):
        return "modules/communication/wifi"

    return ""


def _extract_component_type_data_from_text(text: str) -> dict:
    txt = text or ""
    lower = txt.lower()
    out: dict = {}

    mhz = [int(x) for x in re.findall(r"(\d{2,4})\s*mhz", lower)]
    if mhz:
        out["clock_speed_mhz"] = max(mhz)

    ram = re.findall(r"(\d+(?:\.\d+)?)\s*(kb|kib|mb|mib|gb|gib)\s*ram", lower)
    if ram:
        val, unit = ram[0]
        f = float(val)
        mult = {"kb": 1, "kib": 1, "mb": 1024, "mib": 1024, "gb": 1024 * 1024, "gib": 1024 * 1024}
        out["ram_size_kb"] = int(f * mult.get(unit, 1))

    flash = re.findall(r"(\d+(?:\.\d+)?)\s*(mb|mib|kb|kib)\s*(?:flash|rom)", lower)
    if flash:
        val, unit = flash[0]
        f = float(val)
        out["flash_size_mb"] = int(f) if unit.startswith("m") else round(f / 1024, 2)

    pin_match = re.search(r"(\d{1,3})(?:\s*(?:or|/)\s*(\d{1,3}))?\s*pins?", lower)
    if pin_match:
        a = int(pin_match.group(1))
        b = int(pin_match.group(2)) if pin_match.group(2) else None
        out["pin_count"] = f"{a}/{b}" if b else a

    if "dual core" in lower:
        out["core_count"] = 2
    elif "quad core" in lower:
        out["core_count"] = 4

    wireless = []
    if "wifi" in lower:
        out["wifi"] = True
        wireless.append("WiFi")
    if "bluetooth" in lower:
        out["bluetooth"] = True
        wireless.append("Bluetooth")
    if wireless:
        out["wireless"] = "+".join(wireless)

    interfaces = [x.upper() for x in ["uart", "spi", "i2c", "adc", "dac"] if x in lower]
    if interfaces:
        out["interface"] = ", ".join(interfaces)
        out["peripherals"] = ", ".join(interfaces)

    if "esp32" in lower:
        out.setdefault("mcu_family", "ESP32")
        out.setdefault("variant", "ESP32")
    if "tensilica" in lower:
        out.setdefault("architecture", "Tensilica LX6")

    if any(k in lower for k in ["development board", "dev board", "devkit"]):
        out.setdefault("form_factor", "Development Board")

    bridge = re.search(r"(cp2102|ch340|ft232|pl2303)", lower)
    if bridge:
        out["usb_bridge"] = bridge.group(1).upper()

    return out


def _build_suggested_add_fields(existing: dict, patch_fields: dict) -> list[dict]:
    suggestions: list[dict] = []
    existing = existing or {}

    for k, v in (patch_fields or {}).items():
        if k == "type_data":
            continue
        if v in (None, "", []):
            continue
        cur = existing.get(k)
        if cur in (None, "", []):
            suggestions.append({"field": k, "value": v, "reason": "inferred update"})

    type_data_patch = patch_fields.get("type_data") if isinstance(patch_fields, dict) else None
    existing_type_data = existing.get("type_data") if isinstance(existing.get("type_data"), dict) else {}
    if isinstance(type_data_patch, dict):
        for k, v in type_data_patch.items():
            if v in (None, "", []):
                continue
            if existing_type_data.get(k) in (None, "", []):
                suggestions.append({"field": f"type_data.{k}", "value": v, "reason": "archetype detail"})

    return suggestions[:20]


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

    existing_obj = req.existing_data or {}
    existing_type_path = (existing_obj.get("type_path") or "").strip()
    archetype_fields = get_fields_for_type(existing_type_path).get("fields", []) if existing_type_path else []
    editable_fields_txt = ", ".join(sorted(COMPONENT_EDITABLE_FIELDS))
    archetype_fields_txt = ", ".join(archetype_fields) if archetype_fields else "(none yet)"

    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string"},
            "action": {"type": "string"},
            "summary": {"type": "string"},
            "confidence": {"type": "number"},
            "patch_fields": {"type": "object"},
            "remove_fields": {"type": "array", "items": {"type": "string"}},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "suggested_add_fields": {"type": "array", "items": {"type": "object"}},
            "supplier_hints": {"type": "array", "items": {"type": "object"}},
            "order_hints": {"type": "array", "items": {"type": "object"}},
            "component_candidates": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["mode", "action", "summary", "confidence", "patch_fields", "remove_fields", "assumptions", "suggested_add_fields"],
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
3) assumptions: concise list of what you inferred/guessed when data is ambiguous.
4) suggested_add_fields: explicit list of key/value fields you propose adding.
5) supplier_hints: optional seller, marketplace, store, sku/mpn, price, url.
6) order_hints: optional order number, quantities, line-items, totals, dates.
7) component_candidates: when mode=kit/order, candidate components with quantity/type_path.

IMPORTANT:
- Do not ask follow-up questions, do not ask for confirmation, and do not request more user input.
- Make best-effort guesses and put uncertain choices in assumptions.
- Prefer concrete inferred values for technical fields (pin count, package, flash/ram, interface, protocol, dimensions) rather than returning empty patch_fields.

Supported type paths:
{available_paths}

Existing record:
{existing_json}

Editable top-level component fields:
{editable_fields_txt}

Current archetype/sub-archetype fields for type_data (from type_path={existing_type_path or 'n/a'}):
{archetype_fields_txt}

Strict output rules for patch_fields:
- Use only editable top-level fields above for direct updates.
- Put archetype-specific and extra technical keys under patch_fields.type_data.
- Never invent unknown top-level keys.
- If source uses labels like "Main Material", map to snake_case (e.g. main_material) in type_data.

Noisy text:
{req.text[:16000]}"""

    result = await _gemini(prompt, schema)
    result["patch_fields"] = result.get("patch_fields") or {}
    result["remove_fields"] = result.get("remove_fields") or []
    result["assumptions"] = [str(x).strip() for x in (result.get("assumptions") or []) if str(x).strip()]
    result["suggested_add_fields"] = result.get("suggested_add_fields") or []

    # Normalize AI output so unknown fields become type_data keys instead of being dropped.
    result["patch_fields"] = _normalize_patch_fields(existing_obj, result["patch_fields"])
    effective_type_path = (
        result["patch_fields"].get("type_path")
        or existing_type_path
        or ""
    )
    result["remove_fields"] = _normalize_remove_fields(result["remove_fields"], effective_type_path)

    # Deterministic fallback enrichment for common module/dev-board texts.
    if req.mode == "component":
        existing = existing_obj
        patch_fields = result["patch_fields"]
        inferred_type_path = _infer_component_type_path(req.text, existing, patch_fields)
        if inferred_type_path and not patch_fields.get("type_path"):
            patch_fields["type_path"] = inferred_type_path

        inferred_type_data = _extract_component_type_data_from_text(req.text)
        if inferred_type_data:
            existing_td = patch_fields.get("type_data") if isinstance(patch_fields.get("type_data"), dict) else {}
            patch_fields["type_data"] = {**existing_td, **inferred_type_data}

            # Map a few common fields upward for existing form compatibility.
            if not patch_fields.get("package") and inferred_type_data.get("form_factor"):
                patch_fields["package"] = inferred_type_data.get("form_factor")

        result["patch_fields"] = patch_fields
        result["suggested_add_fields"] = _normalize_suggested_add_fields(existing, patch_fields, result.get("suggested_add_fields"))

    if not result["assumptions"] and not result["patch_fields"] and not result["remove_fields"]:
        hints = _suggest_needed_details(req.mode, req.existing_data)
        result["assumptions"] = [f"Best-effort inference; unresolved ambiguity around: {', '.join(hints[:3])}"] if hints else ["Best-effort inference from noisy text."]

    if req.mode == "component":
        result["suggested_add_fields"] = _normalize_suggested_add_fields(existing_obj, result["patch_fields"], result.get("suggested_add_fields"))

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

    html_snippet = (req.html or "")[:12000]
    urls = req.source_urls or []
    if not urls:
        urls = _extract_urls((req.text or "") + "\n" + (req.html or ""))
    urls_txt = "\n".join(f"- {u}" for u in urls[:20])

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

Links extracted from paste:
{urls_txt or '- none'}

Rich/HTML snippet:
{html_snippet or '[none]'}

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
        "timeout_seconds": GEMINI_TIMEOUT_SECONDS,
        "max_workers": GEMINI_MAX_WORKERS,
        "last_model_check": _last_model_check.isoformat() if _last_model_check else None,
    }


@router.get("/model-status")
async def get_model_status():
    """Get current model selection and quota info"""
    return {
        "current_model": _current_model or FREE_TIER_MODELS[0]["name"],
        "available_models": FREE_TIER_MODELS,
        "check_interval_minutes": _model_check_interval.total_seconds() / 60,
        "timeout_seconds": GEMINI_TIMEOUT_SECONDS,
        "max_workers": GEMINI_MAX_WORKERS,
        "last_check": _last_model_check.isoformat() if _last_model_check else None,
        "strategy": "Auto-fallback on 429 rate limit errors",
    }
