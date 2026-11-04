"""Figure images: the full-resolution crop, a cleaned crop and an optional sanitised vector redraw.

For every figure / diagram / image element with a box, ``save_figure_images`` writes

* ``pages/NNNN/figures/<element_id>.png``: the original page render cropped to the (refined) box with a small
  margin, at full resolution. ``Element.source.region_image`` points at it. It is the authoritative copy and is
  never replaced.
* ``pages/NNNN/figures/<element_id>.clean.png`` (scanned pages only): paper and shadows whitened by dividing
  out the background estimate, ruled lines removed, only the pen strokes that belong to the figure kept (the
  components assigned to it by ``layout_refine``, so a neighbour's text that pokes into the rectangle is
  dropped) in their own colour, trimmed to the content. ``Element.figure.image_clean`` points at it.

An optional redraw (``<element_id>.svg``) is made only on request (``redraw_figure``); see ``svg_sanitize``.
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

from ..schemas import Element, FigureData, Page
from ..schemas.elements import VISUAL_TYPES
from ..storage import ArtifactStore
from ..storage.artifacts import atomic_write_bytes
from .layout_refine import PageInk, analyse_page

CROP_PAD_PT = 4.0
CLEAN_MARGIN_PX = 10
RESTORE_STEPS_PX = 40  # how far a stroke is followed along a removed rule
LEVEL_RUN_PX = 18  # a level run this long, coloured like a rule, is a stretch of rule inside a figure's stroke
CLEAN_KINDS_SKIPPED = {"photo", "photograph"}  # a cleaned (whitened) photo would destroy it


def figure_dir(store: ArtifactStore, document_id: str, page_number: int):
    return store.page_dir(document_id, page_number) / "figures"


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


def crop_box_px(el: Element, dpi: float, size: tuple[int, int], pad_pt: float = CROP_PAD_PT) -> tuple[int, int, int, int]:
    assert el.bbox is not None
    k = dpi / 72.0
    x0 = max(0, int((el.bbox.x0 - pad_pt) * k))
    y0 = max(0, int((el.bbox.y0 - pad_pt) * k))
    x1 = min(size[0], int((el.bbox.x1 + pad_pt) * k) + 1)
    y1 = min(size[1], int((el.bbox.y1 + pad_pt) * k) + 1)
    return x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)


def _pen_only(mask: np.ndarray, rules: np.ndarray, rule_coloured: np.ndarray, pen_tinted: np.ndarray) -> np.ndarray:
    """On a page whose pen colour differs from its rules: put back pen strokes the rule removal took with the rule
    (a wire drawn along a ruled line), following them from the strokes around through pixels tinted like the pen; and
    drop stretches of rule still joined to a stroke, which are long level runs coloured like a rule. A faint
    stroke at an angle or a small letter never forms such a run, so it stays although its colour is as pale."""
    along = (rules & pen_tinted).astype(np.uint8)
    kernel = np.ones((3, 3), np.uint8)
    grown = (mask & pen_tinted).astype(np.uint8)
    for _ in range(RESTORE_STEPS_PX):
        more = cv2.dilate(grown, kernel) & (along | grown)
        if (more == grown).all():
            break
        grown = more
    pale = (mask & rule_coloured).astype(np.uint8)
    level = cv2.morphologyEx(pale, cv2.MORPH_OPEN, np.ones((1, LEVEL_RUN_PX), np.uint8))
    level = (cv2.dilate(level, np.ones((3, 1), np.uint8)) > 0) & (pale > 0)
    kept = (mask & ~level) | (grown > 0)
    # a whole stroke with none of the pen's tint, thin and level, is a stretch of rule the tracing missed
    n, labels, stats, _ = cv2.connectedComponentsWithStats(kept.astype(np.uint8), connectivity=8)
    if n > 1:
        tinted = np.bincount(labels[kept & pen_tinted], minlength=n).astype(np.float64)
        w, h = stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]
        stray = (tinted < 0.05 * stats[:, cv2.CC_STAT_AREA]) & (w >= 6) & (w >= 2 * h)
        stray[0] = False
        kept &= ~stray[labels]
    return kept


def clean_figure(original: Image.Image, ink: PageInk, box: tuple[int, int, int, int],
                 components: list[int] | None) -> Image.Image | None:
    """Cleaned crop of ``box``: white paper, no ruled lines, pen strokes in their colour, trimmed. With
    ``components`` only those strokes are kept; otherwise all ink inside the box. None when no ink is left."""
    placed = clean_figure_placed(original, ink, box, components)
    return placed[0] if placed else None


def clean_figure_placed(original: Image.Image, ink: PageInk, box: tuple[int, int, int, int],
                        components: list[int] | None) -> tuple[Image.Image, tuple[int, int, int, int]] | None:
    """``clean_figure`` plus the pixel rectangle of the page the trimmed image covers."""
    x0, y0, x1, y1 = box
    rgb = np.asarray(original.convert("RGB"), dtype=np.float32)[y0:y1, x0:x1]
    paper = ink.paper_rgb[y0:y1, x0:x1]
    labels = ink.labels[y0:y1, x0:x1]
    if components:
        mask = np.isin(labels, np.asarray(components, dtype=np.int32))
    else:
        mask = ink.ink[y0:y1, x0:x1].copy()
    if not mask.any():
        return None
    if ink.rule_coloured is not None and ink.pen_tinted is not None:
        mask = _pen_only(mask, ink.rules[y0:y1, x0:x1], ink.rule_coloured[y0:y1, x0:x1], ink.pen_tinted[y0:y1, x0:x1])
        if not mask.any():
            return None
    # The strokes plus a two-pixel fringe (faint ballpoint is wider than its thresholded core), never the
    # ruled lines it touches; everything else is paper.
    soft = cv2.dilate(mask.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))) > 0
    soft &= ~ink.rules[y0:y1, x0:x1] | mask
    norm = np.clip(rgb / np.maximum(paper, 1.0) * 255.0, 0, 255)
    # Stretch so the darker strokes come out dark whatever the pen or the light: the 10th percentile of stroke
    # darkness maps to about 50.
    grey = norm.mean(axis=2)
    p10 = float(np.percentile(grey[mask], 10))
    gain = float(np.clip((255.0 - 50.0) / max(255.0 - p10, 1.0), 1.0, 3.0))
    norm = np.clip(255.0 - (255.0 - norm) * gain, 0, 255)
    soft &= grey < 238  # fringe pixels that are paper anyway stay white (no grey halo)
    out = np.full_like(norm, 255.0)
    out[soft] = norm[soft]
    ys, xs = np.nonzero(mask)
    t0, t1 = max(0, ys.min() - CLEAN_MARGIN_PX), min(mask.shape[0], ys.max() + 1 + CLEAN_MARGIN_PX)
    l0, l1 = max(0, xs.min() - CLEAN_MARGIN_PX), min(mask.shape[1], xs.max() + 1 + CLEAN_MARGIN_PX)
    return (Image.fromarray(out[t0:t1, l0:l1].astype(np.uint8), "RGB"),
            (x0 + int(l0), y0 + int(t0), x0 + int(l1), y0 + int(t1)))


def save_figure_images(store: ArtifactStore, page: Page, original: Image.Image, dpi: float,
                       ink: PageInk | None = None, *, clean: bool = True) -> list[str]:
    """Write the crop (and, with ``clean``, the cleaned crop) of every visual element of ``page``; set
    ``source.region_image`` and ``figure.image_clean``. Returns the element ids written."""
    visuals = [e for e in page.elements if e.type in VISUAL_TYPES and e.bbox is not None]
    if not visuals:
        return []
    out_dir = figure_dir(store, page.document_id, page.page_number)
    written = []
    for el in visuals:
        box = crop_box_px(el, dpi, original.size)
        path = out_dir / f"{el.element_id}.png"
        atomic_write_bytes(path, _png(original.crop(box)))
        el.source.region_image = store.rel(page.document_id, path)
        if el.figure is None:
            el.figure = FigureData()
        el.figure.image_clean = None
        el.figure.image_clean_box = None
        kind = (el.figure.kind or "").lower()
        if clean and kind not in CLEAN_KINDS_SKIPPED:
            if ink is None:
                ink = analyse_page(original, dpi)
            cleaned = clean_figure_placed(original, ink, box, ink.components_of(el.element_id) or None)
            if cleaned is not None:
                cpath = out_dir / f"{el.element_id}.clean.png"
                atomic_write_bytes(cpath, _png(cleaned[0]))
                el.figure.image_clean = store.rel(page.document_id, cpath)
                el.figure.image_clean_box = [round(v * 72.0 / dpi, 3) for v in cleaned[1]]
        written.append(el.element_id)
    return written
