"""Small shared helpers (hashing, ids, time, page ranges)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from .exceptions import InputError


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def page_id(document_id: str, page_number: int) -> str:
    return f"{document_id}-p{page_number:04d}"


def element_id(page_id_: str, index: int) -> str:
    return f"{page_id_}-e{index:03d}"


def parse_page_range(spec: str | None, total: int) -> list[int]:
    """Parse "1-5,8,10-" into sorted 1-based page numbers within [1, total]."""
    if spec is None or str(spec).strip() in ("", "all", "*"):
        return list(range(1, total + 1))
    pages: set[int] = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", part)
        try:
            if m:
                start = int(m.group(1)) if m.group(1) else 1
                end = int(m.group(2)) if m.group(2) else total
            else:
                start = end = int(part)
        except ValueError:
            raise InputError(f"Invalid page range component: {part!r}") from None
        if start < 1 or end < start:
            raise InputError(f"Invalid page range component: {part!r}")
        if start > total:
            raise InputError(f"Page {start} is outside the document (1-{total})")
        pages.update(range(start, min(end, total) + 1))
    if not pages:
        raise InputError(f"Empty page range: {spec!r}")
    return sorted(pages)


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def compact_ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 5, 7, 8] -> "1-3, 5, 7-8"."""
    out: list[str] = []
    nums = sorted(set(numbers))
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)
