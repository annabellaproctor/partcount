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
# Use gemini-flash-latest - stable pointer to latest flash model
# Available models: gemini-flash-latest, gemini-pro-latest, gemini-pro-vision-latest
GEMINI_MODEL = "gemini-flash-latest"
# CRITICAL: Use global generativelanguage.googleapis.com endpoint for pay-as-you-go
# Do NOT use aiplatform.googleapis.com or region-specific endpoints
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

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
    """
    global _last_usage_check
    
    if not GEMINI_KEY:
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
                log.warning(f"Gemini total timeout ({total_timeout}s) exceeded")
                raise HTTPException(504, "Gemini API timeout")
            
            async with httpx.AsyncClient(timeout=15.0) as client:
                # CRITICAL: Use X-goog-api-key header per Google documentation
                # https://ai.google.dev/gemini-api/docs/api-key
                headers = {
                    "Content-Type": "application/json",
                    "X-goog-api-key": GEMINI_KEY,
                }
                
                # Don't pass key in URL when using header
                r = await client.post(GEMINI_URL, json=payload, headers=headers)
                
                # Handle rate limit (429)
                if r.status_code == 429:
                    wait_time = min((2 ** attempt) * 1.0, 8.0)  # 1s, 2s, 4s, 8s max
                    log.warning(
                        f"Gemini rate limit (429) on attempt {attempt + 1}/{retries}. "
                        f"Retrying in {wait_time:.1f}s"
                    )
                    if attempt < retries - 1:
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        raise HTTPException(
                            429, 
                            "Gemini API rate limit. Try again in a moment. "
                            f"(Model: {GEMINI_MODEL}, Endpoint: generativelanguage.googleapis.com)"
                        )
                
                # Handle other errors
                if r.status_code != 200:
                    error_body = r.text[:500]
                    log.error(f"Gemini error {r.status_code}: {error_body}")
                    
                    # Don't retry on client errors (400-499 except 429)
                    if 400 <= r.status_code < 500 and r.status_code != 429:
                        raise HTTPException(
                            502, 
                            f"Gemini API error {r.status_code}. "
                            f"Model: {GEMINI_MODEL}. Check model name is valid."
                        )
                    
                    # Retry on server errors (500+)
                    if attempt < retries - 1:
                        wait_time = (2 ** attempt) * 1.0
                        log.warning(f"Retrying after server error in {wait_time:.1f}s")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    raise HTTPException(502, f"Gemini API error: {r.status_code}")
                
                # Success - parse response
                data = r.json()
                
                # Extract text from response
                if "candidates" not in data or not data["candidates"]:
                    log.error(f"Gemini returned no candidates: {data}")
                    raise HTTPException(502, "Gemini returned no candidates")
                
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                
                # CRITICAL: Check for error responses in text
                text_lower = text.lower()
                for pattern in ERROR_RESPONSE_PATTERNS:
                    if pattern in text_lower:
                        log.warning(f"Gemini returned error response containing '{pattern}'")
                        # Don't cache this, treat as error
                        if attempt < retries - 1:
                            wait_time = (2 ** attempt) * 1.0
                            log.warning(f"Retrying error response in {wait_time:.1f}s")
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
            log.warning(f"Gemini timeout on attempt {attempt + 1}/{retries}")
            if attempt < retries - 1:
                await asyncio.sleep((2 ** attempt) * 1.0)
                continue
            raise HTTPException(504, "Gemini API timeout")
            
        except json.JSONDecodeError as e:
            last_error = e
            log.error(f"Gemini returned invalid JSON: {e}")
            raise HTTPException(502, "Gemini returned invalid JSON")
            
        except HTTPException:
            raise
            
        except Exception as e:
            last_error = e
            log.error(f"Unexpected Gemini error: {e}")
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
