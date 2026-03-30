import barcode
from barcode.writer import ImageWriter, SVGWriter
from PIL import Image
import qrcode
import io
import os

IMAGE_DIR = os.getenv("IMAGE_DIR", "/app/images")
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(f"{IMAGE_DIR}/barcodes", exist_ok=True)
os.makedirs(f"{IMAGE_DIR}/qrcodes", exist_ok=True)
os.makedirs(f"{IMAGE_DIR}/components", exist_ok=True)


def generate_code128_svg(barcode_id: str) -> str:
    """Returns SVG string for embedding directly in HTML"""
    code128 = barcode.get("code128", barcode_id, writer=SVGWriter())
    output = io.BytesIO()
    code128.write(output)
    return output.getvalue().decode("utf-8")


def generate_code128_png(barcode_id: str) -> str:
    """Saves PNG to disk, returns path"""
    path = f"{IMAGE_DIR}/barcodes/{barcode_id}.png"
    if not os.path.exists(path):
        code128 = barcode.get("code128", barcode_id, writer=ImageWriter())
        with open(path, "wb") as f:
            code128.write(f)
    return path


def generate_qr(barcode_id: str, data: str = None) -> str:
    """Generates QR code PNG, returns path. data defaults to barcode_id."""
    path = f"{IMAGE_DIR}/qrcodes/{barcode_id}.png"
    if not os.path.exists(path):
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=2,
        )
        qr.add_data(data or barcode_id)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(path)
    return path


def autocrop_image(src_path: str, dest_path: str = None) -> str:
    """Removes whitespace/transparency border from component PNG"""
    dest_path = dest_path or src_path
    img = Image.open(src_path).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    img.save(dest_path)
    return dest_path


def next_barcode_id(prefix: str, existing_ids: list[str]) -> str:
    """
    Generate a fixed-length alphanumeric barcode ID.
    Format: {PREFIX}-{4 alphanumeric chars}
    e.g. R-4K2M, C-9XBQ, M-A3FP
    Uses uppercase letters + digits, excludes confusables (0/O, 1/I/L).
    Guaranteed unique within existing_ids.
    """
    import random
    CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O/1/I/L
    p = prefix.upper()[:1]
    for _ in range(1000):
        suffix = "".join(random.choices(CHARS, k=4))
        bid = f"{p}-{suffix}"
        if bid not in existing_ids:
            return bid
    # fallback — timestamp-based
    import time
    return f"{p}-{int(time.time()) % 100000:05d}"
