"""
AI parsing endpoint using Claude Haiku via Anthropic API.
Extracts structured component data from arbitrary text (Amazon listings, datasheets, etc).
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx, os, json, logging

log = logging.getLogger("ai_parse")
router = APIRouter(prefix="/api/ai", tags=["ai"])

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")


class ParseRequest(BaseModel):
    text: str


@router.post("/parse")
async def parse_component(req: ParseRequest):
    """
    Send arbitrary text (Amazon listing, datasheet snippet, etc.) to Claude Haiku.
    Returns structured component fields.
    """
    if not req.text or len(req.text.strip()) < 5:
        raise HTTPException(400, "Text too short")

    if not ANTHROPIC_KEY:
        # return best-effort regex parse if no API key
        return _regex_fallback(req.text)

    prompt = f"""Extract electronic component data from this text. Return ONLY valid JSON with these fields (use null for unknown):

{{
  "name": "short part name or MPN",
  "manufacturer": "manufacturer name",
  "mpn": "manufacturer part number",
  "value": "electrical value (e.g. 10k, 100nF, 1N4148)",
  "unit": "Ω, F, H, V, A, etc",
  "package": "physical package (e.g. 0805, TO-92, DIP-8)",
  "voltage_rating": number or null,
  "tolerance": "e.g. 1%, 5%",
  "description": "one sentence description",
  "type": "resistor|capacitor|diode|transistor|mosfet|ic|inductor|connector|relay|led|module|sensor|crystal",
  "datasheet_url": "URL if present",
  "image_url": "URL of product image if present"
}}

Text to parse:
{req.text[:3000]}"""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if r.status_code != 200:
                log.error(f"Anthropic API error {r.status_code}: {r.text[:200]}")
                return _regex_fallback(req.text)

            content = r.json()["content"][0]["text"].strip()
            # strip markdown fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                content = content.rsplit("```", 1)[0]
            parsed = json.loads(content)
            parsed["source"] = "ai"
            return parsed
    except json.JSONDecodeError:
        log.warning("AI returned non-JSON, using regex fallback")
        return _regex_fallback(req.text)
    except Exception as e:
        log.error(f"AI parse error: {e}")
        return _regex_fallback(req.text)


def _regex_fallback(text: str) -> dict:
    """Best-effort extraction without AI."""
    import re
    result = {"source": "regex", "name": None, "manufacturer": None, "mpn": None,
              "value": None, "unit": None, "package": None, "voltage_rating": None,
              "tolerance": None, "description": None, "type": None,
              "datasheet_url": None, "image_url": None}

    # URLs
    urls = re.findall(r'https?://\S+', text)
    for url in urls:
        if any(x in url.lower() for x in ['pdf', 'datasheet']):
            result["datasheet_url"] = url
        elif any(x in url.lower() for x in ['jpg', 'jpeg', 'png', 'image', 'media', 'photo']):
            result["image_url"] = url

    # Resistance
    m = re.search(r'(\d+(?:\.\d+)?)\s*(k|M|m)?\s*[Ωohm]', text, re.IGNORECASE)
    if m:
        result["value"] = m.group(0).strip()
        result["unit"] = "Ω"
        result["type"] = "resistor"

    # Capacitance
    m = re.search(r'(\d+(?:\.\d+)?)\s*(p|n|u|µ|m)?F', text)
    if m:
        result["value"] = m.group(0).strip()
        result["unit"] = "F"
        result["type"] = "capacitor"

    # Package
    for pkg in ['0201','0402','0603','0805','1206','1210','SOT-23','TO-92','TO-220','DIP-8','DIP-16','SOIC','QFN','BGA']:
        if pkg.lower() in text.lower():
            result["package"] = pkg
            break

    # Tolerance
    m = re.search(r'[±±]?\s*(\d+(?:\.\d+)?)\s*%', text)
    if m:
        result["tolerance"] = m.group(0).strip()

    # Voltage
    m = re.search(r'(\d+(?:\.\d+)?)\s*V(?:DC|AC|olt)?', text, re.IGNORECASE)
    if m:
        try:
            result["voltage_rating"] = float(re.search(r'[\d.]+', m.group(0)).group())
        except Exception:
            pass

    # First line as name fallback
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if lines:
        result["name"] = lines[0][:80]

    return result
