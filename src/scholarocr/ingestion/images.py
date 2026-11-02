"""Raster image inputs (PNG/JPEG/TIFF incl. multi-page, WebP, BMP, GIF)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

from ..exceptions import CorruptDocumentError

DEFAULT_DPI = 300.0
PLACEHOLDER_DPI_MAX = 96.0  # assumed when an image carries no DPI metadata (typical scanner output)
Image.MAX_IMAGE_PIXELS = 300_000_000  # allow large scans, still guard against decompression bombs


def image_frame_count(path: Path) -> int:
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1)
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise CorruptDocumentError(f"{path.name}: damaged or unreadable image ({e.__class__.__name__})") from None


def load_frame(path: Path, index: int) -> tuple[Image.Image, float | None, bool]:
    """Return (RGB image with EXIF orientation applied, dpi or None, exif_rotated)."""
    with Image.open(path) as im:
        frame = None
        for i, f in enumerate(ImageSequence.Iterator(im)):
            if i == index:
                frame = f.copy()
                break
        if frame is None:
            raise CorruptDocumentError(f"{path.name}: frame {index} missing")
        dpi = im.info.get("dpi")
    exif_orientation = frame.getexif().get(0x0112, 1)
    rotated = exif_orientation not in (1, None)
    img = (ImageOps.exif_transpose(frame) if rotated else frame).convert("RGB")
    dpi_val = None
    if dpi:
        try:
            raw = float(dpi[0])
            dpi_val = (float(round(raw)) if abs(raw - round(raw)) < 0.1 else raw) if raw > 1 else None
            if dpi_val is not None and dpi_val <= PLACEHOLDER_DPI_MAX:
                dpi_val = None  # 72/96 dpi are screen/software defaults, not a physical scan resolution
        except (TypeError, ValueError):
            dpi_val = None
    return img, dpi_val, bool(rotated)
