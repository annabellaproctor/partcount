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
async def search_images(q: str, limit: int = 20, db: AsyncSession = Depends(get_db)):
    from app.services.api_usage import track_api_call, check_rate_limit
    import time
    
    results = []
    errors = []

    # Source 1: Wikimedia Commons (always first, free, reliable)
    try:
        start = time.time()
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
            elapsed = int((time.time() - start) * 1000)
            
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
                log.info(f"Wikimedia: {len([r for r in results if r['source'] == 'wikimedia'])} images ({elapsed}ms)")
            else:
                errors.append(f"Wikimedia {r.status_code}")
    except Exception as e:
        errors.append(f"Wikimedia: {str(e)[:100]}")

    # Source 2: Mouser API
    if len(results) < limit:
        try:
            mouser_key = os.getenv("MOUSER_API_KEY")
            if mouser_key:
                allowed, reason = await check_rate_limit(db, "mouser")
                if allowed:
                    start = time.time()
                    async with httpx.AsyncClient(timeout=10) as client:
                        r = await client.get(
                            "https://api.mouser.com/api/v1/search/keyword",
                            params={"apiKey": mouser_key},
                            json={"SearchByKeywordRequest": {"keyword": q, "records": 10}},
                            headers={"Content-Type": "application/json"},
                        )
                        elapsed = int((time.time() - start) * 1000)
                        success = r.status_code == 200
                        
                        await track_api_call(db, "mouser", "search/keyword", success, None if success else str(r.status_code), elapsed)
                        
                        if success:
                            data = r.json()
                            for part in data.get("SearchResults", {}).get("Parts", []):
                                img = part.get("ImagePath")
                                if img:
                                    _add_result(results, img, img, part.get("Description", ""), "mouser")
                            log.info(f"Mouser API: {len([r for r in results if r['source'] == 'mouser'])} images ({elapsed}ms)")
                        else:
                            errors.append(f"Mouser API {r.status_code}")
                else:
                    errors.append(f"Mouser: {reason}")
        except Exception as e:
            await track_api_call(db, "mouser", "search/keyword", False, str(e)[:200], None)
            errors.append(f"Mouser API: {str(e)[:100]}")

    # Source 3: DigiKey API
    if len(results) < limit:
        try:
            dk_token = os.getenv("DIGIKEY_TOKEN")
            dk_client_id = os.getenv("DIGIKEY_CLIENT_ID")
            if dk_token and dk_client_id:
                allowed, reason = await check_rate_limit(db, "digikey")
                if allowed:
                    start = time.time()
                    async with httpx.AsyncClient(timeout=10) as client:
                        r = await client.get(
                            "https://api.digikey.com/products/v4/search/keyword",
                            params={"keywords": q, "limit": 10},
                            headers={
                                "Authorization": f"Bearer {dk_token}",
                                "X-DIGIKEY-Client-Id": dk_client_id,
                            },
                        )
                        elapsed = int((time.time() - start) * 1000)
                        success = r.status_code == 200
                        
                        await track_api_call(db, "digikey", "search/keyword", success, None if success else str(r.status_code), elapsed)
                        
                        if success:
                            for product in r.json().get("Products", []):
                                img = product.get("PrimaryPhoto")
                                if img:
                                    _add_result(results, img, img, product.get("ProductDescription", ""), "digikey")
                            log.info(f"DigiKey: {len([r for r in results if r['source'] == 'digikey'])} images ({elapsed}ms)")
                        else:
                            errors.append(f"DigiKey {r.status_code}")
                else:
                    errors.append(f"DigiKey: {reason}")
        except Exception as e:
            await track_api_call(db, "digikey", "search/keyword", False, str(e)[:200], None)
            errors.append(f"DigiKey: {str(e)[:100]}")

    # Source 4: Brave Search (image-specific search)
    if len(results) < limit:
        try:
            brave_key = os.getenv("BRAVE_API_KEY")
            if brave_key:
                allowed, reason = await check_rate_limit(db, "brave")
                if allowed:
                    start = time.time()
                    async with httpx.AsyncClient(timeout=10) as client:
                        r = await client.get(
                            "https://api.search.brave.com/res/v1/images/search",
                            params={"q": f"{q} electronic component", "count": 20},
                            headers={"Accept": "application/json", "X-Subscription-Token": brave_key},
                        )
                        elapsed = int((time.time() - start) * 1000)
                        success = r.status_code == 200
                        
                        await track_api_call(db, "brave", "images/search", success, None if success else str(r.status_code), elapsed)
                        
                        if success:
                            for item in r.json().get("results", [])[:limit - len(results)]:
                                _add_result(
                                    results,
                                    item.get("properties", {}).get("url"),
                                    item.get("thumbnail", {}).get("src"),
                                    item.get("title", ""),
                                    "brave",
                                )
                            log.info(f"Brave: {len([r for r in results if r['source'] == 'brave'])} images ({elapsed}ms)")
                        else:
                            errors.append(f"Brave {r.status_code}")
                else:
                    errors.append(f"Brave: {reason}")
        except Exception as e:
            await track_api_call(db, "brave", "images/search", False, str(e)[:200], None)
            errors.append(f"Brave: {str(e)[:100]}")

    # Source 5: Tavily (AI-powered search, good for finding product images)
    if len(results) < limit:
        try:
            tavily_key = os.getenv("TAVILY_API_KEY")
            if tavily_key:
                allowed, reason = await check_rate_limit(db, "tavily")
                if allowed:
                    start = time.time()
                    async with httpx.AsyncClient(timeout=10) as client:
                        r = await client.post(
                            "https://api.tavily.com/search",
                            json={
                                "api_key": tavily_key,
                                "query": f"{q} electronic component product image",
                                "search_depth": "basic",
                                "include_images": True,
                                "max_results": 5,
                            },
                        )
                        elapsed = int((time.time() - start) * 1000)
                        success = r.status_code == 200
                        
                        await track_api_call(db, "tavily", "search", success, None if success else str(r.status_code), elapsed)
                        
                        if success:
                            data = r.json()
                            for img_url in data.get("images", [])[:limit - len(results)]:
                                _add_result(results, img_url, img_url, "", "tavily")
                            log.info(f"Tavily: {len([r for r in results if r['source'] == 'tavily'])} images ({elapsed}ms)")
                        else:
                            errors.append(f"Tavily {r.status_code}")
                else:
                    errors.append(f"Tavily: {reason}")
        except Exception as e:
            await track_api_call(db, "tavily", "search", False, str(e)[:200], None)
            errors.append(f"Tavily: {str(e)[:100]}")

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
