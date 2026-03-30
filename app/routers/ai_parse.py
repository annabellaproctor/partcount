"""
AI parsing via Gemini Flash - cheapest model with highest free tier limits.
Used for: component data extraction, aggregate result merging, confidence scoring.
Free tier: 1500 RPD (15 RPM burst).

CRITICAL: Uses generativelanguage.googleapis.com (global endpoint) for pay-as-you-go.
Search grounding tool DISABLED (causes 429 errors due to undocumented quota).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx, os, json, logging, asyncio
from datetime import datetime, timedelta

log = logging.getLogger("ai_parse")
router = APIRouter(prefix="/api/ai", tags=["ai"])

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
# Use gemini-1.5-flash (documented stable model)
GEMINI_MODEL = "gemini-1.5-flash"
# CRITICAL: Must use v1beta for responseMimeType/responseSchema support
# v1 does NOT support these fields (400 error)
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# Failed request logging
_failed_requests = []
_max_failed_logs = 50

# Rate limit tracking - check every 30 minutes at :10 and :40 past the hour
_last_usage_check = None
_usage_check_interval = timedelta(minutes=30)

# Error response patterns that should never be cached
ERROR_RESPONSE_PATTERNS = [
    "i'm sorry",
    "i cannot",
    "i can't",
    "something went wrong",
    "an error occurred",
    "unable to",
    "failed to",
]


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

# JSON schema enforced by Gemini — no cleaning needed
COMPONENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "name":           {"type": "STRING"},
        "manufacturer":   {"type": "STRING"},
        "mpn":            {"type": "STRING"},
        "value":          {"type": "STRING"},
        "unit":           {"type": "STRING"},
        "package":        {"type": "STRING"},
        "voltage_rating": {"type": "NUMBER"},
        "tolerance":      {"type": "STRING"},
        "description":    {"type": "STRING"},
        "type":           {"type": "STRING",
                           "enum": ["resistor","capacitor","diode","transistor","mosfet",
                                    "ic","inductor","connector","relay","led","module",
                                    "sensor","crystal","fuse","switch","default"]},
        "datasheet_url":  {"type": "STRING"},
        "image_url":      {"type": "STRING"},
        "confidence":     {"type": "NUMBER"},
        "notes":          {"type": "STRING"},
    },
    "required": ["name", "type", "confidence"]
}

MERGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "name":           {"type": "STRING"},
        "manufacturer":   {"type": "STRING"},
        "mpn":            {"type": "STRING"},
        "value":          {"type": "STRING"},
        "unit":           {"type": "STRING"},
        "package":        {"type": "STRING"},
        "voltage_rating": {"type": "NUMBER"},
        "tolerance":      {"type": "STRING"},
        "description":    {"type": "STRING"},
        "type":           {"type": "STRING"},
        "datasheet_url":  {"type": "STRING"},
        "image_url":      {"type": "STRING"},
        "confidence":     {"type": "NUMBER"},
        "reasoning":      {"type": "STRING"},
    },
    "required": ["name", "confidence", "reasoning"]
}


async def _gemini(prompt: str, schema: dict, retries: int = 3) -> dict:
    """
    Call Gemini API with exponential backoff and proper error handling.
    
    CRITICAL:
    - Uses generativelanguage.googleapis.com (global endpoint)
    - X-goog-api-key header per Google documentation
    - NO search grounding tool (causes undocumented 429 quota errors)
    - Retries on 429 with exponential backoff
    - Never returns error responses (detects "I'm sorry", "I cannot", etc.)
    - Logs all failures for debugging
    """
    global _last_usage_check
    
    if not GEMINI_KEY:
        log_failed_request("no_api_key", {"message": "GEMINI_API_KEY not set"})
        raise HTTPException(503, "GEMINI_API_KEY not configured")
    
    # Check if we should log usage stats (every 30 min at :10 and :40 past hour)
    now = datetime.utcnow()
    if _last_usage_check is None or (now - _last_usage_check) > _usage_check_interval:
        _last_usage_check = now
        log.info(f"Gemini usage check due (every {_usage_check_interval.total_seconds()/60:.0f}min at :10 and :40)")
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.1,
            "maxOutputTokens": 1024,
        },
        # CRITICAL: Do NOT include tools (especially search grounding)
        # Search grounding causes 429 errors due to undocumented quota
    }
    
    last_error = None
    total_timeout = 30.0  # Max 30s for all retries
    start_time = asyncio.get_event_loop().time()
    
    for attempt in range(retries):
        try:
            # Check total timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > total_timeout:
                log_failed_request("timeout", {
                    "elapsed": elapsed,
                    "max_timeout": total_timeout,
                })
                raise HTTPException(504, "Gemini API timeout")
            
            async with httpx.AsyncClient(timeout=15.0) as client:
                # CRITICAL: Use X-goog-api-key header per Google documentation
                headers = {
                    "Content-Type": "application/json",
                    "X-goog-api-key": GEMINI_KEY,
                }
                
                log.debug(f"Calling Gemini: {GEMINI_URL} (attempt {attempt + 1}/{retries})")
                
                # Don't pass key in URL when using header
                r = await client.post(GEMINI_URL, json=payload, headers=headers)
                
                # Log full response for debugging
                response_body = r.text[:1000]
                
                # Handle rate limit (429)
                if r.status_code == 429:
                    wait_time = min((2 ** attempt) * 1.0, 8.0)
                    log_failed_request("rate_limit_429", {
                        "attempt": attempt + 1,
                        "model": GEMINI_MODEL,
                        "endpoint": GEMINI_URL,
                        "response_body": response_body,
                        "wait_time": wait_time,
                    })
                    
                    if attempt < retries - 1:
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        raise HTTPException(
                            429, 
                            f"Gemini API rate limit (429). Model: {GEMINI_MODEL}. "
                            f"This may indicate: (1) API key not valid, (2) Billing not enabled, "
                            f"(3) Model name incorrect. Check Google AI Studio for valid models."
                        )
                
                # Handle other errors
                if r.status_code != 200:
                    log_failed_request(f"http_{r.status_code}", {
                        "status": r.status_code,
                        "model": GEMINI_MODEL,
                        "endpoint": GEMINI_URL,
                        "response_body": response_body,
                        "headers": dict(r.headers),
                    })
                    
                    # Don't retry on client errors (400-499 except 429)
                    if 400 <= r.status_code < 500 and r.status_code != 429:
                        raise HTTPException(
                            502, 
                            f"Gemini API error {r.status_code}. "
                            f"Model: {GEMINI_MODEL}. Response: {response_body[:200]}"
                        )
                    
                    # Retry on server errors (500+)
                    if attempt < retries - 1:
                        wait_time = (2 ** attempt) * 1.0
                        await asyncio.sleep(wait_time)
                        continue
                    
                    raise HTTPException(502, f"Gemini API error: {r.status_code}")
                
                # Success - parse response
                data = r.json()
                
                # Extract text from response
                if "candidates" not in data or not data["candidates"]:
                    log_failed_request("no_candidates", {
                        "response_data": data,
                    })
                    raise HTTPException(502, "Gemini returned no candidates")
                
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                
                # CRITICAL: Check for error responses in text
                text_lower = text.lower()
                for pattern in ERROR_RESPONSE_PATTERNS:
                    if pattern in text_lower:
                        log_failed_request("error_response_pattern", {
                            "pattern": pattern,
                            "text_preview": text[:200],
                        })
                        
                        if attempt < retries - 1:
                            wait_time = (2 ** attempt) * 1.0
                            await asyncio.sleep(wait_time)
                            continue
                        raise HTTPException(502, "Gemini returned error response instead of valid data")
                
                result = json.loads(text)
                
                # Log success on retry
                if attempt > 0:
                    log.info(f"Gemini call succeeded on attempt {attempt + 1}")
                
                return result
                
        except httpx.TimeoutException as e:
            last_error = e
            log_failed_request("timeout_exception", {
                "attempt": attempt + 1,
                "error": str(e),
            })
            if attempt < retries - 1:
                await asyncio.sleep((2 ** attempt) * 1.0)
                continue
            raise HTTPException(504, "Gemini API timeout")
            
        except json.JSONDecodeError as e:
            last_error = e
            log_failed_request("json_decode_error", {
                "error": str(e),
                "text_preview": text[:200] if 'text' in locals() else None,
            })
            raise HTTPException(502, "Gemini returned invalid JSON")
            
        except HTTPException:
            raise
            
        except Exception as e:
            last_error = e
            log_failed_request("unexpected_error", {
                "attempt": attempt + 1,
                "error_type": type(e).__name__,
                "error": str(e),
            })
            if attempt < retries - 1:
                await asyncio.sleep((2 ** attempt) * 1.0)
                continue
            raise HTTPException(502, f"Gemini API error: {str(e)[:100]}")
    
    # Should never reach here
    raise HTTPException(502, "Gemini API failed after retries")


class ParseRequest(BaseModel):
    text: str


class MergeRequest(BaseModel):
    results: list[dict]
    query: str = ""


@router.post("/parse")
async def parse_component(req: ParseRequest):
    """
    Extract structured component data from any text.
    Amazon listing, datasheet snippet, product description, etc.
    """
    if not req.text or len(req.text.strip()) < 5:
        raise HTTPException(400, "Text too short")

    prompt = f"""You are an electronics component database assistant.
Extract structured component information from the following text.
Be precise. If a field is not present, omit it or use an empty string.
Set confidence 0.0-1.0 based on how certain you are of the extracted data.
For 'type', pick the best matching electronic component category.
For 'value', extract the electrical value (e.g. 10k, 100nF, 5V).
For 'unit', extract the SI unit (Ω, F, H, V, A, W, Hz).

Text:
{req.text[:4000]}"""

    result = await _gemini(prompt, COMPONENT_SCHEMA)
    result["source"] = "gemini"
    return result


@router.post("/merge")
async def merge_results(req: MergeRequest):
    """
    Given multiple lookup results from different sources (DigiKey, LCSC, etc.),
    use Gemini to intelligently merge them into the best single record.
    Resolves conflicts, picks best image, combines descriptions.
    """
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
    """
    Given a component name/description, return the best type classification
    and suggest barcode ID prefix.
    """
    CLASSIFY_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "type":        {"type": "STRING"},
            "prefix":      {"type": "STRING"},
            "confidence":  {"type": "NUMBER"},
            "reasoning":   {"type": "STRING"},
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
        "model": GEMINI_MODEL,
        "endpoint": GEMINI_URL,
    }
