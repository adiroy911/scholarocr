"""Per-user sandboxes and upload intake.

Layout under ``SCHOLAROCR_REMOTE_DATA_DIR``::

    users/<user_id>/inbox/<random>/<safe filename>   staged uploads (deleted right after ingestion)
    users/<user_id>/data/                            that user's ScholarOCR data_dir (db, documents, exports)

Nothing a caller sends is ever used as a path: filenames are reduced to a safe basename, the
staging directory is random, and the service layer's PathPolicy only allows the user's inbox.
File type is decided by magic bytes, never by the claimed name or MIME type.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import secrets
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

from ..exceptions import AccessDeniedError, InputError, LimitExceededError, UnsupportedFormatError
from ..ingestion.validation import detect_media_type
from .config import RemoteConfig

USER_ID_RE = re.compile(r"^u_[0-9a-f]{24}$|^local$|^op_[0-9a-f]{24}$")
EXT_FOR_TYPE = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                "image/tiff": ".tiff", "image/gif": ".gif", "image/bmp": ".bmp",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}


def safe_filename(name: str | None, fallback: str = "upload") -> str:
    """Basename only, conservative character set, bounded length. Never empty, never a dot-file."""
    raw = (name or "").replace("\\", "/").split("/")[-1]
    raw = raw.replace("\x00", "")
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", raw).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or set(cleaned) <= {"_", "."}:
        cleaned = fallback
    stem, dot, ext = cleaned.rpartition(".")
    if dot and len(ext) <= 5:
        return f"{stem[:100]}.{ext.lower()}"
    return cleaned[:100]


def dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


class Fetcher(Protocol):
    def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> tuple[bytes, str | None]:
        """Return (body, content-type). Must enforce ``max_bytes`` while streaming."""


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved
                or addr.is_unspecified or (addr.version == 6 and addr.ipv4_mapped is not None
                                           and not _is_public_ip(str(addr.ipv4_mapped))))


class HttpFetcher:
    """HTTPS-only download with SSRF guards: hostname allowlist (optional), every resolved address must
    be public, redirects followed manually (max 3) and re-checked, size enforced while streaming.

    Residual risk: DNS can change between the check and the connection (rebinding). Run the server
    with egress restricted to the public internet (see docs/CHATGPT_APP.md)."""

    def __init__(self, allowed_hosts: list[str] | None = None):
        self.allowed_hosts = [h.lower() for h in (allowed_hosts or [])]

    def _check_url(self, url: str) -> None:
        p = urlparse(url)
        if p.scheme != "https" or not p.hostname:
            raise AccessDeniedError("download_url must be an https URL")
        host = p.hostname.lower()
        if self.allowed_hosts and not any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
            raise AccessDeniedError(f"download host {host} is not in SCHOLAROCR_REMOTE_DOWNLOAD_HOSTS")
        try:
            infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise InputError(f"cannot resolve download host {host}") from None
        if not infos or not all(_is_public_ip(i[4][0]) for i in infos):
            raise AccessDeniedError("download_url resolves to a non-public address")

    def fetch(self, url: str, *, max_bytes: int, timeout_s: float) -> tuple[bytes, str | None]:
        import httpx

        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            for _ in range(4):
                self._check_url(url)
                with client.stream("GET", url, headers={"Accept": "*/*"}) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code != 200:
                        raise InputError(f"file download failed with HTTP {resp.status_code}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise LimitExceededError(_too_big(int(declared), max_bytes))
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise LimitExceededError(_too_big(len(buf), max_bytes))
                    return bytes(buf), resp.headers.get("content-type")
        raise InputError("too many redirects while downloading the file")


def _too_big(n: int, limit: int) -> str:
    return f"file is larger than the {limit / 1048576:.0f} MB limit (SCHOLAROCR_REMOTE_MAX_FILE_MB)"


@dataclass
class StagedFile:
    path: Path
    filename: str
    media_type: str
    size: int


class UserSandbox:
    def __init__(self, cfg: RemoteConfig, user_id: str):
        if not USER_ID_RE.match(user_id):
            raise AccessDeniedError("invalid user id")
        self.cfg = cfg
        self.user_id = user_id
        self.root = (cfg.users_dir / user_id).resolve()
        self.inbox = self.root / "inbox"
        self.data_dir = self.root / "data"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def usage_bytes(self) -> int:
        return dir_size(self.root)

    def new_batch(self) -> Path:
        d = self.inbox / secrets.token_hex(8)
        d.mkdir(parents=True)
        return d

    def stage(self, batch: Path, data: bytes, filename: str | None, index: int) -> StagedFile:
        """Write one upload into ``batch`` after size, quota and magic-byte type checks."""
        max_bytes = int(self.cfg.max_file_mb * 1048576)
        if len(data) == 0:
            raise InputError("uploaded file is empty")
        if len(data) > max_bytes:
            raise LimitExceededError(_too_big(len(data), max_bytes))
        quota = int(self.cfg.user_quota_mb * 1048576)
        if self.usage_bytes() + len(data) > quota:
            raise LimitExceededError(f"storage quota of {self.cfg.user_quota_mb:.0f} MB reached; delete documents "
                                     "or wait for retention cleanup")
        batch = batch.resolve()
        if not batch.is_relative_to(self.inbox):
            raise AccessDeniedError("invalid staging directory")
        name = safe_filename(filename, fallback=f"upload_{index + 1}")
        probe = batch / f".probe_{index}"
        probe.write_bytes(data[:64])
        try:
            # detect_media_type looks at magic bytes; the .docx suffix is only consulted for zip archives.
            if data[:4] == b"PK\x03\x04":
                probe = probe.rename(probe.with_suffix(".docx"))
                probe.write_bytes(data)
            media_type = detect_media_type(probe)
        except InputError:
            raise UnsupportedFormatError(f"{name}: not a supported document (PDF or image"
                                         f"{', DOCX' if self.cfg.allow_docx else ''})") from None
        finally:
            probe.unlink(missing_ok=True)
        if media_type not in self.cfg.allowed_types:
            raise UnsupportedFormatError(f"{name}: type {media_type} is not accepted by this server")
        ext = EXT_FOR_TYPE[media_type]
        if not name.lower().endswith(ext) and not (ext == ".jpg" and name.lower().endswith(".jpeg")) and not (
                ext == ".tiff" and name.lower().endswith(".tif")):
            name = f"{name.rsplit('.', 1)[0] if '.' in name else name}{ext}"
        target = batch / f"{index + 1:02d}_{name}"
        target.write_bytes(data)
        return StagedFile(target, name, media_type, len(data))

    @staticmethod
    def discard(batch: Path) -> None:
        shutil.rmtree(batch, ignore_errors=True)

    def sweep_inbox(self, older_than_s: float) -> int:
        """Remove staging batches left behind by crashed calls."""
        n = 0
        cutoff = time.time() - older_than_s
        for d in self.inbox.iterdir():
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
                    n += 1
            except OSError:
                pass
        return n


def decode_base64(data: str, max_bytes: int) -> bytes:
    if not isinstance(data, str) or not data:
        raise InputError("data_base64 is empty")
    s = data.strip()
    if s.startswith("data:") and "," in s[:200]:
        s = s.split(",", 1)[1]
    if len(s) > (max_bytes * 4) // 3 + 8:
        raise LimitExceededError(f"base64 payload exceeds {max_bytes / 1048576:.1f} MB "
                                 "(SCHOLAROCR_REMOTE_MAX_BASE64_MB); use a file upload instead")
    try:
        out = base64.b64decode(re.sub(r"\s+", "", s), validate=True)
    except (binascii.Error, ValueError):
        raise InputError("data_base64 is not valid base64") from None
    if len(out) > max_bytes:
        raise LimitExceededError(f"decoded file exceeds {max_bytes / 1048576:.1f} MB")
    return out
