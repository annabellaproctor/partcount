from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import httpx, os, uuid, shutil, re
from app.models.database import get_db
from app.models.models import Component
from app.services.barcode_svc import autocrop_image

IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")
router = APIRouter(prefix="/api/images", tags=["images"])


@router.get("/search")
async def search_images(q: str, limit: int = 9):
    """
    Searches DuckDuckGo image search (no API key) for component images.
    Returns list of {url, thumb, width, height} dicts.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LabInv/1.0)"}
        async with httpx.AsyncClient(timeout=8, headers=headers, follow_redirects=True) as client:
            # DDG image search via their API-lite endpoint
            r = await client.get(
                "https://duckduckgo.com/",
                params={"q": q, "iax": "images", "ia": "images"},
            )
            # extract vqd token
            vqd_match = re.search(r"vqd=['\"]([\d-]+)['\"]", r.text)
            if not vqd_match:
                return []
            vqd = vqd_match.group(1)

            r2 = await client.get(
                "https://duckduckgo.com/i.js",
                params={"l": "us-en", "o": "json", "q": q, "vqd": vqd, "f": ",,,,,", "p": "1"},
            )
            data = r2.json()
            results = data.get("results", [])[:limit]
            return [{"url": x.get("image"), "thumb": x.get("thumbnail"), "title": x.get("title", "")} for x in results]
    except Exception as e:
        return []


@router.post("/fetch")
async def fetch_and_save(
    component_id: str = Form(...),
    image_url: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Download image from URL, autocrop, save, link to component"""
    result = await db.execute(select(Component).where(Component.id == component_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404)

    ext = ".jpg"
    fname = f"{comp.barcode_id}{ext}"
    dest = f"{IMAGE_DIR}/components/{fname}"

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(image_url, headers={"User-Agent": "Mozilla/5.0"})
            with open(dest, "wb") as f:
                f.write(r.content)
        autocrop_image(dest)
        comp.image_path = f"/images/components/{fname}"
        return {"image_path": comp.image_path}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/upload/{component_id}")
async def upload_image(
    component_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Component).where(Component.id == component_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404)

    ext = os.path.splitext(file.filename)[1] or ".png"
    fname = f"{comp.barcode_id}{ext}"
    dest = f"{IMAGE_DIR}/components/{fname}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    autocrop_image(dest)
    comp.image_path = f"/images/components/{fname}"
    return {"image_path": comp.image_path}
