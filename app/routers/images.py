"""
Image search — Openverse (no license filter) + Wikimedia Commons.
Both confirmed reachable from inside Docker.
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

UA = "LabInventory/1.0 (personal hobby project; github.com/p-sum/lab-inventory-tracker)"


@router.get("/search")
async def search_images(q: str, limit: int = 12):
    results = []

    # Source 1: Openverse — no license_type filter, it kills electronics results
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            for variant in [f"{q} electronic component", q]:
                r = await client.get(
                    "https://api.openverse.org/v1/images/",
                    params={"q": variant, "page_size": limit},
                    headers={"User-Agent": UA},
                )
                if r.status_code == 200:
                    for item in r.json().get("results", []):
                        url = item.get("url", "")
                        if url and url not in {x["url"] for x in results}:
                            results.append({
                                "url": url,
                                "thumb": item.get("thumbnail") or url,
                                "title": item.get("title", ""),
                                "source": "openverse",
                            })
                if results:
                    break
    except Exception as e:
        log.warning(f"Openverse failed: {e}")

    # Source 2: Wikimedia Commons — good for electronic component photos
    if len(results) < limit:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params={
                        "action": "query",
                        "generator": "search",
                        "gsrnamespace": "6",
                        "gsrsearch": f"filetype:bitmap {q} electronic component",
                        "gsrlimit": str(limit - len(results)),
                        "prop": "imageinfo",
                        "iiprop": "url|thumburl",
                        "iiurlwidth": "300",
                        "format": "json",
                    },
                    headers={"User-Agent": UA},
                )
                if r.status_code == 200:
                    pages = r.json().get("query", {}).get("pages", {})
                    for page in pages.values():
                        ii = (page.get("imageinfo") or [{}])[0]
                        url = ii.get("url", "")
                        if url and any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
                            results.append({
                                "url": url,
                                "thumb": ii.get("thumburl") or url,
                                "title": page.get("title", "").replace("File:", ""),
                                "source": "wikimedia",
                            })
        except Exception as e:
            log.warning(f"Wikimedia failed: {e}")

    return results[:limit]


def _remove_bg(src: str, dest: str) -> bool:
    try:
        from rembg import remove
        from PIL import Image
        with open(src, "rb") as f:
            result = remove(f.read())
        Image.open(io.BytesIO(result)).convert("RGBA").save(dest, "PNG")
        return True
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"rembg failed: {e}")
    try:
        from PIL import Image
        img = Image.open(src).convert("RGBA")
        pixels = list(img.getdata())
        img.putdata([(r, g, b, 0) if r > 230 and g > 230 and b > 230 else (r, g, b, a)
                     for r, g, b, a in pixels])
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        img.save(dest, "PNG")
        return True
    except Exception as e:
        log.warning(f"Fallback bg removal: {e}")
        return False


def _process(src: str, dest: str, remove_bg: bool = False):
    try:
        from PIL import Image
        if remove_bg:
            if not _remove_bg(src, dest):
                Image.open(src).convert("RGBA").save(dest, "PNG")
        else:
            img = Image.open(src).convert("RGBA")
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
            img.save(dest, "PNG")
    except Exception as e:
        log.warning(f"_process fallback copy: {e}")
        shutil.copy(src, dest)


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

    tmp = f"{IMAGE_DIR}/components/_tmp_{component_id}"
    dest = f"{IMAGE_DIR}/components/{comp.barcode_id}.png"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = await client.get(image_url)
            if r.status_code != 200:
                raise HTTPException(400, f"Fetch failed: HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                f.write(r.content)
        _process(tmp, dest, remove_bg=remove_bg)
        if os.path.exists(tmp):
            os.unlink(tmp)
        comp.image_path = f"/images/components/{comp.barcode_id}.png"
        return {"image_path": comp.image_path, "bg_removed": remove_bg}
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
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

    tmp = f"{IMAGE_DIR}/components/_tmp_up_{component_id}"
    dest = f"{IMAGE_DIR}/components/{comp.barcode_id}.png"
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f)
        _process(tmp, dest, remove_bg=remove_bg)
        if os.path.exists(tmp):
            os.unlink(tmp)
        comp.image_path = f"/images/components/{comp.barcode_id}.png"
        return {"image_path": comp.image_path, "bg_removed": remove_bg}
    except Exception as e:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except: pass
        log.error(f"upload_image: {e}")
        raise HTTPException(500, str(e))
