from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
import asyncio, httpx, os, shutil, re, io, logging
from app.models.database import get_db
from app.models.models import Component
from app.services.barcode_svc import autocrop_image

log = logging.getLogger("images")
IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")
router = APIRouter(prefix="/api/images", tags=["images"])

os.makedirs(f"{IMAGE_DIR}/components", exist_ok=True)


async def prewarm_rembg():
    """Run rembg import + model load off the event loop so the first real request is fast."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _prewarm_rembg_sync)
        log.info("rembg pre-warm complete")
    except Exception as e:
        log.warning(f"rembg pre-warm failed (non-fatal): {e}")


def _prewarm_rembg_sync():
    from rembg import remove
    from PIL import Image
    import io as _io
    # Feed a 1×1 white pixel to load the model into memory.
    img = Image.new("RGBA", (1, 1), (255, 255, 255, 255))
    buf = _io.BytesIO()
    img.save(buf, "PNG")
    remove(buf.getvalue())


# ── Image search ──────────────────────────────────────────────────────────────

# Ordered list of patterns tried against the DDG HTML response to extract vqd.
_VQD_PATTERNS = [
    re.compile(r'vqd=["\']?([\d-]+)["\']?'),
    re.compile(r'"vqd"\s*:\s*"([\d-]+)"'),
    re.compile(r'vqd=([\d-]+)'),
]


def _extract_vqd(html: str) -> Optional[str]:
    for pat in _VQD_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


@router.get("/search")
async def search_images(q: str, limit: int = 9):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=True) as client:
            r = await client.get("https://duckduckgo.com/", params={"q": q, "iax": "images", "ia": "images"})
            vqd = _extract_vqd(r.text)
            if not vqd:
                log.warning("DDG vqd not found in response")
                return []
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
    Runs synchronously — call via run_in_executor to avoid blocking the event loop.
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
        pass
    except Exception as e:
        log.warning(f"rembg failed: {e}")

    # Fallback: just autocrop whitespace
    try:
        from PIL import Image
        img = Image.open(src_path).convert("RGBA")
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
    """
    Full pipeline: download → optionally remove bg → autocrop → save as PNG.
    Runs synchronously — call via run_in_executor to avoid blocking the event loop.
    """
    try:
        from PIL import Image
        if remove_bg:
            _remove_background(src_path, dest_path)
        else:
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

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _process_image, tmp_path, dest, remove_bg)
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

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _process_image, tmp_path, dest, remove_bg)
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
