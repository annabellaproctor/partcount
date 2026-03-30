from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import httpx, os, shutil, re, io, logging
from app.models.database import get_db
from app.models.models import Component
from app.services.barcode_svc import autocrop_image

log = logging.getLogger("images")
IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")
router = APIRouter(prefix="/api/images", tags=["images"])

os.makedirs(f"{IMAGE_DIR}/components", exist_ok=True)


# ── Image search ──────────────────────────────────────────────────────────────

@router.get("/search")
async def search_images(q: str, limit: int = 9):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as client:
            r = await client.get("https://duckduckgo.com/", params={"q": q, "iax": "images", "ia": "images"})
            vqd_match = re.search(r'vqd=["\']?([\d-]+)["\']?', r.text)
            if not vqd_match:
                log.warning("DDG vqd not found")
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
        log.warning(f"DDG image search failed: {e}")
        return []


# ── Background removal ────────────────────────────────────────────────────────

def _remove_background(src_path: str, dest_path: str):
    """
    Remove image background using rembg if available, fallback to white-edge crop.
    Output is always PNG with transparency.
    """
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
        # rembg not installed — fall back to autocrop only
        pass
    except Exception as e:
        log.warning(f"rembg failed: {e}")

    # Fallback: just autocrop whitespace
    try:
        from PIL import Image
        img = Image.open(src_path).convert("RGBA")
        # make near-white pixels transparent
        data = img.getdata()
        new_data = []
        for r, g, b, a in data:
            if r > 230 and g > 230 and b > 230:
                new_data.append((r, g, b, 0))
            else:
                new_data.append((r, g, b, a))
        img.putdata(new_data)
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        img.save(dest_path, "PNG")
        return True
    except Exception as e:
        log.warning(f"fallback bg removal failed: {e}")
        return False


def _process_image(src_path: str, dest_path: str, remove_bg: bool = False):
    """Full pipeline: download → optionally remove bg → autocrop → save as PNG."""
    try:
        from PIL import Image
        if remove_bg:
            _remove_background(src_path, dest_path)
        else:
            # just convert to PNG and autocrop
            img = Image.open(src_path).convert("RGBA")
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
            img.save(dest_path, "PNG")
    except Exception as e:
        log.warning(f"_process_image failed, using source: {e}")
        shutil.copy(src_path, dest_path)


# ── Fetch from URL ────────────────────────────────────────────────────────────

@router.post("/fetch")
async def fetch_and_save(
    component_id: str = Form(...),
    image_url: str = Form(...),
    remove_bg: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    """Download image from URL, optionally remove background, autocrop, save."""
    result = await db.execute(select(Component).where(Component.id == component_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    tmp_path = f"{IMAGE_DIR}/components/_tmp_{component_id}"
    dest = f"{IMAGE_DIR}/components/{comp.barcode_id}.png"

    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as client:
            r = await client.get(image_url)
            if r.status_code != 200:
                raise HTTPException(400, f"Could not fetch image: HTTP {r.status_code}")
            with open(tmp_path, "wb") as f:
                f.write(r.content)

        _process_image(tmp_path, dest, remove_bg=remove_bg)
        os.unlink(tmp_path)

        comp.image_path = f"/images/components/{comp.barcode_id}.png"
        return {"image_path": comp.image_path, "bg_removed": remove_bg}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"fetch_and_save error: {e}")
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        raise HTTPException(500, str(e))


# ── Upload from computer ──────────────────────────────────────────────────────

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

    tmp_path = f"{IMAGE_DIR}/components/_tmp_upload_{component_id}"
    dest = f"{IMAGE_DIR}/components/{comp.barcode_id}.png"

    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        _process_image(tmp_path, dest, remove_bg=remove_bg)
        os.unlink(tmp_path)

        comp.image_path = f"/images/components/{comp.barcode_id}.png"
        return {"image_path": comp.image_path, "bg_removed": remove_bg}
    except Exception as e:
        log.error(f"upload_image error: {e}")
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        raise HTTPException(500, str(e))
