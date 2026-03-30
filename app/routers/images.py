"""
Image search — DigiKey, Mouser, DuckDuckGo, Openverse, Wikimedia.
Multi-source with smart deduplication and Google Images-style grid layout.
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import httpx, os, shutil, io, logging, re, hashlib
from PIL import Image
from app.models.database import get_db
from app.models.models import Component

log = logging.getLogger("images")
IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")
router = APIRouter(prefix="/api/images", tags=["images"])
os.makedirs(f"{IMAGE_DIR}/components", exist_ok=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _img_hash(url: str) -> str:
    """Normalize URL for deduplication"""
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _add_result(results: list, url: str, thumb: str, title: str, source: str, width: int = None, height: int = None):
    """Add image if not duplicate"""
    h = _img_hash(url)
    if h not in {x.get("hash") for x in results}:
        results.append({
            "url": url,
            "thumb": thumb or url,
            "title": title,
            "source": source,
            "hash": h,
            "width": width or 300,
            "height": height or 300,
        })


@router.get("/search")
async def search_images(q: str, limit: int = 20):
    results = []
    errors = []

    # Source 1: Wikimedia Commons — most reliable for electronics
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrnamespace": "6",
                    "gsrsearch": f"filetype:bitmap {q}",
                    "gsrlimit": "20",
                    "prop": "imageinfo",
                    "iiprop": "url|thumburl|size",
                    "iiurlwidth": "300",
                    "format": "json",
                },
                headers={"User-Agent": UA},
            )
            if r.status_code == 200:
                data = r.json()
                pages = data.get("query", {}).get("pages", {})
                for page in pages.values():
                    ii = (page.get("imageinfo") or [{}])[0]
                    url = ii.get("url", "")
                    if url:
                        _add_result(
                            results,
                            url,
                            ii.get("thumburl") or url,
                            page.get("title", "").replace("File:", ""),
                            "wikimedia",
                            ii.get("thumbwidth", 300),
                            ii.get("thumbheight", 300),
                        )
                log.info(f"Wikimedia: found {len([r for r in results if r['source'] == 'wikimedia'])} images")
            else:
                errors.append(f"Wikimedia {r.status_code}")
    except Exception as e:
        errors.append(f"Wikimedia: {str(e)[:100]}")

    # Source 2: DigiKey API — if token available
    if len(results) < limit:
        try:
            dk_token = os.getenv("DIGIKEY_TOKEN")
            dk_client_id = os.getenv("DIGIKEY_CLIENT_ID")
            if dk_token and dk_client_id:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://api.digikey.com/products/v4/search/keyword",
                        params={"keywords": q, "limit": 10},
                        headers={
                            "Authorization": f"Bearer {dk_token}",
                            "X-DIGIKEY-Client-Id": dk_client_id,
                        },
                    )
                    if r.status_code == 200:
                        for product in r.json().get("Products", []):
                            img = product.get("PrimaryPhoto")
                            if img:
                                _add_result(results, img, img, product.get("ProductDescription", ""), "digikey")
                        log.info(f"DigiKey: found {len([r for r in results if r['source'] == 'digikey'])} images")
                    else:
                        errors.append(f"DigiKey {r.status_code}")
            else:
                log.debug("DigiKey token not configured")
        except Exception as e:
            errors.append(f"DigiKey: {str(e)[:100]}")

    # Source 3: Mouser scraping
    if len(results) < limit:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(
                    f"https://www.mouser.com/c/?q={q}",
                    headers={"User-Agent": UA},
                )
                if r.status_code == 200:
                    # Try multiple patterns
                    patterns = [
                        r'data-img="([^"]+)"',
                        r'src="(https://www\.mouser\.com/images/[^"]+)"',
                        r'<img[^>]+src="([^"]*mouser\.com/images[^"]+)"',
                    ]
                    for pattern in patterns:
                        for match in re.findall(pattern, r.text):
                            if "mouser.com/images/" in match and match not in {x["url"] for x in results}:
                                _add_result(results, match, match, "", "mouser")
                                if len(results) >= limit:
                                    break
                        if len(results) >= limit:
                            break
                    log.info(f"Mouser: found {len([r for r in results if r['source'] == 'mouser'])} images")
                else:
                    errors.append(f"Mouser {r.status_code}")
        except Exception as e:
            errors.append(f"Mouser: {str(e)[:100]}")

    # Source 4: Google Images proxy via SerpAPI (if configured)
    if len(results) < limit:
        serpapi_key = os.getenv("SERPAPI_KEY")
        if serpapi_key:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://serpapi.com/search.json",
                        params={
                            "engine": "google_images",
                            "q": f"{q} electronic component",
                            "api_key": serpapi_key,
                            "num": str(limit - len(results)),
                        },
                    )
                    if r.status_code == 200:
                        for item in r.json().get("images_results", []):
                            _add_result(
                                results,
                                item.get("original"),
                                item.get("thumbnail"),
                                item.get("title", ""),
                                "google",
                            )
                        log.info(f"Google: found {len([r for r in results if r['source'] == 'google'])} images")
            except Exception as e:
                errors.append(f"Google: {str(e)[:100]}")

    if errors:
        log.warning(f"Image search errors: {'; '.join(errors)}")
    
    log.info(f"Image search '{q}' returned {len(results)} total results")
    return results[:limit]


def _mechanical_bg_remove(src: str, dest: str) -> bool:
    """
    Mechanical background removal — no AI, just white threshold + contour detection.
    Fast, deterministic, no model downloads.
    """
    try:
        import numpy as np
        
        img = Image.open(src).convert("RGBA")
        data = np.array(img)
        
        # Extract RGB and alpha
        rgb = data[:, :, :3]
        alpha = data[:, :, 3]
        
        # White threshold — pixels with R,G,B all > 240 become transparent
        white_mask = (rgb[:, :, 0] > 240) & (rgb[:, :, 1] > 240) & (rgb[:, :, 2] > 240)
        
        # Also check for near-white (235-245 range) to catch gradients
        near_white = (rgb[:, :, 0] > 235) & (rgb[:, :, 1] > 235) & (rgb[:, :, 2] > 235)
        
        # Set alpha to 0 where white or near-white
        alpha[white_mask | near_white] = 0
        
        # Update alpha channel
        data[:, :, 3] = alpha
        
        # Create new image
        result = Image.fromarray(data, mode="RGBA")
        result.save(dest, "PNG")
        return True
    except Exception as e:
        log.warning(f"Mechanical bg removal: {e}")
        return False


def _smart_crop(img: Image.Image, padding_ratio: float = 0.05) -> tuple:
    """
    Returns suggested crop box (left, top, right, bottom) with uniform padding.
    Finds the content bounding box and adds proportional padding.
    """
    bbox = img.getbbox()
    if not bbox:
        return (0, 0, img.width, img.height)
    
    left, top, right, bottom = bbox
    width = right - left
    height = bottom - top
    
    # Add padding (5% of content size on each side)
    pad_x = int(width * padding_ratio)
    pad_y = int(height * padding_ratio)
    
    # Clamp to image bounds
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(img.width, right + pad_x)
    bottom = min(img.height, bottom + pad_y)
    
    return (left, top, right, bottom)


def _process(src: str, dest: str, remove_bg: bool = False, crop: tuple = None, rotate: int = 0):
    """
    Process image: optional BG removal, rotation, crop, save as PNG.
    crop: (left, top, right, bottom) or None for auto-crop with padding
    rotate: degrees clockwise (0, 90, 180, 270)
    """
    try:
        img = Image.open(src).convert("RGBA")
        
        # Rotate first if requested
        if rotate in (90, 180, 270):
            img = img.rotate(-rotate, expand=True)  # PIL rotates counter-clockwise
        
        # Remove background
        if remove_bg:
            # Save to temp, process, reload
            import uuid
            tmp = f"/tmp/labinv_bg_{uuid.uuid4().hex}.png"
            img.save(tmp, "PNG")
            if _mechanical_bg_remove(tmp, tmp):
                img = Image.open(tmp).convert("RGBA")
                os.unlink(tmp)
        
        # Crop
        if crop:
            img = img.crop(crop)
        else:
            # Auto-crop with padding
            bbox = _smart_crop(img)
            img = img.crop(bbox)
        
        img.save(dest, "PNG")
    except Exception as e:
        log.warning(f"_process error: {e}")
        shutil.copy(src, dest)


@router.post("/preview")
async def preview_image(
    component_id: str = Form(...),
    image_url: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Download image, return base64 preview + suggested crop coordinates.
    Does NOT save to disk yet.
    """
    result = await db.execute(select(Component).where(Component.id == component_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")
    
    import uuid, base64
    from PIL import Image
    
    tmp = f"/tmp/labinv_{uuid.uuid4().hex}.tmp"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as client:
            r = await client.get(image_url)
            if r.status_code != 200:
                raise HTTPException(400, f"Fetch failed: HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                f.write(r.content)
        
        # Load and get dimensions + suggested crop
        img = Image.open(tmp).convert("RGBA")
        crop_box = _smart_crop(img)
        
        # Return base64 preview + crop suggestion
        with open(tmp, "rb") as f:
            preview_b64 = base64.b64encode(f.read()).decode()
        
        os.unlink(tmp)
        return {
            "preview": f"data:image/png;base64,{preview_b64}",
            "width": img.width,
            "height": img.height,
            "suggested_crop": {
                "left": crop_box[0],
                "top": crop_box[1],
                "right": crop_box[2],
                "bottom": crop_box[3],
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(tmp):
            try: os.unlink(tmp)
            except: pass
        log.error(f"preview: {e}")
        raise HTTPException(500, str(e))


@router.post("/fetch")
async def fetch_and_save(
    component_id: str = Form(...),
    image_url: str = Form(...),
    remove_bg: bool = Form(False),
    rotate: int = Form(0),
    crop_left: int = Form(None),
    crop_top: int = Form(None),
    crop_right: int = Form(None),
    crop_bottom: int = Form(None),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Component).where(Component.id == component_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    # Use /tmp to avoid directory permission issues and path conflicts
    import uuid
    tmp = f"/tmp/labinv_{uuid.uuid4().hex}.tmp"
    dest_tmp = f"/tmp/labinv_{uuid.uuid4().hex}.png"
    dest = f"{IMAGE_DIR}/components/{comp.barcode_id}.png"
    
    crop = None
    if all(x is not None for x in [crop_left, crop_top, crop_right, crop_bottom]):
        crop = (crop_left, crop_top, crop_right, crop_bottom)
    
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": UA}) as client:
            r = await client.get(image_url)
            if r.status_code != 200:
                raise HTTPException(400, f"Fetch failed: HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                f.write(r.content)
        # Process to temp location first, then atomic move to prevent serving half-written files
        _process(tmp, dest_tmp, remove_bg=remove_bg, crop=crop, rotate=rotate)
        shutil.move(dest_tmp, dest)
        if os.path.exists(tmp):
            os.unlink(tmp)
        comp.image_path = f"/images/components/{comp.barcode_id}.png"
        await db.commit()
        return {"image_path": comp.image_path, "bg_removed": remove_bg}
    except HTTPException:
        raise
    except Exception as e:
        for f in [tmp, dest_tmp]:
            if os.path.exists(f):
                try: os.unlink(f)
                except: pass
        log.error(f"fetch_and_save: {e}")
        raise HTTPException(500, str(e))


@router.post("/upload/{component_id}")
async def upload_image(
    component_id: str,
    file: UploadFile = File(...),
    remove_bg: bool = Query(False),
    rotate: int = Query(0),
    crop_left: int = Query(None),
    crop_top: int = Query(None),
    crop_right: int = Query(None),
    crop_bottom: int = Query(None),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Component).where(Component.id == component_id))
    comp = result.scalar_one_or_none()
    if not comp:
        raise HTTPException(404, "Component not found")

    import uuid
    tmp = f"/tmp/labinv_{uuid.uuid4().hex}.tmp"
    dest_tmp = f"/tmp/labinv_{uuid.uuid4().hex}.png"
    dest = f"{IMAGE_DIR}/components/{comp.barcode_id}.png"
    
    crop = None
    if all(x is not None for x in [crop_left, crop_top, crop_right, crop_bottom]):
        crop = (crop_left, crop_top, crop_right, crop_bottom)
    
    try:
        with open(tmp, "wb") as f:
            shutil.copyfileobj(file.file, f)
        _process(tmp, dest_tmp, remove_bg=remove_bg, crop=crop, rotate=rotate)
        shutil.move(dest_tmp, dest)
        if os.path.exists(tmp):
            os.unlink(tmp)
        comp.image_path = f"/images/components/{comp.barcode_id}.png"
        await db.commit()
        return {"image_path": comp.image_path, "bg_removed": remove_bg}
    except Exception as e:
        for f in [tmp, dest_tmp]:
            if os.path.exists(f):
                try: os.unlink(f)
                except: pass
        log.error(f"upload_image: {e}")
        raise HTTPException(500, str(e))
