"""
AI parsing via Gemini using official google-genai SDK.
FREE TIER (no billing): 
- gemini-flash-latest: 15 RPM, 1M TPM, 1500 RPD (stable pointer to latest)
Using gemini-flash-latest for best compatibility.
https://ai.google.dev/pricing
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import os, json, logging
from datetime import datetime, timedelta
from google import genai
from google.genai import types

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

# Rate limit tracking
_last_usage_check = None
_usage_check_interval = timedelta(minutes=30)


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
        "name":           {"type": "string"},
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
    },
    "required": ["name", "type", "confidence"]
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


async def _gemini(prompt: str, schema: dict) -> dict:
    """Call Gemini API using official SDK - Free tier optimized"""
    if not client:
        raise HTTPException(
            503, 
            "Gemini AI is not configured. Add GEMINI_API_KEY to .env (free tier: no billing required)"
        )
    
    try:
        # Use gemini-flash-latest - stable pointer to latest flash model
        # Free tier: 15 RPM, 1M TPM, 1500 RPD (no billing required)
        response = client.models.generate_content(
            model='gemini-flash-latest',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            )
        )
        
        # Parse JSON response
        result = json.loads(response.text)
        return result
        
    except Exception as e:
        error_msg = str(e)
        log_failed_request("sdk_error", {
            "error": error_msg,
            "type": type(e).__name__,
            "model": "gemini-flash-latest",
        })
        
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
                f"Gemini model not available. Using: gemini-flash-latest. "
                f"Error: {error_msg[:200]}"
            )
        
        raise HTTPException(502, f"Gemini API error: {error_msg[:200]}")


class ParseRequest(BaseModel):
    text: str


class MergeRequest(BaseModel):
    results: list[dict]
    query: str = ""


@router.post("/parse")
async def parse_component(req: ParseRequest):
    """Extract structured component data from any text."""
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
        "model": "gemini-flash-latest",
        "tier": "free (no billing)",
        "limits": "15 RPM, 1M TPM, 1500 RPD",
        "sdk": "google-genai",
    }
