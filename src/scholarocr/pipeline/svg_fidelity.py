"""Deterministic fidelity check of a redrawn figure (SVG) against the crop it was drawn from.

A redraw must be a faithful vector copy, not an interpretation. The SVG (already sanitised) is rasterised with
PyMuPDF at the crop's own pixel size, with its viewBox mapped onto the crop (the redraw brief asks for
``viewBox="0 0 W H"`` in the crop's pixels, so the geometry maps 1:1). Both images are binarised (the crop is
the cleaned one, measured relative to its paper level), and the ink is compared both ways with a distance
tolerance (chamfer-style, via distance transforms):

* ``recall``: share of the crop's ink within the tolerance of the redraw's ink (strokes the redraw kept);
* ``precision``: share of the redraw's ink within the tolerance of the crop's ink (nothing invented).

Regions where ink is missing or extra are reported as connected blobs with their position; any blob larger
than a small size fails the check on its own (a missing arrow is a few percent of the ink, but it is a missing
arrow). Every label in ``figure.labels`` must also appear as SVG text (normalised: LaTeX, sub/superscripts,
spaces and case ignored). The result is stored as ``figure.svg_fidelity``.
"""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass, field

import cv2
import numpy as np
from PIL import Image

from .layout_refine import INK_DELTA, _background, _normalise

MIN_RECALL = 0.85
MIN_PRECISION = 0.80
TOLERANCE_FRACTION = 0.012  # of the crop diagonal
MIN_TOLERANCE_PX = 4
BLOB_FRACTION = 0.03  # a missing or extra region larger than this (of the diagonal) fails the check by itself
MIN_BLOB_PX = 14
MAX_SIDE = 1400  # larger crops are compared at this size


@dataclass
class Region:
    x0: int
    y0: int
    x1: int
    y1: int
    where: str

    def describe(self) -> str:
        return f"{self.where} (x {self.x0}-{self.x1}, y {self.y0}-{self.y1})"


@dataclass
class Fidelity:
    score: float  # F1 of recall and precision, 0-1
    recall: float  # crop ink covered by the redraw
    precision: float  # redraw ink supported by the crop
    passed: bool
    tolerance_px: float
    size: tuple[int, int]  # the crop size = the expected viewBox
    labels_missing: list[str] = field(default_factory=list)
    missing_regions: list[Region] = field(default_factory=list)  # in the crop / viewBox coordinates
    extra_regions: list[Region] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["missing_regions"] = [r.describe() for r in self.missing_regions]
        d["extra_regions"] = [r.describe() for r in self.extra_regions]
        d["size"] = list(self.size)
        return d

    def feedback(self) -> list[str]:
        """Specific, actionable problems for a retry."""
        out = list(self.problems)
        for r in self.missing_regions[:6]:
            out.append(f"strokes of the original are missing from your SVG at {r.describe()}: draw them as they are")
        for r in self.extra_regions[:6]:
            out.append(f"your SVG has ink at {r.describe()} where the original has none: remove it or move it to "
                       "where it is drawn in the original")
        if self.labels_missing:
            out.append("labels missing as <text>: " + ", ".join(self.labels_missing[:12]))
        return out


# ---------------------------------------------------------------------------------------------- rasterising
def rasterise_svg(svg: str, size: tuple[int, int]) -> np.ndarray:
    """Ink mask (bool, ``size`` = (w, h)) of a sanitised SVG drawn onto a ``w x h`` canvas. The root's
    width/height are set to the canvas size, so the viewBox is mapped onto it as a browser would."""
    import pymupdf

    import xml.etree.ElementTree as ET

    w, h = size
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    root = ET.fromstring(svg)
    root.set("width", str(w))
    root.set("height", str(h))
    svg = ET.tostring(root, encoding="unicode")
    with pymupdf.open(stream=svg.encode("utf-8"), filetype="svg") as d:
        page = d[0]
        zoom_x, zoom_y = w / max(page.rect.width, 1e-6), h / max(page.rect.height, 1e-6)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom_x, zoom_y), alpha=False, colorspace=pymupdf.csGRAY)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    if arr.shape != (h, w):
        arr = cv2.resize(arr, (w, h), interpolation=cv2.INTER_AREA)
    return arr < 200


def reference_ink(img: Image.Image) -> np.ndarray:
    """Ink of the crop, relative to its paper level after dividing out the background (as box refinement does).
    The crop given to the model is the cleaned one, so ruled lines are already gone; rule detection is not run
    here because on a small crop a drawing's own long edges would look like rules."""
    gray = cv2.cvtColor(np.asarray(img.convert("RGB"), dtype=np.uint8), cv2.COLOR_RGB2GRAY)
    if float(np.median(gray)) < 110:
        gray = 255 - gray
    norm = _normalise(gray, _background(gray))
    ink = norm < float(np.percentile(norm, 90)) - INK_DELTA
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    specks = stats[:, cv2.CC_STAT_AREA] < 6
    specks[0] = False
    return ink & ~specks[labels]


# ---------------------------------------------------------------------------------------------- labels
_SUB_SUP = str.maketrans("₀₁₂₃₄₅₆₇₈₉⁰¹²³⁴⁵⁶⁷⁸⁹ₐₑₒₓᵢⱼₙₘₖₜⁿⁱˣ₊₋⁺⁻", "01234567890123456789aeoxijnmktnix+-+-")


def _norm_label(s: str) -> str:
    from ..export.base import latex_to_readable  # LaTeX -> readable text (\\theta_1 -> θ₁)

    s = latex_to_readable(s.replace("$", ""))
    s = unicodedata.normalize("NFKC", s.translate(_SUB_SUP))
    return "".join(c for c in s.casefold() if c.isalnum())


def missing_labels(labels: list[str], texts: list[str]) -> list[str]:
    joined = _norm_label("".join(texts))
    out = []
    for lab in labels:
        n = _norm_label(lab)
        if n and n not in joined:
            out.append(lab)
    return out


# ---------------------------------------------------------------------------------------------- comparison
def _where(x: float, y: float, w: int, h: int) -> str:
    v = "top" if y < h / 3 else "bottom" if y > 2 * h / 3 else "middle"
    hz = "left" if x < w / 3 else "right" if x > 2 * w / 3 else "centre"
    return "centre" if (v, hz) == ("middle", "centre") else f"{v} {hz}".replace("middle ", "")


def _blobs(mask: np.ndarray, min_extent: float, scale: float, w: int, h: int) -> list[Region]:
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if max(bw, bh) >= min_extent and area >= 0.15 * min_extent:
            x0, y0, x1, y1 = (int(round(v / scale)) for v in (x, y, x + bw, y + bh))
            out.append(Region(x0, y0, x1, y1, _where((x0 + x1) / 2, (y0 + y1) / 2, w, h)))
    out.sort(key=lambda r: -(r.x1 - r.x0) * (r.y1 - r.y0))
    return out


def check_fidelity(svg: str, svg_texts: list[str], reference: Image.Image, labels: list[str] | None = None
                   ) -> Fidelity:
    """Compare a sanitised SVG with the crop it was drawn from. See the module docstring."""
    w, h = reference.size
    scale = min(1.0, MAX_SIDE / max(w, h))
    cw, ch = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    ref = reference if scale == 1.0 else reference.resize((cw, ch), Image.Resampling.LANCZOS)
    a = reference_ink(ref)  # the original
    b = rasterise_svg(svg, (cw, ch))  # the redraw
    diag = float(np.hypot(cw, ch))
    tol = float(max(MIN_TOLERANCE_PX, TOLERANCE_FRACTION * diag))
    problems = []
    if not a.any():
        problems.append("the crop has no ink to compare with")
    if not b.any():
        problems.append("the SVG draws nothing visible at the crop size (check the viewBox: 0 0 "
                        f"{w} {h})")
    if problems:
        return Fidelity(0.0, 0.0, 0.0, False, tol / scale, (w, h), missing_labels(labels or [], svg_texts),
                        problems=problems)
    dist_to_b = cv2.distanceTransform((~b).astype(np.uint8), cv2.DIST_L2, 3)
    dist_to_a = cv2.distanceTransform((~a).astype(np.uint8), cv2.DIST_L2, 3)
    missing = a & (dist_to_b > tol)
    extra = b & (dist_to_a > tol)
    recall = float(1.0 - missing.sum() / max(int(a.sum()), 1))
    precision = float(1.0 - extra.sum() / max(int(b.sum()), 1))
    score = 0.0 if recall + precision == 0 else 2 * recall * precision / (recall + precision)
    min_extent = max(MIN_BLOB_PX * scale, BLOB_FRACTION * diag)
    miss_r = _blobs(missing, min_extent, scale, w, h)
    extra_r = _blobs(extra, min_extent, scale, w, h)
    labs = missing_labels(labels or [], svg_texts)
    if recall < MIN_RECALL:
        problems.append(f"only {recall:.0%} of the original's strokes are reproduced (need {MIN_RECALL:.0%})")
    if precision < MIN_PRECISION:
        problems.append(f"{1 - precision:.0%} of the SVG's ink is not in the original (allowed "
                        f"{1 - MIN_PRECISION:.0%})")
    passed = bool(recall >= MIN_RECALL and precision >= MIN_PRECISION and not miss_r and not extra_r and not labs)
    return Fidelity(round(score, 3), round(recall, 3), round(precision, 3), passed, round(tol / scale, 1), (w, h),
                    labs, miss_r, extra_r, problems)
