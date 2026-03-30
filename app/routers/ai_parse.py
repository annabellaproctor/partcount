"""
AI parsing via Gemini 2.0 Flash with response schema enforcement.
Used for: component data extraction, aggregate result merging, confidence scoring.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx, os, json, logging

log = logging.getLogger("ai_parse")
router = APIRouter(prefix="/api/ai", tags=["ai"])

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

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


async def _gemini(prompt: str, schema: dict) -> dict:
    if not GEMINI_KEY:
        raise HTTPException(503, "GEMINI_API_KEY not configured")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.1,
            "maxOutputTokens": 1024,
        }
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"{GEMINI_URL}?key={GEMINI_KEY}",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code != 200:
            log.error(f"Gemini error {r.status_code}: {r.text[:300]}")
            raise HTTPException(502, f"Gemini API error: {r.status_code}")
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)


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
