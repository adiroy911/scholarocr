"""PDF page analysis, rendering and deterministic text-layer extraction (PyMuPDF)."""

from __future__ import annotations

import re
import statistics
import unicodedata
from typing import Any

from PIL import Image

from ..schemas import PageProfile

MATH_FONT_RE = re.compile(r"(CMMI|CMSY|CMEX|CMBSY|MSAM|MSBM|EUFM|EUSM|RSFS|STIX|Symbol|Math|LMMath|MTPro|Mathematica|TeXGyre.*Math)", re.I)
MONO_FONT_RE = re.compile(r"(Mono|Courier|Consolas|CMTT|LMMono|Menlo)", re.I)


def render_page(page, dpi: int) -> Image.Image:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _clean_char(ch: str) -> bool:
    if ch in "\n\t ":
        return True
    if ch == "�":
        return False
    cat = unicodedata.category(ch)
    return not (cat in ("Co", "Cs", "Cn") or (cat == "Cc"))


def profile_page(page) -> PageProfile:
    text = page.get_text("text") or ""
    stripped = "".join(text.split())
    chars = len(stripped)
    quality = (sum(_clean_char(c) for c in stripped) / chars) if chars else 0.0

    # Invisible text (render mode 3) is typically an existing OCR layer of unknown quality.
    invisible = visible = 0
    try:
        for span in page.get_texttrace():
            n = len(span.get("chars", ()))
            if span.get("type") == 3 or span.get("opacity", 1) == 0:
                invisible += n
            else:
                visible += n
    except Exception:
        visible = chars

    area = max(1.0, page.rect.width * page.rect.height)
    covered = 0.0
    try:
        for info in page.get_image_info():
            bb = info["bbox"]
            x0, y0 = max(bb[0], page.rect.x0), max(bb[1], page.rect.y0)
            x1, y1 = min(bb[2], page.rect.x1), min(bb[3], page.rect.y1)
            if x1 > x0 and y1 > y0:
                covered += (x1 - x0) * (y1 - y0)
    except Exception:
        pass
    coverage = min(1.0, covered / area)

    math_spans = 0
    try:
        d = page.get_text("dict")
        for b in d.get("blocks", []):
            for line in b.get("lines", []):
                for s in line.get("spans", []):
                    if MATH_FONT_RE.search(s.get("font", "")) and s.get("text", "").strip():
                        math_spans += 1
    except Exception:
        pass

    try:
        drawings = len(page.get_drawings())
    except Exception:
        drawings = 0

    visible_chars = chars if invisible == 0 else max(0, chars - invisible)
    has_text = visible_chars >= 1 and visible > 0
    is_scanned = coverage >= 0.5 and (visible < 20)
    is_blank = chars == 0 and coverage == 0.0 and drawings == 0
    return PageProfile(
        has_text_layer=has_text,
        text_chars=visible_chars,
        text_quality=round(quality, 4),
        image_coverage=round(coverage, 4),
        math_font_signals=math_spans,
        vector_drawings=drawings,
        is_scanned=is_scanned,
        is_blank=is_blank,
    )


# ---------------------------------------------------------------------------
# Deterministic text-layer extraction
# ---------------------------------------------------------------------------

BULLET_RE = re.compile(r"^\s*([•●▪–\-*•]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s+")


def extract_text_blocks(page) -> list[dict[str, Any]]:
    """Return text blocks in reading order with font statistics.

    Each block: {"bbox": (x0,y0,x1,y1), "text": str, "lines": [str], "size": float,
                 "bold": bool, "italic": bool, "mono": bool, "font": str}
    """
    d = page.get_text("dict", sort=True)
    blocks: list[dict[str, Any]] = []
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        lines_text: list[str] = []
        sizes: list[tuple[float, int]] = []
        bold = italic = mono = True
        fonts: dict[str, int] = {}
        for line in b.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text")]
            t = "".join(s["text"] for s in spans).strip()
            if not t:
                continue
            lines_text.append(t)
            for s in spans:
                n = len(s["text"].strip())
                if not n:
                    continue
                sizes.append((s.get("size", 0.0), n))
                flags = s.get("flags", 0)
                font = s.get("font", "")
                fonts[font] = fonts.get(font, 0) + n
                bold &= bool(flags & 16) or "bold" in font.lower()
                italic &= bool(flags & 2) or "italic" in font.lower()
                mono &= bool(flags & 8) or bool(MONO_FONT_RE.search(font))
        if not lines_text:
            continue
        total = sum(n for _, n in sizes) or 1
        size = sum(sz * n for sz, n in sizes) / total
        blocks.append({
            "bbox": tuple(b["bbox"]),
            "lines": lines_text,
            "text": _join_lines(lines_text),
            "size": round(size, 2),
            "bold": bold,
            "italic": italic,
            "mono": mono,
            "font": max(fonts, key=fonts.get) if fonts else "",
        })
    return _column_order(blocks, page.rect.width)


def _join_lines(lines: list[str]) -> str:
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif out.endswith("-") and ln[:1].islower():
            out = out[:-1] + ln  # de-hyphenate soft line-break hyphenation
        else:
            out += " " + ln
    return out


def _column_order(blocks: list[dict], page_width: float) -> list[dict]:
    """Recursive XY-cut reading order.

    Split into horizontal slabs at vertical whitespace gaps larger than ~1.2 body lines; inside a
    slab, split into columns at a vertical whitespace gap when both sides are reasonably wide
    (so a narrow column of question numbers is not treated as a separate text column).
    """
    if len(blocks) < 2:
        return blocks
    body = body_font_size(blocks)
    return _xy_cut(blocks, page_width, h_gap=max(12.0, 1.2 * body), depth=0)


def _gaps(intervals: list[tuple[float, float]], min_gap: float) -> list[float]:
    """Cut positions in the middle of gaps >= min_gap between merged intervals."""
    iv = sorted(intervals)
    cuts, cur_end = [], iv[0][1]
    for a, b in iv[1:]:
        if a - cur_end >= min_gap:
            cuts.append((a + cur_end) / 2)
        cur_end = max(cur_end, b)
    return cuts


def _xy_cut(blocks: list[dict], page_width: float, h_gap: float, depth: int) -> list[dict]:
    if len(blocks) <= 1 or depth > 12:
        return sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))
    ycuts = _gaps([(b["bbox"][1], b["bbox"][3]) for b in blocks], h_gap)
    if ycuts:
        slabs: list[list[dict]] = [[] for _ in range(len(ycuts) + 1)]
        for b in blocks:
            cy = (b["bbox"][1] + b["bbox"][3]) / 2
            slabs[sum(cy > c for c in ycuts)].append(b)
        return [x for slab in slabs if slab for x in _xy_cut(slab, page_width, h_gap, depth + 1)]
    xcuts = _gaps([(b["bbox"][0], b["bbox"][2]) for b in blocks], 8.0)
    for xc in xcuts:
        left = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 < xc]
        right = [b for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 >= xc]
        lw = max(b["bbox"][2] for b in left) - min(b["bbox"][0] for b in left)
        rw = max(b["bbox"][2] for b in right) - min(b["bbox"][0] for b in right)
        if lw >= 0.25 * page_width and rw >= 0.25 * page_width:
            return _xy_cut(left, page_width, h_gap, depth + 1) + _xy_cut(right, page_width, h_gap, depth + 1)
    return sorted(blocks, key=lambda b: (round(b["bbox"][1] / 3), b["bbox"][0]))


def body_font_size(blocks: list[dict]) -> float:
    weighted: list[float] = []
    for b in blocks:
        weighted += [b["size"]] * min(len(b["text"]), 2000)
    return statistics.median(weighted) if weighted else 10.0


def drawing_clusters(page, min_area_fraction: float = 0.02) -> list[tuple[float, float, float, float]]:
    """Bounding boxes of clustered vector drawings large enough to be figures/diagrams."""
    try:
        rects = page.cluster_drawings()
    except Exception:
        return []
    area = page.rect.width * page.rect.height
    return [tuple(r) for r in rects if r.width * r.height >= min_area_fraction * area and r.width > 30 and r.height > 30]


def page_image_blocks(page) -> list[tuple[float, float, float, float]]:
    out = []
    try:
        for info in page.get_image_info():
            bb = info["bbox"]
            if (bb[2] - bb[0]) > 20 and (bb[3] - bb[1]) > 20:
                out.append(tuple(bb))
    except Exception:
        pass
    return out
