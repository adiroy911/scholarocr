"""Upload intake: files go into the app's own data directory, never to a path the browser chose.

Each upload gets a random id and folder (``uploads/<id>/<safe name>``). The type is decided by magic bytes
(PDF, PNG, JPEG, WebP, TIFF, GIF, BMP, DOCX); size and count limits come from the user's settings.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from ..exceptions import InputError, LimitExceededError, NotFoundError, UnsupportedFormatError
from ..ingestion.validation import detect_media_type
from ..remote.sandbox import EXT_FOR_TYPE, safe_filename

UPLOAD_ID_RE = re.compile(r"^u[0-9a-f]{20}$")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/tiff", "image/gif", "image/bmp"}


class Uploads:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, upload_id: str) -> Path:
        if not UPLOAD_ID_RE.match(upload_id or ""):
            raise InputError("invalid upload id")
        d = (self.root / upload_id).resolve()
        if not d.is_relative_to(self.root):
            raise InputError("invalid upload id")
        return d

    def save(self, data: bytes, filename: str | None, *, max_mb: float) -> dict[str, Any]:
        if not data:
            raise InputError("the file is empty")
        if len(data) > max_mb * 1048576:
            raise LimitExceededError(f"the file is larger than the {max_mb:g} MB limit (change it in Settings)")
        upload_id = "u" + secrets.token_hex(10)
        d = self.root / upload_id
        d.mkdir(parents=True)
        name = safe_filename(filename, fallback="upload")
        probe = d / (".probe.docx" if data[:4] == b"PK\x03\x04" else ".probe")
        try:
            probe.write_bytes(data)
            media_type = detect_media_type(probe)
        except InputError:
            shutil.rmtree(d, ignore_errors=True)
            raise UnsupportedFormatError(f"{name}: not a PDF, image or DOCX file") from None
        finally:
            probe.unlink(missing_ok=True)
        ext = EXT_FOR_TYPE.get(media_type)
        if ext is None:
            shutil.rmtree(d, ignore_errors=True)
            raise UnsupportedFormatError(f"{name}: type {media_type} is not supported")
        low = name.lower()
        if not (low.endswith(ext) or (ext == ".jpg" and low.endswith(".jpeg")) or (ext == ".tiff" and low.endswith(".tif"))):
            name = f"{name.rsplit('.', 1)[0] if '.' in name else name}{ext}"
        (d / name).write_bytes(data)
        meta = {"id": upload_id, "name": name, "type": media_type, "bytes": len(data), "created": time.time(),
                "kind": "image" if media_type in IMAGE_TYPES else ("pdf" if media_type == "application/pdf" else "docx")}
        (d / "upload.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta

    def get(self, upload_id: str) -> tuple[Path, dict[str, Any]]:
        d = self._dir(upload_id)
        try:
            meta = json.loads((d / "upload.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise NotFoundError(f"upload {upload_id} not found (it may have expired; attach the file again)") from None
        path = d / meta["name"]
        if not path.is_file():
            raise NotFoundError(f"upload {upload_id} not found")
        return path, meta

    def delete(self, upload_id: str) -> None:
        shutil.rmtree(self._dir(upload_id), ignore_errors=True)

    def sweep(self, older_than_s: float, now: float | None = None) -> int:
        cutoff = (now or time.time()) - older_than_s
        n = 0
        for d in self.root.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
            except OSError:
                pass
        return n
