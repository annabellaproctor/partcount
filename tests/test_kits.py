import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_create_kit_with_inline_components(client: AsyncClient):
    response = await client.post(
        "/api/kits/",
        json={
            "name": "Test Kit",
            "description": "Sample kit",
            "components": [
                {
                    "name": "10k Ohm Resistor",
                    "type_path": "passives/resistor/film",
                    "value": "10k",
                    "unit": "Ω",
                    "quantity": 10,
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["barcode_id"].startswith("K")
    assert payload["component_count"] >= 1


@pytest.mark.asyncio
async def test_ai_parse_kit(client: AsyncClient, monkeypatch):
    from app.routers import ai_parse

    async def fake_gemini(prompt, schema, retry_count=0):
        return {
            "is_kit": True,
            "name": "Parsed Kit",
            "confidence": 0.93,
            "kit_components": [
                {"name": "10k resistor", "type_path": "passives/resistor/film", "quantity": 10},
                {"name": "100nF capacitor", "type_path": "passives/capacitor/ceramic", "quantity": 5},
            ],
        }

    monkeypatch.setattr(ai_parse, "_gemini", fake_gemini)

    response = await client.post(
        "/api/ai/parse",
        json={"text": "Kit with 10x 10kΩ resistors and 5x 100nF capacitors"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["is_kit"] is True
    assert len(data["kit_components"]) == 2
