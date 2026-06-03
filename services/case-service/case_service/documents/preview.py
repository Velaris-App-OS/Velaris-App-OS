"""Preview (thumbnail) generation — pypdfium2 + Pillow backend."""
from __future__ import annotations
import io
from typing import Optional

PREVIEW_SUPPORTED_TYPES = {
    "application/pdf",
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/webp", "image/bmp", "image/tiff",
}


def _is_supported(content_type: str) -> bool:
    return (content_type or "").lower().split(";")[0].strip() in PREVIEW_SUPPORTED_TYPES


def generate_preview(
    data: bytes,
    content_type: str,
    max_dim: int = 512,
) -> Optional[bytes]:
    """Generate a PNG thumbnail. Returns None for unsupported types or on error."""
    if not _is_supported(content_type):
        return None

    ct = content_type.lower().split(";")[0].strip()

    try:
        if ct == "application/pdf":
            return _pdf_preview(data, max_dim)
        else:
            return _image_preview(data, max_dim)
    except Exception:
        return None


def _pdf_preview(data: bytes, max_dim: int) -> Optional[bytes]:
    try:
        import pypdfium2
    except ImportError:
        return None

    pdf = pypdfium2.PdfDocument(data)
    if len(pdf) == 0:
        return None
    page = pdf[0]
    w, h = page.get_width(), page.get_height()
    scale = max_dim / max(w, h)
    bitmap = page.render(scale=scale)
    pil_image = bitmap.to_pil()
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return buf.getvalue()


def _image_preview(data: bytes, max_dim: int) -> Optional[bytes]:
    try:
        from PIL import Image
    except ImportError:
        return None

    img = Image.open(io.BytesIO(data))
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    # Ensure RGB for PNG output (handles RGBA, palette, etc.)
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
