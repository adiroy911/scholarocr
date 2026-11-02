"""File-type detection by magic bytes and integrity checks."""

from __future__ import annotations

import zipfile
from pathlib import Path

from ..exceptions import CorruptDocumentError, EncryptedDocumentError, UnsupportedFormatError

IMAGE_TYPES = {"image/png", "image/jpeg", "image/tiff", "image/webp", "image/bmp", "image/gif"}
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif", ".docx"}


def detect_media_type(path: Path) -> str:
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(b"%PDF-") or (b"%PDF-" in head):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head.startswith(b"PK\x03\x04") and path.suffix.lower() == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                if "word/document.xml" in z.namelist():
                    return DOCX_TYPE
        except zipfile.BadZipFile:
            raise CorruptDocumentError(f"{path.name}: damaged DOCX archive") from None
    if path.suffix.lower() == ".pdf":
        raise CorruptDocumentError(f"{path.name}: has a .pdf extension but no PDF header")
    raise UnsupportedFormatError(
        f"{path.name}: unsupported file type. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
    )


def open_pdf_checked(path: Path):
    """Open a PDF, raising typed errors for corrupt or password-protected files."""
    import pymupdf

    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as e:  # pymupdf raises FileDataError / RuntimeError variants
        raise CorruptDocumentError(f"{path.name}: cannot be opened as a PDF ({e.__class__.__name__})") from None
    if doc.needs_pass:
        doc.close()
        raise EncryptedDocumentError(
            f"{path.name}: is password-protected. Decrypt it first; ScholarOCR does not accept passwords."
        )
    if doc.page_count == 0:
        doc.close()
        raise CorruptDocumentError(f"{path.name}: PDF has no pages")
    if getattr(doc, "is_repaired", False):
        # Opened, but MuPDF had to repair the xref; record rather than fail.
        pass
    return doc
