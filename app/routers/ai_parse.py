"""
AI parsing via Gemini using official google-genai SDK.
Smart model selection: Automatically picks best available model based on quota.
Falls back to alternatives when primary model quota is exhausted.
https://ai.google.dev/pricing
"""
from fastapi import APIRouter, HTTPException, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from pydantic import BaseModel, Field
import os, json, logging, re, uuid
from datetime import datetime, timedelta
import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
import httpx
from google import genai
from google.genai import types
from app.schemas.type_hierarchy import TYPE_HIERARCHY, flatten_type_paths, get_fields_for_type
from app.models.database import get_db
from app.models.models import Component, SystemSetting
from app.services.ai_context import build_workspace_snapshot, extract_text_from_upload, get_ai_sources, upsert_ai_source, serialize_ai_source, AI_SOURCE_DIR
from app.services.api_usage import track_api_call

log = logging.getLogger("ai_parse")
router = APIRouter(prefix="/api/ai", tags=["ai"])

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "10"))
# Order parsing reads a whole page of orders against the catalogue and taxonomy,
# so it is far slower than a single-part lookup and needs its own ceiling.
GEMINI_ORDER_TIMEOUT_SECONDS = float(os.getenv("GEMINI_ORDER_TIMEOUT_SECONDS", "60"))
GEMINI_MAX_WORKERS = int(os.getenv("GEMINI_MAX_WORKERS", "2"))
AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().lower()
AI_OPENAI_BASE_URL = os.getenv("AI_OPENAI_BASE_URL", os.getenv("OPENAI_BASE_URL", "")).strip().rstrip("/")
AI_OPENAI_API_KEY = os.getenv("AI_OPENAI_API_KEY", os.getenv("OPENAI_API_KEY", "")).strip()
AI_OPENAI_MODEL = os.getenv("AI_OPENAI_MODEL", "gpt-4o-mini").strip()
AI_ASSISTANT_TIMEOUT_SECONDS = float(os.getenv("AI_ASSISTANT_TIMEOUT_SECONDS", "30"))
AI_PROVIDER_CONFIG_KEY = "ai_provider_config_v1"

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
_provider_rr_state: dict[str, int] = {}
_provider_health: dict[str, dict] = {}

# Free tier models ranked by quota (RPD)
FREE_TIER_MODELS = [
    {"name": "gemini-3.1-flash-lite-preview", "rpd": 500, "rpm": 15, "tpm": 250000},
    {"name": "gemini-2.5-flash-lite", "rpd": 20, "rpm": 10, "tpm": 250000},
    {"name": "gemini-3-flash", "rpd": 20, "rpm": 5, "tpm": 250000},
]
GEMINI_RENAME_MODEL = os.getenv("GEMINI_RENAME_MODEL", "gemini-3-flash")

AI_TASKS = ["assistant", "parse", "enrich", "order", "merge", "classify", "rename"]


def _default_ai_provider_config() -> dict:
    return {
        "strategy": "priority",  # priority | round_robin | weighted_random
        "task_model_preferences": {
            "assistant": "",
            "parse": "",
            "enrich": "",
            "order": "",
            "merge": "",
            "classify": "",
            "rename": "",
        },
        "providers": [
            {
                "id": "gemini-default",
                "type": "gemini",
                "label": "Gemini",
                "enabled": bool(GEMINI_KEY),
                "priority": 10,
                "weight": 1,
                "api_key": GEMINI_KEY,
                "base_url": "",
                "default_model": FREE_TIER_MODELS[0]["name"],
                "models": {
                    "assistant": FREE_TIER_MODELS[0]["name"],
                    "parse": FREE_TIER_MODELS[0]["name"],
                    "enrich": FREE_TIER_MODELS[0]["name"],
                    "order": FREE_TIER_MODELS[0]["name"],
                    "merge": FREE_TIER_MODELS[0]["name"],
                    "classify": FREE_TIER_MODELS[0]["name"],
                    "rename": GEMINI_RENAME_MODEL,
                },
                "fallback_models": [m["name"] for m in FREE_TIER_MODELS[1:]],
            },
            {
                "id": "openai-compatible-default",
                "type": "openai-compatible",
                "label": "OpenAI Compatible",
                "enabled": bool(AI_OPENAI_BASE_URL),
                "priority": 20,
                "weight": 1,
                "api_key": AI_OPENAI_API_KEY,
                "base_url": AI_OPENAI_BASE_URL,
                "default_model": AI_OPENAI_MODEL,
                "models": {
                    "assistant": AI_OPENAI_MODEL,
                    "parse": AI_OPENAI_MODEL,
                    "enrich": AI_OPENAI_MODEL,
                    "order": AI_OPENAI_MODEL,
                    "merge": AI_OPENAI_MODEL,
                    "classify": AI_OPENAI_MODEL,
                    "rename": AI_OPENAI_MODEL,
                },
                "fallback_models": [],
            },
        ],
    }


ROUTING_SORTS = {"price", "throughput", "latency"}


def _normalize_routing(raw) -> dict:
    """OpenRouter provider-routing preferences. Empty dict when unset."""
    r = raw if isinstance(raw, dict) else {}
    out: dict = {}

    sort = (r.get("sort") or "").strip().lower()
    if sort in ROUTING_SORTS:
        out["sort"] = sort

    order = [str(x).strip() for x in (r.get("order") or []) if str(x).strip()]
    if order:
        out["order"] = order

    if r.get("require_parameters"):
        out["require_parameters"] = True

    dc = (r.get("data_collection") or "").strip().lower()
    if dc in {"allow", "deny"}:
        out["data_collection"] = dc

    if r.get("allow_fallbacks") is not None:
        out["allow_fallbacks"] = bool(r.get("allow_fallbacks"))

    for k in ("referer", "title"):
        v = (r.get(k) or "").strip()
        if v:
            out[k] = v

    return out


def _normalize_provider(raw: dict) -> dict:
    p = raw if isinstance(raw, dict) else {}
    provider_type = (p.get("type") or "openai-compatible").strip().lower()
    models = p.get("models") if isinstance(p.get("models"), dict) else {}
    norm_models = {task: (models.get(task) or "").strip() for task in AI_TASKS}
    return {
        "id": (p.get("id") or f"provider-{uuid.uuid4().hex[:8]}").strip(),
        "type": provider_type,
        "label": (p.get("label") or provider_type).strip(),
        "enabled": bool(p.get("enabled", True)),
        "priority": int(p.get("priority") or 100),
        "weight": max(1, int(p.get("weight") or 1)),
        "api_key": (p.get("api_key") or "").strip(),
        "base_url": (p.get("base_url") or "").strip().rstrip("/"),
        "alt_base_urls": [str(u).strip().rstrip("/") for u in (p.get("alt_base_urls") or []) if str(u).strip()],
        "default_model": (p.get("default_model") or "").strip(),
        "models": norm_models,
        "fallback_models": [str(m).strip() for m in (p.get("fallback_models") or []) if str(m).strip()],
        "routing": _normalize_routing(p.get("routing")),
    }


def _sanitize_provider_for_client(provider: dict) -> dict:
    p = dict(provider)
    key = p.get("api_key") or ""
    p["api_key_masked"] = ("" if not key else ("*" * max(0, len(key) - 4)) + key[-4:])
    p.pop("api_key", None)
    return p


def _normalize_ai_provider_config(raw: dict | None) -> dict:
    base = _default_ai_provider_config()
    data = raw if isinstance(raw, dict) else {}
    strategy = str(data.get("strategy") or base["strategy"]).strip().lower()
    if strategy not in {"priority", "round_robin", "weighted_random"}:
        strategy = "priority"

    prefs = data.get("task_model_preferences") if isinstance(data.get("task_model_preferences"), dict) else {}
    task_prefs = {task: str(prefs.get(task) or "").strip() for task in AI_TASKS}

    raw_providers = data.get("providers") if isinstance(data.get("providers"), list) else base["providers"]
    providers = [_normalize_provider(p) for p in raw_providers]
    if not providers:
        providers = [_normalize_provider(p) for p in base["providers"]]

    return {
        "strategy": strategy,
        "task_model_preferences": task_prefs,
        "providers": providers,
    }


async def _get_ai_provider_config(db: AsyncSession | None) -> dict:
    if not db:
        return _normalize_ai_provider_config(None)
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == AI_PROVIDER_CONFIG_KEY))).scalar_one_or_none()
    if not row or not (row.value or "").strip():
        cfg = _normalize_ai_provider_config(None)
        db.add(SystemSetting(key=AI_PROVIDER_CONFIG_KEY, value=json.dumps(cfg, ensure_ascii=False)))
        await db.flush()
        return cfg
    try:
        loaded = json.loads(row.value)
    except Exception:
        loaded = None
    cfg = _normalize_ai_provider_config(loaded)
    if loaded != cfg:
        row.value = json.dumps(cfg, ensure_ascii=False)
        row.updated_at = datetime.utcnow()
        await db.flush()
    return cfg


async def _set_ai_provider_config(db: AsyncSession, cfg: dict) -> dict:
    existing = await _get_ai_provider_config(db)
    existing_keys = {
        (p.get("id") or "").strip(): (p.get("api_key") or "")
        for p in (existing.get("providers") or [])
        if (p.get("id") or "").strip()
    }

    normalized = _normalize_ai_provider_config(cfg)
    for p in normalized.get("providers") or []:
        pid = (p.get("id") or "").strip()
        if pid and not (p.get("api_key") or "").strip() and existing_keys.get(pid):
            p["api_key"] = existing_keys[pid]

    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == AI_PROVIDER_CONFIG_KEY))).scalar_one_or_none()
    payload = json.dumps(normalized, ensure_ascii=False)
    if row:
        row.value = payload
        row.updated_at = datetime.utcnow()
    else:
        db.add(SystemSetting(key=AI_PROVIDER_CONFIG_KEY, value=payload))
    await db.flush()
    return normalized


def _provider_is_ready(provider: dict) -> bool:
    if not provider.get("enabled"):
        return False
    ptype = provider.get("type")
    if ptype == "gemini":
        return bool(provider.get("api_key"))
    if ptype in {"openai", "openai-compatible"}:
        return bool(provider.get("base_url"))
    return False


def _is_local_openai_endpoint(base_url: str) -> bool:
    b = (base_url or "").lower()
    return any(x in b for x in ["localhost", "127.0.0.1", "host.docker.internal", "172.17.0.1"]) or b.startswith("http://ollama")


def _provider_health_entry(provider_id: str) -> dict:
    return _provider_health.setdefault(provider_id, {"failures": 0, "cooldown_until": None, "last_error": "", "last_ok": None})


def _provider_mark_success(provider: dict):
    pid = (provider.get("id") or provider.get("label") or "provider").strip()
    state = _provider_health_entry(pid)
    state["failures"] = 0
    state["cooldown_until"] = None
    state["last_error"] = ""
    state["last_ok"] = datetime.utcnow().isoformat()


def _provider_mark_failure(provider: dict, error: Exception):
    pid = (provider.get("id") or provider.get("label") or "provider").strip()
    state = _provider_health_entry(pid)
    state["failures"] = int(state.get("failures") or 0) + 1
    message = str(error)[:220]
    state["last_error"] = message

    cooldown = 0
    if isinstance(error, HTTPException) and error.status_code == 429:
        cooldown = 45
    elif isinstance(error, HTTPException) and error.status_code in {502, 503, 504}:
        cooldown = 25
    else:
        cooldown = min(20 + (state["failures"] * 8), 90)

    # Keep local providers available aggressively even if they fail once.
    if provider.get("type") in {"openai", "openai-compatible"} and _is_local_openai_endpoint(provider.get("base_url") or ""):
        cooldown = min(cooldown, 6)

    state["cooldown_until"] = (datetime.utcnow() + timedelta(seconds=cooldown)).isoformat() if cooldown > 0 else None


def _provider_is_in_cooldown(provider: dict) -> bool:
    pid = (provider.get("id") or provider.get("label") or "provider").strip()
    state = _provider_health.get(pid) or {}
    cutoff = state.get("cooldown_until")
    if not cutoff:
        return False
    try:
        return datetime.fromisoformat(cutoff) > datetime.utcnow()
    except Exception:
        return False


def _ordered_candidate_providers(cfg: dict, task: str) -> list[dict]:
    candidates = [p for p in (cfg.get("providers") or []) if _provider_is_ready(p)]
    if not candidates:
        return []

    active = [p for p in candidates if not _provider_is_in_cooldown(p)]
    cooling = [p for p in candidates if _provider_is_in_cooldown(p)]
    # Prefer non-cooling providers; if all are cooling we still attempt them.
    candidates = active + cooling if active else cooling

    candidates.sort(key=lambda p: (int(p.get("priority") or 100), p.get("id") or ""))
    strategy = (cfg.get("strategy") or "priority").strip().lower()
    if strategy == "priority" or len(candidates) == 1:
        return candidates

    if strategy == "round_robin":
        key = f"{task}:{'|'.join(p.get('id') or '' for p in candidates)}"
        idx = _provider_rr_state.get(key, 0) % len(candidates)
        _provider_rr_state[key] = idx + 1
        return candidates[idx:] + candidates[:idx]

    if strategy == "weighted_random":
        total = sum(max(1, int(p.get("weight") or 1)) for p in candidates)
        draw = random.uniform(0, total)
        upto = 0.0
        chosen_idx = 0
        for i, p in enumerate(candidates):
            upto += max(1, int(p.get("weight") or 1))
            if upto >= draw:
                chosen_idx = i
                break
        return candidates[chosen_idx:] + candidates[:chosen_idx]

    return candidates

COMPONENT_EDITABLE_FIELDS = {
    "name", "value", "unit", "package", "voltage_rating", "tolerance", "notes",
    "current_rating", "power_rating",
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


def _strip_json_fences(text: str) -> str:
    blob = (text or "").strip()
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob, flags=re.IGNORECASE)
        blob = re.sub(r"\s*```$", "", blob)
    return blob.strip()


async def _call_openai_compatible(
    prompt: str,
    schema: dict,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float = AI_ASSISTANT_TIMEOUT_SECONDS,
    routing: dict | None = None,
) -> dict:
    if not base_url:
        raise HTTPException(503, "OpenAI-compatible AI provider is not configured")

    model_name = model or AI_OPENAI_MODEL
    schema_text = json.dumps(schema, ensure_ascii=False)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a data-aware assistant for an electronics inventory app. "
                "Return only strict JSON matching the requested schema. "
                "Do not wrap the response in markdown fences. "
                f"Schema: {schema_text}"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # OpenRouter serves many models from competing hosts. `provider` picks
    # between those hosts for the SAME model -- cheapest first, only ones that
    # honour our parameters, and optionally only ones that do not log prompts.
    # Ignored by other OpenAI-compatible endpoints, so it is safe to always send
    # when configured.
    if routing:
        prov: dict = {}
        if routing.get("sort"):
            prov["sort"] = routing["sort"]
        if routing.get("order"):
            prov["order"] = routing["order"]
        if routing.get("require_parameters"):
            prov["require_parameters"] = True
        if routing.get("data_collection"):
            prov["data_collection"] = routing["data_collection"]
        if routing.get("allow_fallbacks") is not None:
            prov["allow_fallbacks"] = bool(routing["allow_fallbacks"])
        if prov:
            payload["provider"] = prov
        # Attribution headers; optional, but they identify this app on
        # OpenRouter's dashboard rather than showing as anonymous traffic.
        if routing.get("referer"):
            headers["HTTP-Referer"] = routing["referer"]
        if routing.get("title"):
            headers["X-Title"] = routing["title"]

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        try:
            r = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as exc:
            retry_after = (exc.response.headers.get("retry-after") or "").strip() if exc.response is not None else ""
            if exc.response is not None and exc.response.status_code == 429:
                detail = f"OpenAI-compatible provider rate limited (429). retry-after={retry_after or 'n/a'}"
                raise HTTPException(429, detail)
            status = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(502 if status >= 500 else status, f"OpenAI-compatible provider error ({status})")
        except Exception:
            payload.pop("response_format", None)
            r = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()

    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    content = _strip_json_fences(content)
    if not content:
        raise ValueError("OpenAI-compatible provider returned empty content")
    return json.loads(content)


def _gemini_call_sync(prompt: str, schema: dict, model: str, api_key: str):
    """Blocking Gemini SDK call executed in dedicated worker threads."""
    sdk_client = genai.Client(api_key=api_key)
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


def _task_model_for_provider(provider: dict, task: str, preferred_model: str | None, cfg: dict | None = None) -> str:
    if preferred_model:
        return preferred_model
    cfg = cfg or {}
    task_prefs = cfg.get("task_model_preferences") if isinstance(cfg.get("task_model_preferences"), dict) else {}
    if task_prefs.get(task):
        return str(task_prefs[task]).strip()
    models = provider.get("models") if isinstance(provider.get("models"), dict) else {}
    if models.get(task):
        return str(models[task]).strip()
    return str(provider.get("default_model") or "").strip()


async def _call_provider(
    provider: dict,
    prompt: str,
    schema: dict,
    *,
    task: str,
    preferred_model: str | None,
    cfg: dict | None,
    collapse_list: bool,
    db: AsyncSession | None,
) -> dict:
    provider_type = (provider.get("type") or "").strip().lower()
    model = _task_model_for_provider(provider, task, preferred_model, cfg)
    if provider_type == "gemini":
        return await _gemini(
            prompt,
            schema,
            preferred_model=model,
            collapse_list=collapse_list,
            db=db,
            api_key=(provider.get("api_key") or "").strip(),
            fallback_models=provider.get("fallback_models") or [],
            timeout_seconds=(
                GEMINI_ORDER_TIMEOUT_SECONDS if task == "order" else GEMINI_TIMEOUT_SECONDS
            ),
        )

    if provider_type in {"openai", "openai-compatible"}:
        base_urls = []
        primary = (provider.get("base_url") or "").strip().rstrip("/")
        if primary:
            base_urls.append(primary)
        base_urls.extend(
            u for u in [str(x).strip().rstrip("/") for x in (provider.get("alt_base_urls") or []) if str(x).strip()] if u and u not in base_urls
        )
        if not base_urls:
            raise HTTPException(503, "OpenAI-compatible provider has no base URL configured")

        last_exc: Exception | None = None
        for url in base_urls:
            try:
                return await _call_openai_compatible(
                    prompt,
                    schema,
                    base_url=url,
                    api_key=(provider.get("api_key") or "").strip(),
                    model=model,
                    timeout_seconds=(
                        GEMINI_ORDER_TIMEOUT_SECONDS if task == "order"
                        else AI_ASSISTANT_TIMEOUT_SECONDS
                    ),
                    routing=provider.get("routing") or None,
                )
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc:
            raise last_exc
        raise HTTPException(502, "OpenAI-compatible provider endpoints failed")

    raise HTTPException(400, f"Unsupported provider type: {provider_type}")


async def _call_data_aware_ai(
    prompt: str,
    schema: dict,
    *,
    task: str,
    preferred_model: str | None = None,
    collapse_list: bool = True,
    db: AsyncSession | None = None,
) -> dict:
    cfg = await _get_ai_provider_config(db)
    candidates = _ordered_candidate_providers(cfg, task)
    if not candidates:
        return {
            "reply": "No configured AI provider is active. Add at least one enabled provider in Settings > AI Providers.",
            "confidence": 0.0,
            "actions": [],
            "queries": [],
            "migration_sql": [],
            "cleanup_steps": [],
            "source": "local_fallback",
        }

    errors: list[str] = []
    for provider in candidates:
        provider_id = provider.get("id") or provider.get("label") or provider.get("type")
        try:
            result = await _call_provider(
                provider,
                prompt,
                schema,
                task=task,
                preferred_model=preferred_model,
                cfg=cfg,
                collapse_list=collapse_list,
                db=db,
            )
            if isinstance(result, dict):
                result.setdefault("provider_id", provider_id)
                result.setdefault("provider_type", provider.get("type"))
            _provider_mark_success(provider)
            return result
        except Exception as exc:
            _provider_mark_failure(provider, exc)
            errors.append(f"{provider_id}: {str(exc)[:160]}")
            continue

    raise HTTPException(502, "All AI providers failed. " + " | ".join(errors[:4]))


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
        "current_rating": {"type": "number"},
        "power_rating":   {"type": "number"},
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
                    "mpn": {"type": "string"},
                    "manufacturer_name": {"type": "string"},
                    "type_path": {"type": "string"},
                    "value": {"type": "string"},
                    "unit": {"type": "string"},
                    "package": {"type": "string"},
                    "current_rating": {"type": "number"},
                    "power_rating": {"type": "number"},
                    "quantity": {"type": "integer"},
                    "stock_quantity": {"type": "integer"},
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
        "current_rating": {"type": "number"},
        "power_rating":   {"type": "number"},
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
    db: AsyncSession | None = None,
    api_key: str | None = None,
    fallback_models: list[str] | None = None,
    timeout_seconds: float | None = None,
):
    """Call Gemini API with smart model selection and fallback"""
    effective_timeout = timeout_seconds or GEMINI_TIMEOUT_SECONDS
    effective_key = (api_key or GEMINI_KEY or "").strip()
    if not effective_key:
        raise HTTPException(
            503, 
            "Gemini AI is not configured. Add GEMINI_API_KEY to .env (free tier: no billing required)"
        )

    fallback = [m for m in (fallback_models or []) if isinstance(m, str) and m.strip()]
    model_chain = [m["name"] for m in FREE_TIER_MODELS]
    if fallback:
        model_chain = list(dict.fromkeys(fallback + model_chain))
    if preferred_model and preferred_model.strip():
        model_chain = [preferred_model.strip()] + [m for m in model_chain if m != preferred_model.strip()]
    else:
        selected = select_best_model()
        model_chain = [selected] + [m for m in model_chain if m != selected]

    for attempt, model in enumerate(model_chain[retry_count:], start=retry_count):
        global _current_model
        _current_model = model
        
        start_time = datetime.utcnow()
        try:
            async with _gemini_semaphore:
                loop = asyncio.get_running_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(_gemini_executor, _gemini_call_sync, prompt, schema, model, effective_key),
                    timeout=effective_timeout,
                )

            # Track successful call
            if db:
                response_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
                await track_api_call(
                    db,
                    api_name="gemini",
                    endpoint=model,
                    success=True,
                    response_time_ms=response_time_ms,
                )

            # Handle case where Gemini returns a list instead of object
            if isinstance(result, list) and collapse_list:
                if len(result) > 0:
                    result = result[0]
                else:
                    raise ValueError("Gemini returned empty list")

            return result

        except asyncio.TimeoutError:
            # Track timeout
            if db:
                await track_api_call(
                    db,
                    api_name="gemini",
                    endpoint=model,
                    success=False,
                    error_message=f"Timeout after {effective_timeout}s",
                )
            log_failed_request("timeout", {
                "timeout_seconds": effective_timeout,
                "model": model,
                "attempt": attempt,
            })
            raise HTTPException(504, f"Gemini request timed out after {effective_timeout:.0f}s")
        except Exception as e:
            error_msg = str(e)
            # Track error
            if db:
                await track_api_call(
                    db,
                    api_name="gemini",
                    endpoint=model,
                    success=False,
                    error_message=error_msg[:200],
                )
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
    source_ids: list[str] | None = None
    page_context: dict | None = None
    context_query: str | None = None


class MergeRequest(BaseModel):
    results: list[dict]
    query: str = ""


class EnrichRequest(BaseModel):
    mode: str = "component"  # component | kit | order
    action: str = "smart"    # smart | add | repair | remove
    text: str
    existing_data: dict | None = None
    page_context: dict | None = None
    source_ids: list[str] | None = None
    context_query: str | None = None


class RenameWithRulesRequest(BaseModel):
    component_data: dict
    rules: str
    model: str | None = None


class AssistantRequest(BaseModel):
    message: str
    page_context: dict | None = None
    source_ids: list[str] | None = None
    context_query: str | None = None
    mode: str = "general"
    model: str | None = None


class ReferenceTextRequest(BaseModel):
    title: str | None = None
    text: str
    source_kind: str = "paste"
    mime_type: str | None = None
    page_context: dict | None = None


class AssistantActionResponse(BaseModel):
    reply: str
    confidence: float = 0.0
    actions: list[dict] = Field(default_factory=list)
    queries: list[dict] = Field(default_factory=list)
    cleanup_steps: list[dict] = Field(default_factory=list)
    migration_sql: list[str] = Field(default_factory=list)
    source_ids_used: list[str] = Field(default_factory=list)


class ProviderConfigUpdateRequest(BaseModel):
    strategy: str = "priority"
    task_model_preferences: dict = Field(default_factory=dict)
    providers: list[dict] = Field(default_factory=list)


async def generate_component_title_with_rules(component_data: dict, rules: str, model: str | None = None, db: AsyncSession | None = None) -> dict:
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
    result = await _call_data_aware_ai(prompt, schema, task="rename", preferred_model=chosen_model, db=db)
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
async def rename_component_title(req: RenameWithRulesRequest, db: AsyncSession = Depends(get_db)):
    return await generate_component_title_with_rules(req.component_data or {}, req.rules, req.model, db=db)


class OrderParseRequest(BaseModel):
    text: str
    html: str | None = None
    source_urls: list[str] | None = None
    source_ids: list[str] | None = None
    page_context: dict | None = None
    context_query: str | None = None


TYPE_PATH_VOCAB_KEY = "type_path_vocabulary_v1"

# Paths for things ordered but not yet stocked. A type_path only otherwise
# exists once some component uses it, so without these the parser cannot file a
# relay module until a relay module is already filed.
DEFAULT_EXTRA_TYPE_PATHS = [
    "modules/relay",
    "modules/display",
    "modules/display/lcd",
    "modules/sensor",
    "modules/mcu-module",
    "modules/power/converter",
    "modules/power/regulator",
    "power/supply",
    "prototyping/breadboard",
    "prototyping/jumper-wire",
    "connectors/header",
    "consumables/anti-static-bag",
    "consumables/storage",
]


async def get_known_type_paths(db: AsyncSession) -> list[str]:
    """Every type_path the parser may choose from.

    Union of paths already on components and a declared vocabulary, so a
    category can exist before its first part does.
    """
    in_use = {
        r[0] for r in (await db.execute(
            select(Component.type_path).where(Component.type_path.isnot(None)).distinct()
        )).all() if r[0]
    }

    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == TYPE_PATH_VOCAB_KEY)
    )).scalar_one_or_none()
    if row and row.value:
        try:
            declared = json.loads(row.value)
            if isinstance(declared, list):
                in_use.update(str(p).strip() for p in declared if str(p).strip())
        except Exception:
            pass
    else:
        in_use.update(DEFAULT_EXTRA_TYPE_PATHS)

    return sorted(in_use)


def _num_or_none(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _normalize_order_item(item: dict, known_paths: set[str] | None = None) -> dict:
    name = (item.get("name") or item.get("item_name") or "").strip()

    qty = item.get("quantity")
    try:
        qty = int(qty) if qty is not None else 1
    except Exception:
        qty = 1
    if qty < 1:
        qty = 1

    # A path the model invented is worse than none: it silently creates a second
    # taxonomy. Drop anything not already in use.
    type_path = (item.get("type_path") or "").strip() or None
    if type_path and known_paths is not None and type_path not in known_paths:
        type_path = None

    return {
        "name": name,
        "quantity": qty,
        "unit_price": _num_or_none(item.get("unit_price", item.get("price"))),
        "line_total": _num_or_none(item.get("line_total")),
        "is_kit": bool(item.get("is_kit", False)),
        # Default true so a model that omits the flag does not silently drop
        # real parts; the reject list is the exception, not the rule.
        "is_inventory": bool(item.get("is_inventory", True)),
        "reject_reason": (item.get("reject_reason") or "").strip() or None,
        "type_path": type_path,
        "match_barcode_id": (item.get("match_barcode_id") or "").strip() or None,
        "match_reason": (item.get("match_reason") or "").strip() or None,
        "notes": item.get("notes"),
    }


def _normalize_order_parse_result(raw, known_paths: set[str] | None = None) -> dict:
    """Normalize AI output into canonical multi-order payload.

    Accepts the current {orders:[{items:[]}]} shape and the older flat
    {items:[]} one, so a model that ignores the schema still parses.
    """
    if isinstance(raw, list):
        # A bare array is ambiguous, and models return both kinds. If its
        # elements carry their own items[], it is a list of orders; otherwise
        # they are line items belonging to one unnamed order.
        if raw and isinstance(raw[0], dict) and isinstance(raw[0].get("items"), list):
            raw = {"orders": raw}
        else:
            raw = {"items": raw}
    if not isinstance(raw, dict):
        return {"orders": [], "confidence": 0.0}

    orders_raw = raw.get("orders")

    if not isinstance(orders_raw, list) or not orders_raw:
        # Older/flat shape, or a bare single item — wrap it as one order.
        items = raw.get("items")
        if items is None and any(k in raw for k in ("item_name", "name", "quantity", "price", "unit_price")):
            items = [raw]
        orders_raw = [{
            "order_reference": raw.get("order_reference", raw.get("order_id", "")),
            "order_date": raw.get("order_date"),
            "order_total": raw.get("order_total"),
            "items": items if isinstance(items, list) else [],
        }]

    orders = []
    for o in orders_raw:
        if not isinstance(o, dict):
            continue
        items = o.get("items")
        if not isinstance(items, list):
            items = []
        norm = [_normalize_order_item(i, known_paths) for i in items if isinstance(i, dict)]
        norm = [i for i in norm if i["name"]]
        if not norm:
            continue

        ref = (o.get("order_reference") or "").strip()
        # The model used to answer "Multiple" when it merged orders; that is not
        # a reference, and a PO carrying it is worse than one with none.
        if ref.lower() in {"multiple", "multiple orders", "various", "n/a", "unknown"}:
            ref = ""

        orders.append({
            "order_reference": ref,
            "order_date": (o.get("order_date") or "").strip() or None,
            "order_total": _num_or_none(o.get("order_total")),
            "items": norm,
        })

    return {
        "summary": raw.get("summary", ""),
        "supplier": raw.get("supplier", ""),
        "currency": raw.get("currency", "USD"),
        "orders": orders,
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
async def parse_component(req: ParseRequest, db: AsyncSession = Depends(get_db)):
    """Extract structured component data from any text."""
    if not req.text or len(req.text.strip()) < 5:
        raise HTTPException(400, "Text too short")

    available_paths = "\n".join(f"- {p}" for p in flatten_type_paths())
    context_bundle = None
    if req.source_ids or req.page_context or req.context_query:
        context_bundle = await build_workspace_snapshot(
            db,
            message=req.context_query or req.text,
            page_context=req.page_context or {},
            source_ids=req.source_ids,
        )
    context_json = json.dumps(context_bundle or {}, ensure_ascii=False)[:6000]

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

Workspace context and reference sources:
{context_json}

Text:
{req.text[:16000]}"""

    result = await _call_data_aware_ai(prompt, COMPONENT_SCHEMA, task="parse", db=db)
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
async def enrich_record(req: EnrichRequest, db: AsyncSession = Depends(get_db)):
    """Suggest add/repair/remove field changes for component/kit/order from noisy pasted text."""
    if not req.text or len(req.text.strip()) < 10:
        raise HTTPException(400, "Text too short")

    existing_json = json.dumps(req.existing_data or {}, ensure_ascii=False)[:6000]
    context_bundle = await build_workspace_snapshot(
        db,
        message=req.context_query or req.text,
        page_context=req.page_context or {},
        source_ids=req.source_ids,
    )
    context_json = json.dumps({
        "page_context": req.page_context or {},
        "workspace": context_bundle,
    }, ensure_ascii=False)[:6000]
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
- If action is "smart", combine repair/add/remove behavior in one pass.
- Prefer preserving strong identifiers (barcode_id, mpn, supplier part numbers) and improve weak/empty fields around them.

Supported type paths:
{available_paths}

Existing record:
{existing_json}

Page context:
{context_json}

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

    result = await _call_data_aware_ai(prompt, schema, task="enrich", db=db)
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
async def parse_order_text(req: OrderParseRequest, db: AsyncSession = Depends(get_db)):
    """Extract order-centric line items from noisy page text (components + kits)."""
    if not req.text or len(req.text.strip()) < 10:
        raise HTTPException(400, "Text too short")

    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "supplier": {"type": "string"},
            "orders": {
                "type": "array",
                "description": "One entry per distinct order found in the paste.",
                "items": {
                    "type": "object",
                    "properties": {
                        "order_reference": {"type": "string"},
                        "order_date": {"type": "string"},
                        "order_total": {"type": "number"},
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
                                    "is_inventory": {"type": "boolean"},
                                    "reject_reason": {"type": "string"},
                                    "type_path": {"type": "string"},
                                    "match_barcode_id": {"type": "string"},
                                    "match_reason": {"type": "string"},
                                    "notes": {"type": "string"},
                                },
                                "required": ["name", "is_inventory"],
                            },
                        },
                    },
                    "required": ["items"],
                },
            },
            "currency": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["orders", "confidence"],
    }

    html_snippet = (req.html or "")[:12000]
    urls = req.source_urls or []
    if not urls:
        urls = _extract_urls((req.text or "") + "\n" + (req.html or ""))
    urls_txt = "\n".join(f"- {u}" for u in urls[:20])

    # No workspace snapshot here. It added ~1.6k chars of cache keys, component
    # counts, missing-datasheet stats and stray source blurbs to every call --
    # none of which helps read an order. The taxonomy and catalogue below are
    # the only context this task needs.

    # Give the model the real taxonomy and catalogue. Without these it invents
    # paths ("furniture/storage") and cannot recognise parts already stocked.
    known_paths = await get_known_type_paths(db)
    paths_txt = "\n".join(f"- {p}" for p in sorted(known_paths)) or "- (none defined yet)"

    # Only parts a marketplace title could plausibly name. Sending the whole
    # catalogue -- 75 near-identical resistors that no Amazon listing will ever
    # match by name -- blew the provider's payload limit and timeout for no gain.
    catalog_rows = (await db.execute(
        select(Component.barcode_id, Component.name, Component.value, Component.unit)
        .where(
            (Component.type_path.is_(None))
            | (~Component.type_path.in_(("passives/resistors", "passives/capacitors")))
        )
        .order_by(Component.name)
        .limit(120)
    )).all()
    catalog_txt = "\n".join(
        f"- {b} | {n[:60]}" + (f" | {v}{u or ''}" if v else "")
        for b, n, v, u in catalog_rows
    ) or "- (catalogue empty)"

    prompt = f"""Extract purchase orders from noisy copy-pasted order-history text.

This is an ELECTRONICS PARTS INVENTORY. The paste is from a general shopping
site, so most of it is not inventory: household goods, furniture, computer
accessories, cables, groceries, site chrome, adverts, "Sponsored" blocks,
"Recommended", "Browsing History", "Buy it again" suggestion carousels, and
footers. Those must be excluded.

RULES

1. ONE ENTRY PER ORDER. The paste may contain several orders, each with its own
   order number, date and total. Never merge them, never invent a reference like
   "Multiple". If an order number is not visible for a group of items, omit
   order_reference for that group rather than guessing.

2. is_inventory: true only for electronic parts, modules, dev boards, and
   workshop consumables that belong in a parts inventory. Set false for
   furniture, screen protectors, phone/laptop accessories, groceries, tools of
   daily life, and anything from a Sponsored/Recommended/Browsing-History
   section. When false, give a short reject_reason. Do not drop the line --
   return it marked false so a human can override.

3. PRICES. Only report unit_price or line_total if the number genuinely appears
   for that item. An order TOTAL is not an item price: if a $67.32 order holds
   five items, none of them cost $67.32. Leave prices out rather than
   apportioning, inventing, or copying the order total onto a line.

4. NAMES. Keep the detail that distinguishes near-identical items -- pack size,
   count, capacity, voltage, colour. "Pack of 200" and "Case of 1,000" are
   different products and must not collapse to the same name. Drop only the
   marketing padding.

5. type_path MUST be chosen from this exact list, or omitted if none fit.
   Never invent a new path:
{paths_txt}

6. MATCHING. If an item is plainly the SAME product as something already in the
   catalogue below, set match_barcode_id to that barcode and explain in
   match_reason. Be conservative: same function is not the same part. An
   "ESP32 C3 Mini" is NOT an "ESP32-DEVKITC-32E" -- different module. Only match
   when you are confident it is the identical part; otherwise leave
   match_barcode_id empty and a human will decide.

7. quantity is the number of units received. "10pcs" in a title with Qty 1 means
   quantity 10. Default 1 when unknown.

8. is_kit: true for an assortment/variety pack of MIXED values sold as one unit
   (a "150 Values 1500 Pieces Resistor Kit", a "12 Values 120 Pcs IC Kit").
   For a kit, quantity is the number of KITS bought -- almost always 1 -- not
   the piece count inside it. Put the piece count in notes instead. A pack of
   identical parts ("10Pcs Buzzer", "4-Pack Servo") is NOT a kit: is_kit=false
   and quantity is the piece count.

9. ONE PRODUCT PER LINE. A title bundling different products -- "2PCS
   STM32F103C8T6 Board + 1PCS ST-Link V2 Programmer" -- is two line items with
   their own quantities (2 and 1), not one line of 3. Split them.

EXISTING CATALOGUE (match against these):
{catalog_txt}

Links extracted from paste:
{urls_txt or '- none'}

Rich/HTML snippet:
{html_snippet or '[none]'}

Text:
{req.text[:18000]}"""

    raw = await _call_data_aware_ai(prompt, schema, task="order", collapse_list=False, db=db)
    result = _normalize_order_parse_result(raw, known_paths=set(known_paths))

    # A paste this long always contains SOMETHING. Zero orders means the model
    # failed -- truncated mid-JSON, or answered in a shape the normalizer could
    # not read -- and returning that quietly looks identical to "your page had
    # no orders". Fail loudly so the reviewer knows to retry or switch models.
    if not result["orders"] and len((req.text or "").strip()) > 200:
        raise HTTPException(
            502,
            "The model returned nothing readable for this paste. It may have been "
            "truncated. Try again, or switch the order model in Settings.",
        )

    # Resolve claimed matches against the real catalogue. A hallucinated barcode
    # must not reach the review table looking like a confirmed match.
    by_barcode = {b: (b, n) for b, n, _v, _u in catalog_rows}
    for order in result["orders"]:
        for item in order["items"]:
            bid = item.get("match_barcode_id")
            if not bid:
                continue
            hit = by_barcode.get(bid) or by_barcode.get(bid.upper())
            if hit:
                item["match_barcode_id"], item["match_name"] = hit
            else:
                item["match_barcode_id"] = None
                item["match_reason"] = None

    result["source"] = "order_parse_v2"
    return result


@router.get("/type-paths")
async def list_type_paths(db: AsyncSession = Depends(get_db)):
    """The vocabulary the order parser may file into, and which paths are in use."""
    in_use = {
        r[0]: r[1] for r in (await db.execute(
            select(Component.type_path, func.count(Component.id))
            .where(Component.type_path.isnot(None))
            .group_by(Component.type_path)
        )).all()
    }
    paths = await get_known_type_paths(db)
    return {
        "paths": [{"path": p, "component_count": in_use.get(p, 0)} for p in paths],
        "total": len(paths),
    }


class TypePathVocabRequest(BaseModel):
    paths: list[str]


@router.post("/type-paths")
async def set_type_paths(req: TypePathVocabRequest, db: AsyncSession = Depends(get_db)):
    """Replace the declared vocabulary. Paths already on components always remain."""
    cleaned = sorted({
        p.strip().strip("/") for p in req.paths
        if p and p.strip() and re.match(r"^[a-z0-9]+(?:[/-][a-z0-9]+)*$", p.strip().strip("/"))
    })

    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == TYPE_PATH_VOCAB_KEY)
    )).scalar_one_or_none()
    if row:
        row.value = json.dumps(cleaned)
    else:
        db.add(SystemSetting(key=TYPE_PATH_VOCAB_KEY, value=json.dumps(cleaned)))
    await db.flush()

    return {"saved": len(cleaned), "paths": await get_known_type_paths(db)}


@router.post("/merge")
async def merge_results(req: MergeRequest, db: AsyncSession = Depends(get_db)):
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

    result = await _call_data_aware_ai(prompt, MERGE_SCHEMA, task="merge", db=db)
    result["source"] = "gemini_merged"
    return result


@router.post("/classify")
async def classify_component(req: ParseRequest, db: AsyncSession = Depends(get_db)):
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

    return await _call_data_aware_ai(prompt, CLASSIFY_SCHEMA, task="classify", db=db)


@router.get("/sources")
async def list_ai_sources(limit: int = 20, db: AsyncSession = Depends(get_db)):
    sources = await get_ai_sources(db, limit=max(1, min(limit, 50)))
    return {"sources": sources}


@router.get("/provider-config")
async def get_provider_config(db: AsyncSession = Depends(get_db)):
    cfg = await _get_ai_provider_config(db)
    return {
        "strategy": cfg.get("strategy"),
        "task_model_preferences": cfg.get("task_model_preferences") or {},
        "providers": [_sanitize_provider_for_client(p) for p in (cfg.get("providers") or [])],
    }


@router.put("/provider-config")
async def update_provider_config(req: ProviderConfigUpdateRequest, db: AsyncSession = Depends(get_db)):
    normalized = await _set_ai_provider_config(db, req.model_dump())
    return {
        "ok": True,
        "strategy": normalized.get("strategy"),
        "task_model_preferences": normalized.get("task_model_preferences") or {},
        "providers": [_sanitize_provider_for_client(p) for p in (normalized.get("providers") or [])],
    }


@router.post("/provider-test")
async def provider_test(task: str = Form("assistant"), message: str = Form("ping"), db: AsyncSession = Depends(get_db)):
    task_name = (task or "assistant").strip().lower()
    if task_name not in set(AI_TASKS):
        raise HTTPException(400, f"Unsupported task: {task_name}")
    schema = {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["reply", "confidence"],
    }
    result = await _call_data_aware_ai(
        f"Task={task_name}. Reply in one line to: {message[:200]}",
        schema,
        task=task_name,
        db=db,
    )
    return {
        "ok": True,
        "task": task_name,
        "provider_id": result.get("provider_id"),
        "provider_type": result.get("provider_type"),
        "reply": result.get("reply", ""),
        "confidence": result.get("confidence", 0.0),
    }


@router.post("/references/text")
async def add_ai_reference(req: ReferenceTextRequest, db: AsyncSession = Depends(get_db)):
    text = (req.text or "").strip()
    if len(text) < 8:
        raise HTTPException(400, "Reference text too short")

    title = (req.title or "").strip() or f"{req.source_kind} reference"
    source = await upsert_ai_source(
        db,
        source_kind=(req.source_kind or "paste").strip() or "paste",
        title=title,
        raw_text=text,
        extracted_text=text,
        mime_type=req.mime_type,
        metadata={"page_context": req.page_context or {}},
    )
    return {"source": source}


@router.post("/references/upload")
async def add_ai_reference_upload(
    file: UploadFile = File(...),
    source_kind: str = Form("upload"),
    title: str = Form(None),
    page_context: str = Form("{}"),
    db: AsyncSession = Depends(get_db),
):
    payload = await file.read()
    extracted = extract_text_from_upload(file.filename, file.content_type, payload)
    if not extracted:
        extracted = ""

    safe_title = (title or file.filename or "uploaded reference").strip()
    storage_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', file.filename or 'upload')}"
    storage_path = AI_SOURCE_DIR / storage_name
    try:
        storage_path.write_bytes(payload)
    except Exception:
        storage_path = None

    try:
        parsed_context = json.loads(page_context) if page_context else {}
    except Exception:
        parsed_context = {}

    source = await upsert_ai_source(
        db,
        source_kind=(source_kind or "upload").strip() or "upload",
        title=safe_title,
        raw_text=extracted or "",
        extracted_text=extracted or "",
        mime_type=file.content_type,
        storage_path=str(storage_path) if storage_path else None,
        metadata={"filename": file.filename, "page_context": parsed_context},
    )
    source["extracted_chars"] = len(extracted or "")
    source["stored_bytes"] = len(payload)
    return {"source": source}


@router.post("/context-pack")
async def build_context_pack(
    message: str = Form(""),
    source_ids: str = Form("[]"),
    page_context: str = Form("{}"),
    db: AsyncSession = Depends(get_db),
):
    try:
        parsed_ids = json.loads(source_ids) if source_ids else []
    except Exception:
        parsed_ids = []
    try:
        parsed_context = json.loads(page_context) if page_context else {}
    except Exception:
        parsed_context = {}
    pack = await build_workspace_snapshot(db, message=message, page_context=parsed_context, source_ids=parsed_ids)
    return pack


@router.post("/assistant")
async def ai_assistant(req: AssistantRequest, db: AsyncSession = Depends(get_db)):
    if not req.message or len(req.message.strip()) < 3:
        raise HTTPException(400, "Message too short")

    workspace = await build_workspace_snapshot(
        db,
        message=req.context_query or req.message,
        page_context=req.page_context or {},
        source_ids=req.source_ids,
    )
    prompt = f"""You are the lab inventory assistant.
You have access to current workspace context, recent reference sources, and a compact snapshot of the database.
Use the context to answer concretely.

User request:
{req.message[:12000]}

Workspace snapshot:
{json.dumps(workspace, ensure_ascii=False)[:12000]}

Return strict JSON with keys:
- reply: concise answer for the user
- confidence: 0.0 to 1.0
- actions: list of suggested actions or follow-ups
- queries: list of read-only search/query suggestions if more data is needed
- cleanup_steps: list of cleanup or normalization steps
- migration_sql: list of migration statements if schema changes are relevant
- source_ids_used: list of attachment/source ids used in the answer

If the request asks for data cleanup, schema cleanup, or migration work, prefer actionable steps and safe SQL snippets.
If the request is ambiguous, explain what current data you could verify and what remains uncertain.
"""

    schema = {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "confidence": {"type": "number"},
            "actions": {"type": "array", "items": {"type": "object"}},
            "queries": {"type": "array", "items": {"type": "object"}},
            "cleanup_steps": {"type": "array", "items": {"type": "object"}},
            "migration_sql": {"type": "array", "items": {"type": "string"}},
            "source_ids_used": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["reply", "confidence", "actions", "queries", "cleanup_steps", "migration_sql", "source_ids_used"],
    }

    result = await _call_data_aware_ai(prompt, schema, task="assistant", preferred_model=req.model, db=db)
    result.setdefault("source", "ai_assistant")
    result.setdefault("workspace", workspace)
    result.setdefault("source_ids_used", req.source_ids or [])
    return result


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
        "timeout_seconds": effective_timeout,
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
        "timeout_seconds": effective_timeout,
        "max_workers": GEMINI_MAX_WORKERS,
        "last_check": _last_model_check.isoformat() if _last_model_check else None,
        "strategy": "Auto-fallback on 429 rate limit errors",
    }
