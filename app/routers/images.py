"""
Image search and management.
Sources:
  1. DigiKey CDN — PhotoUrl from DigiKey search results (passed directly, no request needed)
  2. Openverse API — free, no key, Creative Commons, works server-side
  3. Fallback: generic SVG icon from our own icon service
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import httpx, os, shutil, io, logging
from app.models.database import get_db
from app.models.models import Component

log = logging.getLogger("images")
IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")
router = APIRouter(prefix="/api/images", tags=["images"])

os.makedirs(f"{IMAGE_DIR}/components", exist_ok=True)

OPENVERSE_URL = "https://api.openverse.org/v1/images/"


@router.get("/search")
async def search_images(q: str, limit: int = 9):
    """
    Search for component images via Openverse (free, no key, CC licensed).
    For electronic components, also tries component-specific query variants.
    """
    results = []

    # Try Openverse — works reliably server-side
    try:
        queries = [
            f"{q} electronic component",
            f"{q} electronics",
            q,
        ]
        async with httpx.AsyncClient(timeout=10) as client:
            for query in queries:
                if len(results) >= limit:
                    break
                r = await client.get(
                    OPENVERSE_URL,
                    params={
                        "q": query,
                        "license_type": "commercial",
                        "page_size": limit,
                        "mature": "false",
                    },
                    headers={"User-Agent": "LabInventory/1.0 (educational project)"},
                )
                if r.status_code == 200:
                    data = r.json()
                    for item in data.get("results", []):
                        url = item.get("url", "")
                        if url and url not in [x["url"] for x in results]:
                            results.append({
                                "url": url,
                                "thumb": item.get("thumbnail", url),
                                "title": item.get("title", ""),
                                "source": "openverse",
                            })
                if results:
                    break
    except Exception as e:
        log.warning(f"Openverse search failed: {e}")

    # Always pad with Wikimedia commons search if short
    if len(results) < 3:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params={
                        "action": "query",
                        "generator": "search",
                        "gsrnamespace": "6",
                        "gsrsearch": f"filetype:bitmap {q} electronic",
                        "gsrlimit": limit,
                        "prop": "imageinfo",
                        "iiprop": "url|thumburl",
                        "iiurlwidth": 200,
                        "format": "json",
                    },
                )
                if r.status_code == 200:
                    pages = r.json().get("query", {}).get("pages", {})
                    for page in pages.values():
                        ii = page.get("imageinfo", [{}])[0]
                        url = ii.get("url", "")
                        if url and url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                            results.append({
                                "url": url,
                                "thumb": ii.get("thumburl", url),
                                "title": page.get("title", ""),
                                "source": "wikimedia",
                            })
        except Exception as e:
            log.warning(f"Wikimedia search failed: {e}")

    return results[:limit]


def _remove_background(src_path: str, dest_path: str) -> bool:
    try:
        from rembg import remove
        from PIL import Image
        with open(src_path, "rb") as f:
            data = f.read()
        result = remove(data)
        img = Image.open(io.BytesIO(result)).convert("RGBA")
        img.save(dest_path, "PNG")
        return True
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"rembg failed: {e}")

    # Fallback: near-white → transparent
    try:
        from PIL import Image
        img = Image.open(src_path).convert("RGBA")
        pixels = img.getdata()
        new_pixels = [
            (r, g, b, 0) if (r > 230 and g > 230 and b > 230) else (r, g, b, a)
            for r, g, b, a in pixels
        ]
        img.putdata(new_pixels)
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        img.save(dest_path, "PNG")
        return True
    except Exception as e:
        log.warning(f"Fallback bg removal failed: {e}")
        return False


def _process_image(src_path: str, dest_path: str, remove_bg: bool = False):
    try:
        from PIL import Image
        if remove_bg:
            success = _remove_background(src_path, dest_path)
            if not success:
                img = Image.open(src_path).convert("RGBA")
                img.save(dest_path, "PNG")
        else:
            img = Image.open(src_path).convert("RGBA")
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
            img.save(dest_path, "PNG")
    except Exception as e:
        log.warning(f"_process_image failed, copying as-is: {e}")
        shutil.copy(src_path, dest_path)


@router.post("/fetch")
async def fetch_and_save(
    component_id: str = Form(...),
    image_url: str = Form(...),
    remove_bg: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Component).where(Component.id == component_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    tmp_path = f"{IMAGE_DIR}/components/_tmp_{component_id}"
    dest = f"{IMAGE_DIR}/components/{comp.barcode_id}.png"

    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as client:
            r = await client.get(image_url)
            if r.status_code != 200:
                raise HTTPException(400, f"Could not fetch image: HTTP {r.status_code}")
            with open(tmp_path, "wb") as f:
                f.write(r.content)

        _process_image(tmp_path, dest, remove_bg=remove_bg)

        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

        comp.image_path = f"/images/components/{comp.barcode_id}.png"
        return {"image_path": comp.image_path, "bg_removed": remove_bg}

    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except: pass
        log.error(f"fetch_and_save: {e}")
        raise HTTPException(500, str(e))


@router.post("/upload/{component_id}")
async def upload_image(
    component_id: str,
    file: UploadFile = File(...),
    remove_bg: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Component).where(Component.id == component_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    tmp_path = f"{IMAGE_DIR}/components/_tmp_up_{component_id}"
    dest = f"{IMAGE_DIR}/components/{comp.barcode_id}.png"

    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        _process_image(tmp_path, dest, remove_bg=remove_bg)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        comp.image_path = f"/images/components/{comp.barcode_id}.png"
        return {"image_path": comp.image_path, "bg_removed": remove_bg}
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except: pass
        log.error(f"upload_image: {e}")
        raise HTTPException(500, str(e))
