"""Deterministic box refinement: snap model-estimated boxes to the ink on the original page image.

Vision models place boxes by eye, so a ``model_estimate`` box is roughly right but often clips a descender,
includes a strip of empty paper, or cuts a diagram short. This module never calls a model. For one page it:

1. binarises the original render relative to the paper level (the same paper-relative idea as the blank check
   in ``ingestion/loader.py``), after dividing out a smooth background estimate so shadows and uneven lighting
   become paper too;
2. removes ruled notebook lines and margin rules: long thin horizontal / vertical runs found with morphological
   opening and chained across small gaps and slight slant (a run must cover a large part of the page to count),
   then re-bridges pen strokes that crossed a removed line;
3. labels the remaining ink as connected components;
4. assigns every component to at most one element, the one whose (slightly expanded) model box covers most of
   it; ties go to the box the component sits deepest in, then to the earlier element in reading order, so one
   box never swallows a neighbour's ink;
5. fits each box to its components. Figures additionally grow along nearby unassigned strokes and short labels
   (``T_A``, ``dx``, ``θ₁``) that belong to the drawing, but never into ink claimed by another element, never into
   a run of components that looks like a text line, and never beyond a growth cap.

When refinement is unreliable (no ink inside the box, or the fit would exceed the cap) the model box is kept.
Refined elements get ``bbox_source = "refined"`` and keep the model's box in ``bbox_model``; refinement is
idempotent because it always starts from ``bbox_model``. ``PageInk`` is returned so figure crops can keep only
the strokes that belong to the figure (``pipeline/figures.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

from ..ingestion.loader import BLANK_INK_DELTA
from ..logging_config import get_logger
from ..schemas import BBox, Element
from ..schemas.elements import VISUAL_TYPES

log = get_logger("layout_refine")

REFINABLE_SOURCES = {"model_estimate", "refined"}
INK_DELTA = BLANK_INK_DELTA  # grey levels below the (background-normalised) paper level that count as ink
RULE_MIN_FRACTION = 0.30  # a ruled line covers at least this fraction of the page width (height for margins)
RULE_SEGMENT_FRACTION = 0.05  # straight runs at least this long are line candidates
MAX_COMPONENT_FRACTION = 0.6  # components larger than this (of the page side) are never assigned
TEXT_GROW_CAP = 0.25  # a text box may grow by this fraction of its own size on each side ...
FIGURE_GROW_CAP = 0.35  # ... a figure box by this much (both also allow a few glyph heights)
ASSIGN_MIN_OVERLAP = 0.3  # share of a component's pixels inside a text element's (expanded) box to claim it
ASSIGN_MIN_OVERLAP_FIGURE = 0.25  # a drawing the model box clips still has a quarter of its strokes inside


@dataclass
class Refinement:
    element_id: str
    refined: bool
    reason: str  # "ok" or why the model box was kept
    components: list[int] = field(default_factory=list)


@dataclass
class PageInk:
    """Ink analysis of one page render (pixel coordinates of the original image)."""

    dpi: float
    width: int
    height: int
    ink: np.ndarray  # bool, ruled lines removed
    rules: np.ndarray  # bool, the removed ruled lines
    labels: np.ndarray  # int32 component label per pixel (0 = none)
    stats: np.ndarray  # (n, 5) x, y, w, h, area per label (row 0 = background)
    glyph_px: float  # typical glyph height
    paper_rgb: np.ndarray  # (h, w, 3) float32 background estimate, for cleaned crops
    results: dict[str, Refinement] = field(default_factory=dict)

    def components_of(self, element_id: str) -> list[int]:
        r = self.results.get(element_id)
        return list(r.components) if r else []

    def pt_to_px(self, v: float) -> float:
        return v * self.dpi / 72.0


# ---------------------------------------------------------------------------------------------- ink analysis
def _background(channel: np.ndarray) -> np.ndarray:
    """Smooth paper estimate: max filter (removes strokes) then median, at quarter resolution."""
    h, w = channel.shape
    small = cv2.resize(channel, (max(1, w // 4), max(1, h // 4)), interpolation=cv2.INTER_AREA)
    k = max(5, (min(small.shape) // 60) | 1)
    bg = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.medianBlur(bg, min(2 * k + 1, 99))
    return cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32)


def _normalise(gray: np.ndarray, bg: np.ndarray) -> np.ndarray:
    return np.clip(gray.astype(np.float32) / np.maximum(bg, 1.0) * 255.0, 0, 255).astype(np.uint8)


def _rule_mask(ink: np.ndarray, horizontal: bool, pen: np.ndarray | None = None) -> np.ndarray:
    """Long thin lines in one direction (ruled lines; margin rules with ``horizontal=False``).

    Seeds are straight runs of RULE_SEGMENT_FRACTION found by morphological opening, chained across small gaps
    and slight slant; a chain covering RULE_MIN_FRACTION of the page is a rule. Each rule is then traced column
    by column beyond its seeds (page curl bends lines near the edges). In each column the vertical ink run through
    the line is removed when it is about as thin as the line; a longer run is a pen stroke crossing the line and
    is kept, and so are pixels marked in ``pen`` (coloured like the pen, not like the rule)."""
    if not horizontal:
        return _rule_mask(ink.T, True, None if pen is None else pen.T).T
    h, w = ink.shape
    seg = max(15, int(w * RULE_SEGMENT_FRACTION))
    # Thicken vertically first, so a slightly slanted line (photographed page) still has straight runs.
    thick = cv2.dilate(ink.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)))
    runs = cv2.morphologyEx(thick, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (seg, 1)))
    runs &= ink.astype(np.uint8)
    out = np.zeros_like(ink)
    if not runs.any():
        return out
    gap = max(9, int(w * 0.03))
    chained = cv2.dilate(runs, cv2.getStructuringElement(cv2.MORPH_RECT, (gap, 7)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(chained, connectivity=8)
    thick_limit = 6 + 0.006 * max(h, w)  # per-column thickness of a ruled line, in pixels
    cols_t = np.ascontiguousarray(ink.T)  # column access without striding
    for i in range(1, n):
        x, y, cw, ch, _ = stats[i]
        if cw < RULE_MIN_FRACTION * w:
            continue
        sub = (labels[y:y + ch, x:x + cw] == i) & (runs[y:y + ch, x:x + cw] > 0)
        counts = sub.sum(axis=0)
        cols = np.flatnonzero(counts)
        if cols.size < 2 or float(np.median(counts[cols])) > thick_limit:
            continue
        fit = _robust_line(sub, x, y, w)
        if fit is None:
            continue
        fit_fn, inlier_x = fit
        fit_vals = fit_fn(np.arange(-1, w + 1, dtype=np.float64))  # fit_vals[cx + 1] = curve at column cx
        lo, hi = x + cols[0], x + cols[-1]
        centre = np.full(w, np.nan)
        start = int(np.median(inlier_x))
        # columns where the fit is backed by seed pixels nearby: there the trace may not drift from it
        hist = np.bincount(np.clip(inlier_x.astype(int), 0, w - 1), minlength=w).astype(np.float64)
        supported = np.convolve(hist, np.ones(61), mode="same") > 0
        _trace(cols_t, centre, start, float(fit_vals[start + 1]), -1, fit_vals, supported)
        _trace(cols_t, centre, start, float(fit_vals[start + 1]), +1, fit_vals, supported)
        gaps = np.isnan(centre[lo:hi + 1])  # inside the seeded span, where tracing lost the line: the fit
        centre[lo:hi + 1][gaps] = fit_vals[lo + 1:hi + 2][gaps]
        # the vertical ink run through the line in each column: the line itself, or a stroke crossing it
        xs = np.flatnonzero(~np.isnan(centre))
        spans = [_run_at(cols_t[cx], centre[cx]) for cx in xs]
        lengths = np.array([b - a + 1 if b >= a else np.nan for a, b in spans], dtype=np.float64)
        if np.isnan(lengths).all():
            continue
        # the line's own thickness, locally: it changes gradually (shadow, curl), a crossing stroke is a spike
        local = _sliding_nanmedian(lengths, 61)
        for idx, (cx, (a, b)) in enumerate(zip(xs, spans)):
            med = local[idx] if not np.isnan(local[idx]) else float(np.nanmedian(lengths))
            if b >= a and b - a + 1 > med * 1.6 + 2:
                continue  # a stroke crosses the line here: keep the column
            reach = int(np.ceil(med / 2 + 3))  # the band: the line plus its detached anti-aliased fringe
            c = int(round(centre[cx]))
            y0, y1 = max(0, c - reach), min(h, c + reach + 1)
            col = ink[y0:y1, cx].copy()
            # leave runs that continue beyond the band (a stroke touching the line from above or below)
            if col[0] and y0 > 0 and ink[y0 - 1, cx]:
                col[:np.argmin(col) if not col.all() else len(col)] = False
            if col[-1] and y1 < h and ink[y1, cx]:
                rev = col[::-1]
                rev[:np.argmin(rev) if not rev.all() else len(rev)] = False
                col = rev[::-1]
            if pen is not None:
                col &= ~pen[y0:y1, cx]
            out[y0:y1, cx] |= col
    return out


def _sliding_nanmedian(values: np.ndarray, window: int) -> np.ndarray:
    half = window // 2
    padded = np.pad(values, half, constant_values=np.nan)
    view = np.lib.stride_tricks.sliding_window_view(padded, window)
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmedian(view, axis=1)


def _robust_line(sub: np.ndarray, x0: int, y0: int, page_w: int):
    """Smooth curve through a rule's seed pixels, ignoring other long strokes chained to it (a drawing's
    edge running close to the line): polynomial fit with iterative outlier rejection. Each vertical run in each
    column is one point, so a thick stroke weighs no more than the thin rule beside it."""
    padded = np.pad(sub, ((1, 1), (0, 0))).astype(np.int8)
    d = np.diff(padded, axis=0)
    starts, ends = np.argwhere(d == 1), np.argwhere(d == -1)
    starts = starts[np.lexsort((starts[:, 0], starts[:, 1]))]
    ends = ends[np.lexsort((ends[:, 0], ends[:, 1]))]
    ys = (starts[:, 0] + ends[:, 0] - 1) / 2.0
    xs = starts[:, 1]
    if xs.size < 10:
        return None
    if xs.size > 6000:  # plenty for a curve; keeps the fit cheap
        pick = np.linspace(0, xs.size - 1, 6000).astype(int)
        ys, xs = ys[pick], xs[pick]
    xs = xs.astype(np.float64) + x0
    ys = ys.astype(np.float64) + y0
    deg = 3 if (xs.max() - xs.min()) > 0.6 * page_w else 2
    keep = np.ones(xs.size, dtype=bool)
    for _ in range(5):
        if keep.sum() < deg + 2:
            return None
        coef = np.polyfit(xs[keep], ys[keep], deg)
        resid = np.abs(ys - np.polyval(coef, xs))
        mad = float(np.median(resid[keep])) or 0.5
        new = resid <= max(3.0, 3.0 * mad)
        if (new == keep).all():
            break
        keep = new
    return (lambda xv: np.polyval(coef, xv)), xs[keep]


def _run_at(col: np.ndarray, c: float, radius: int = 3) -> tuple[int, int]:
    """(first, last) row of the ink run nearest to row ``c`` in one column; (0, -1) when there is none.
    ``col`` is a contiguous boolean column (a row of the transposed mask)."""
    h = col.shape[0]
    ci = int(round(c))
    start = -1
    for d in range(radius + 1):
        if 0 <= ci - d < h and col[ci - d]:
            start = ci - d
            break
        if d and 0 <= ci + d < h and col[ci + d]:
            start = ci + d
            break
    if start < 0:
        return 0, -1
    a = b = start
    while a > 0 and col[a - 1]:
        a -= 1
    while b < h - 1 and col[b + 1]:
        b += 1
    return a, b


def _trace(cols_t: np.ndarray, centre: np.ndarray, x: int, y: float, step: int, fit_vals: np.ndarray,
           supported: np.ndarray,
           radius: int = 4, max_miss: int = 40) -> None:
    """Follow a line from column ``x`` (row ``y``) in direction ``step``. In each column the ink run nearest to
    the predicted row (previous row plus the recent slope) within ``radius`` is taken; columns with no thin run
    there (a gap, or a stroke crossing the line) keep the prediction. Stops after ``max_miss`` columns without
    a confirmation and drops those unconfirmed columns. Where the fitted curve ``fit`` is ``supported`` by seed
    pixels nearby, a prediction more than 4 px off the curve (``fit_vals[cx + 1]``) is pulled back to it (the trace was following a
    drawing's edge); elsewhere (curled ends, faded stretches) the trace runs free."""
    w = cols_t.shape[0]
    slope = float(fit_vals[x + step + 1] - fit_vals[x + 1]) * step
    confirmed: list[tuple[int, float]] = []
    miss = 0
    while 0 <= x < w and miss < max_miss:
        pred = y + slope * step if confirmed else y
        if supported[x]:
            f = float(fit_vals[x + 1])
            if abs(pred - f) > 4:  # drifting along another stroke: back to the line the seeds describe
                pred = f
                slope = float(fit_vals[x + step + 1] - f) * step
        a, b = _run_at(cols_t[x], pred, radius)
        if b >= a and (b - a) <= 2 * radius + 2:
            y = (a + b) / 2.0
            miss = 0
            confirmed.append((x, y))
            if len(confirmed) >= 8:
                (x0, y0), (x1, y1) = confirmed[-8], confirmed[-1]
                if x1 != x0:
                    slope = float(np.clip((y1 - y0) / (x1 - x0) * step, -1.0, 1.0))
        else:
            y = pred
            miss += 1
        centre[x] = y
        x += step
    if miss:  # the last columns were never confirmed by ink
        end = x - step
        for cx in range(end, end - miss * step, -step):
            if 0 <= cx < w:
                centre[cx] = np.nan


def _pen_coloured(sat: np.ndarray, ink: np.ndarray, rules: np.ndarray) -> np.ndarray | None:
    """Ink clearly more saturated than the rules (a blue pen on grey ruling), or None when the pen colour is
    not distinct from the rules' colour on this page (then colour is no evidence either way)."""
    if rules.sum() < 200 or (ink & ~rules).sum() < 200:
        return None
    rule_sat = float(np.median(sat[rules]))
    pen_sat = float(np.median(sat[ink & ~rules]))
    if pen_sat - rule_sat < 30:
        return None
    return ink & (sat > (rule_sat + pen_sat) / 2 + 10)


def _dotted_rules(clean: np.ndarray) -> np.ndarray | None:
    """Faint ruled lines that binarise as a row of short dashes: thin horizontal dashes that line up (small
    vertical offsets, gaps under 8% of the width) across at least a quarter of the page. A drawing's own dashed
    line or ellipsis is far shorter and stays."""
    h, w = clean.shape
    n, labels, stats, cents = cv2.connectedComponentsWithStats(clean.astype(np.uint8), connectivity=8)
    if n <= 1:
        return None
    cw, ch = stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]
    dash = (ch <= 5) & (cw >= 3 * ch) & (cw <= 0.1 * w)
    dash[0] = False
    idx = np.flatnonzero(dash)
    if idx.size < 4:
        return None
    order = idx[np.argsort(cents[idx, 0])]
    parent = {int(i): int(i) for i in order}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    max_dx = 0.08 * w
    xs, ys = cents[order, 0], cents[order, 1]
    for a in range(len(order)):
        b = a + 1
        while b < len(order) and xs[b] - xs[a] <= max_dx:
            if abs(ys[b] - ys[a]) <= 4 + 0.03 * (xs[b] - xs[a]):
                parent[find(int(order[b]))] = find(int(order[a]))
            b += 1
    groups: dict[int, list[int]] = {}
    for i in order:
        groups.setdefault(find(int(i)), []).append(int(i))
    keep = np.zeros(n, dtype=bool)
    for members in groups.values():
        if len(members) < 4:
            continue
        x0 = min(stats[m, cv2.CC_STAT_LEFT] for m in members)
        x1 = max(stats[m, cv2.CC_STAT_LEFT] + stats[m, cv2.CC_STAT_WIDTH] for m in members)
        if x1 - x0 >= 0.25 * w:
            keep[members] = True
    return keep[labels] if keep.any() else None


def _drop_rule_remnants(clean: np.ndarray, rules: np.ndarray, pen: np.ndarray | None) -> np.ndarray:
    """Remove leftovers of removed rules: components lying almost entirely in the band around a rule (its
    anti-aliased fringe, short stretches the per-column test kept). Pen-coloured components stay."""
    band = cv2.dilate(rules.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    n, labels, stats, _ = cv2.connectedComponentsWithStats(clean.astype(np.uint8), connectivity=8)
    if n <= 1:
        return clean
    area = stats[:, cv2.CC_STAT_AREA].astype(np.float64)
    in_band = np.bincount(labels[band], minlength=n).astype(np.float64)
    w, h = stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]
    thin = np.minimum(w, h) <= 6
    elongated = np.maximum(w, h) >= 3 * np.maximum(np.minimum(w, h), 1)
    drop = (in_band >= 0.8 * area) & thin & elongated
    if pen is not None:
        pen_px = np.bincount(labels[pen & clean], minlength=n).astype(np.float64)
        drop &= pen_px < 0.3 * area
    drop[0] = False
    return clean & ~drop[labels]


def analyse_page(img: Image.Image, dpi: float) -> PageInk:
    rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    inverted = float(np.median(gray)) < 110  # light-on-dark page: measure it inverted (as the blank check)
    if inverted:
        gray = 255 - gray
    bg = _background(gray)
    norm = _normalise(gray, bg)
    paper = float(np.percentile(norm, 90))
    raw = norm < paper - INK_DELTA
    # rules are found on the raw ink: faint ruled lines are often only 1-2 px thick
    rules = _rule_mask(raw, True)
    rules |= _rule_mask(raw & ~rules, False)
    pen = _pen_coloured(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[:, :, 1], raw, rules)
    if pen is not None:  # a distinct pen colour: keep pen strokes that run along a rule
        rules &= ~pen
    clean = raw & ~rules  # speckle is removed below with the component size filter
    if rules.any():
        clean = _drop_rule_remnants(clean, rules, pen)
    dotted = _dotted_rules(clean)
    if dotted is not None:
        rules |= dotted
        clean &= ~dotted
    n, labels, stats, _ = cv2.connectedComponentsWithStats(clean.astype(np.uint8), connectivity=8)
    scale = dpi / 200.0
    min_area = max(3, int(5 * scale * scale))
    small = stats[:, cv2.CC_STAT_AREA] < min_area
    small[0] = False
    if small.any():
        labels = np.where(small[labels], 0, labels).astype(np.int32)
        clean &= labels > 0
    hs = stats[1:, cv2.CC_STAT_HEIGHT][~small[1:]]
    plausible = hs[(hs >= 0.03 * dpi) & (hs <= 0.5 * dpi)]
    glyph = float(np.median(plausible)) if plausible.size else 0.12 * dpi
    paper_rgb = np.dstack([_background(rgb[:, :, c]) for c in range(3)]) if not inverted else \
        np.full(rgb.shape, 255.0, np.float32)
    return PageInk(dpi=dpi, width=rgb.shape[1], height=rgb.shape[0], ink=clean, rules=rules,
                   labels=labels.astype(np.int32), stats=stats, glyph_px=glyph, paper_rgb=paper_rgb)


# ---------------------------------------------------------------------------------------------- assignment
def needs_refinement(el: Element) -> bool:
    return el.bbox is not None and el.bbox_source in REFINABLE_SOURCES


def seed_box(el: Element) -> BBox | None:
    """The model's own box (``bbox_model`` after an earlier refinement)."""
    return el.bbox_model or el.bbox


def _px_box(b: BBox, k: float) -> np.ndarray:
    return np.array([b.x0 * k, b.y0 * k, b.x1 * k, b.y1 * k], dtype=np.float64)


def _expand(box: np.ndarray, dx: float, dy: float, w: int, h: int) -> np.ndarray:
    return np.array([max(0.0, box[0] - dx), max(0.0, box[1] - dy), min(float(w), box[2] + dx),
                     min(float(h), box[3] + dy)])


def _comp_boxes(stats: np.ndarray) -> np.ndarray:
    x, y, w, h = (stats[:, i].astype(np.float64) for i in range(4))
    return np.stack([x, y, x + w, y + h], axis=1)


def _depth(comps: np.ndarray, box: np.ndarray) -> np.ndarray:
    """How deep each component centre sits inside ``box``, as a fraction of the box size (negative outside)."""
    cx = (comps[:, 0] + comps[:, 2]) / 2
    cy = (comps[:, 1] + comps[:, 3]) / 2
    bw, bh = max(box[2] - box[0], 1.0), max(box[3] - box[1], 1.0)
    dx = np.minimum(cx - box[0], box[2] - cx) / bw
    dy = np.minimum(cy - box[1], box[3] - cy) / bh
    return np.minimum(dx, dy)


def _union(comps: np.ndarray, ids: list[int]) -> np.ndarray:
    sel = comps[ids]
    return np.array([sel[:, 0].min(), sel[:, 1].min(), sel[:, 2].max(), sel[:, 3].max()])


def _inside(inner: np.ndarray, outer: np.ndarray) -> bool:
    return bool(inner[0] >= outer[0] - 0.5 and inner[1] >= outer[1] - 0.5 and inner[2] <= outer[2] + 0.5
                and inner[3] <= outer[3] + 0.5)


def _box_gap(comps: np.ndarray, box: np.ndarray) -> np.ndarray:
    """Chebyshev gap between each component box and ``box`` (0 when they touch or overlap)."""
    gx = np.maximum(0, np.maximum(box[0] - comps[:, 2], comps[:, 0] - box[2]))
    gy = np.maximum(0, np.maximum(box[1] - comps[:, 3], comps[:, 1] - box[3]))
    return np.maximum(gx, gy)


def _row_cluster(comps: np.ndarray, start: int, pool: np.ndarray, glyph: float) -> list[int]:
    """Components chained horizontally with ``start`` (gaps < 1.2 glyph, vertical overlap): a text line."""
    members = [start]
    box = comps[start].copy()
    changed = True
    while changed:
        changed = False
        for j in np.flatnonzero(pool):
            if j in members:
                continue
            c = comps[j]
            vover = min(c[3], box[3]) - max(c[1], box[1])
            if vover < 0.4 * min(c[3] - c[1], box[3] - box[1]):
                continue
            gap = max(box[0] - c[2], c[0] - box[2], 0)
            if gap <= 1.2 * glyph:
                members.append(int(j))
                box = np.array([min(box[0], c[0]), min(box[1], c[1]), max(box[2], c[2]), max(box[3], c[3])])
                changed = True
    return members


def _pixel_share(labels: np.ndarray, box: np.ndarray, area: np.ndarray) -> np.ndarray:
    """Share of each component's pixels inside ``box``."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    inside = np.bincount(labels[y0:y1, x0:x1].ravel(), minlength=area.shape[0]).astype(np.float64)
    return inside / np.maximum(area, 1.0)


def _clipped_box(labels: np.ndarray, comp: int, box: np.ndarray, window: np.ndarray) -> np.ndarray | None:
    """Bounding box of component ``comp``'s pixels inside ``window`` (None when none are)."""
    x0, y0 = max(int(window[0]), int(box[0])), max(int(window[1]), int(box[1]))
    x1, y1 = min(int(np.ceil(window[2])), int(box[2])), min(int(np.ceil(window[3])), int(box[3]))
    if x1 <= x0 or y1 <= y0:
        return None
    ys, xs = np.nonzero(labels[y0:y1, x0:x1] == comp)
    if not ys.size:
        return None
    return np.array([x0 + xs.min(), y0 + ys.min(), x0 + xs.max() + 1, y0 + ys.max() + 1], dtype=np.float64)


def _split_component(ink: PageInk, comp: int, seeds: list[np.ndarray], owners: list[int]
                     ) -> list[tuple[int, tuple[np.ndarray, np.ndarray]]]:
    """Pixels of ``comp`` grouped by the nearest of ``seeds`` (distance to the box; 0 inside)."""
    x, y, w, h = (int(v) for v in ink.stats[comp, :4])
    ys, xs = np.nonzero(ink.labels[y:y + h, x:x + w] == comp)
    ys, xs = ys + y, xs + x
    dist = []
    for sd in seeds:
        dx = np.maximum(0, np.maximum(sd[0] - xs, xs - sd[2]))
        dy = np.maximum(0, np.maximum(sd[1] - ys, ys - sd[3]))
        dist.append(np.hypot(dx, dy))
    nearest = np.argmin(np.stack(dist, axis=0), axis=0)
    out = []
    for i, j in enumerate(owners):
        sel = nearest == i
        if sel.any():
            out.append((j, (ys[sel], xs[sel])))
    return out


def refine_elements(elements: list[Element], ink: PageInk, page_width_pt: float, page_height_pt: float) -> PageInk:
    """Refine the boxes of ``elements`` in place against ``ink``. Returns ``ink`` with per-element results."""
    k = ink.dpi / 72.0
    W, H = ink.width, ink.height
    targets = [e for e in sorted(elements, key=lambda e: e.reading_order) if needs_refinement(e)]
    if not targets:
        return ink
    comps = _comp_boxes(ink.stats)
    area = ink.stats[:, cv2.CC_STAT_AREA].astype(np.float64)
    n = comps.shape[0]
    usable = area > 0
    usable[0] = False
    usable &= ~(((comps[:, 2] - comps[:, 0]) > MAX_COMPONENT_FRACTION * W)
                | ((comps[:, 3] - comps[:, 1]) > MAX_COMPONENT_FRACTION * H))
    g = ink.glyph_px
    # thin and long (mean stroke thickness = area / length): a remnant of a ruled line, possibly curved
    length = np.maximum(comps[:, 2] - comps[:, 0], comps[:, 3] - comps[:, 1])
    rule_like = (length > 2.5 * g) & (area / np.maximum(length, 1.0) < 0.2 * g)
    is_fig = [e.type in VISUAL_TYPES for e in targets]
    seeds, expanded, caps = [], [], []
    for el, fig in zip(targets, is_fig):
        s = _px_box(seed_box(el), k)
        bw, bh = s[2] - s[0], s[3] - s[1]
        m = max(2 * k, (0.5 if fig else 0.3) * g)
        seeds.append(s)
        expanded.append(_expand(s, m, m, W, H))
        frac = FIGURE_GROW_CAP if fig else TEXT_GROW_CAP
        extra = 2.5 if fig else 0.8
        caps.append(_expand(s, max(frac * bw, extra * g), max(frac * bh, extra * g), W, H))
    # share of each component's pixels inside each expanded box; best share wins, then the depth of the
    # component inside the model box, then the earlier element in reading order (argmax keeps the first)
    share = np.stack([_pixel_share(ink.labels, e, area) for e in expanded], axis=1)  # (n, m)
    depth = np.stack([_depth(comps, sd) for sd in seeds], axis=1)
    share[~usable] = 0
    thresholds = np.array([ASSIGN_MIN_OVERLAP_FIGURE if f else ASSIGN_MIN_OVERLAP for f in is_fig])
    eligible = share >= thresholds[None, :]
    key = np.where(eligible, share.round(1) * 10 + np.clip(depth, -1, 1), -np.inf)
    owner = np.full(n, -1, dtype=np.int64)
    has = eligible.any(axis=1)
    owner[has] = np.argmax(key[has], axis=1)
    # Covered by several boxes (overlapping model boxes): go to the element whose unambiguous ink is nearest,
    # e.g. a label just above a drawing that the paragraph box above also reaches.
    multiple = eligible.sum(axis=1) >= 2
    core = has & ~multiple
    if multiple.any() and core.any():
        for c in np.flatnonzero(multiple):
            best, best_gap = owner[c], np.inf
            for j in np.flatnonzero(eligible[c]):
                mine = np.flatnonzero(core & (owner == j))
                if mine.size:
                    gap = float(_box_gap(comps[mine], comps[c]).min())
                    if gap < best_gap - 0.5:
                        best, best_gap = j, gap
            owner[c] = best
    claimed = owner >= 0
    # a drawing that touches another drawing is one component: split it, each pixel going to the figure whose
    # model box is nearest, as new components owned by those figures
    fig_cols = [j for j, f in enumerate(is_fig) if f]
    if len(fig_cols) > 1:
        multi = (share[:, fig_cols] >= 0.15).sum(axis=1) >= 2
        parts: list[tuple[int, np.ndarray]] = []
        for c in np.flatnonzero(multi & usable):
            js = [fig_cols[i] for i in np.flatnonzero(share[c, fig_cols] >= 0.15)]
            parts += _split_component(ink, int(c), [seeds[j] for j in js], js)
            owner[c] = -1
            usable[c] = False
        if parts:
            base = ink.stats.shape[0]
            rows = []
            for idx, (_owner, (pys, pxs)) in enumerate(parts):
                ink.labels[pys, pxs] = base + idx
                rows.append([pxs.min(), pys.min(), pxs.max() - pxs.min() + 1, pys.max() - pys.min() + 1, pys.size])
            ink.stats = np.vstack([ink.stats, np.array(rows, dtype=ink.stats.dtype)])
            comps = _comp_boxes(ink.stats)
            area = ink.stats[:, cv2.CC_STAT_AREA].astype(np.float64)
            extra = len(parts)
            owner = np.concatenate([owner, np.array([j for j, _ in parts], dtype=np.int64)])
            usable = np.concatenate([usable, np.ones(extra, dtype=bool)])
            length = np.maximum(comps[:, 2] - comps[:, 0], comps[:, 3] - comps[:, 1])
            rule_like = (length > 2.5 * g) & (area / np.maximum(length, 1.0) < 0.2 * g)
            n = comps.shape[0]
        claimed = owner >= 0
    shared: dict[int, list[int]] = {}

    for j, el in enumerate(targets):
        fig = is_fig[j]
        cap = caps[j]
        ids = [int(i) for i in np.flatnonzero(owner == j) if int(i) not in shared]
        ids += [c for c, js in shared.items() if j in js]
        boxes: dict[int, np.ndarray] = {}
        dropped_area = 0.0
        for i in ids:
            if rule_like[i] and not fig and not _inside(comps[i], expanded[j]):
                clipped = _clipped_box(ink.labels, i, comps[i], expanded[j])
                if clipped is not None:
                    boxes[i] = clipped  # a rule remnant crossing the box: count only the part inside
                continue
            if _inside(comps[i], cap):
                boxes[i] = comps[i]
            elif fig:
                clipped = _clipped_box(ink.labels, i, comps[i], cap)
                if clipped is not None:
                    boxes[i] = clipped
            else:
                dropped_area += area[i]
                owner[i] = -1
                claimed[i] = False
        total = sum(area[i] for i in ids) or 1.0
        if not boxes:
            ink.results[el.element_id] = Refinement(el.element_id, False, "no ink inside the model box")
            continue
        if dropped_area > 0.5 * total:
            ink.results[el.element_id] = Refinement(el.element_id, False, "ink extends beyond the growth cap")
            continue
        sx0, sy0, sx1, sy1 = (int(round(v)) for v in seeds[j])
        seed_labels = ink.labels[sy0:sy1, sx0:sx1]
        seed_ink = int((seed_labels > 0).sum())
        own_ink = int(np.isin(seed_labels, np.fromiter(boxes, dtype=np.int32)).sum())
        if seed_ink and own_ink < 0.4 * seed_ink:
            # most ink inside the model box belongs to a larger structure (an unremoved line, a neighbour):
            # the fit would describe that structure, not this element
            ink.results[el.element_id] = Refinement(el.element_id, False, "ink inside the box belongs to "
                                                    "a larger structure")
            continue
        if fig:
            boxes = _grow_figure(boxes, comps, cap, claimed & usable & ~rule_like, g)
            for i in boxes:
                if i not in shared:
                    owner[i] = j
                    claimed[i] = True
        sel = np.array(list(boxes.values()))
        box = np.array([sel[:, 0].min(), sel[:, 1].min(), sel[:, 2].max(), sel[:, 3].max()])
        pad = 1.0
        bb = BBox(x0=max(0.0, (box[0] - pad) / k), y0=max(0.0, (box[1] - pad) / k),
                  x1=min(page_width_pt, (box[2] + pad) / k), y1=min(page_height_pt, (box[3] + pad) / k))
        if bb.width <= 0.5 or bb.height <= 0.5:
            ink.results[el.element_id] = Refinement(el.element_id, False, "refined box degenerate")
            continue
        if el.bbox_model is None:
            el.bbox_model = el.bbox
        el.bbox = bb
        el.bbox_source = "refined"
        ink.results[el.element_id] = Refinement(el.element_id, True, "ok", sorted(boxes))
    for el in targets:
        r = ink.results.get(el.element_id)
        if r is not None and not r.refined and el.bbox_model is not None:
            # an earlier refinement no longer holds: fall back to the model's box
            el.bbox, el.bbox_model, el.bbox_source = el.bbox_model, None, "model_estimate"
    return ink


def _grow_figure(boxes: dict[int, np.ndarray], comps: np.ndarray, cap: np.ndarray, claimed: np.ndarray,
                 glyph: float) -> dict[int, np.ndarray]:
    """Add unclaimed strokes and labels near the figure's ink: strokes within ``0.8 glyph`` of a figure stroke,
    label-sized marks within ``2 glyph``; inside
    the cap, and not part of a text line that runs out of the cap. Rule remnants are excluded by the caller."""
    members = dict(boxes)
    free = ~claimed
    free[0] = False
    gap = 0.8 * glyph  # strokes continuing the drawing
    label_gap = 2.0 * glyph  # a label (T_A, dx, θ₁) written a little apart from the strokes it names
    rejected: set[int] = set()
    for _ in range(12):
        added = False
        sel = np.array(list(members.values()))
        box = np.array([sel[:, 0].min(), sel[:, 1].min(), sel[:, 2].max(), sel[:, 3].max()])
        for c in np.flatnonzero(free & (_box_gap(comps, box) <= label_gap)):
            c = int(c)
            if c in members or c in rejected:
                continue
            if not _inside(comps[c], cap):
                rejected.add(c)
                continue
            nearest = float(_box_gap(sel, comps[c]).min())
            cw, chh = comps[c, 2] - comps[c, 0], comps[c, 3] - comps[c, 1]
            label_sized = chh <= 2.5 * glyph and cw <= 4 * glyph
            if nearest > (label_gap if label_sized else gap):
                continue  # near the bounding box only, not near an actual stroke
            if nearest > 1 and min(cw, chh) <= max(2.0, 0.15 * glyph) and max(cw, chh) >= 3 * min(cw, chh):
                rejected.add(c)  # an isolated thin dash: a piece of a faint dotted rule, not part of the drawing
                continue
            if nearest > 0.3 * glyph and max(cw, chh) < 0.3 * glyph:
                rejected.add(c)  # a speck of dust or a stray dot off the drawing
                continue
            row = _row_cluster(comps, c, free, glyph)
            if len(row) >= 5:
                rb = _union(comps, row)
                if not _inside(rb, cap) or (rb[2] - rb[0]) > 0.9 * (box[2] - box[0]) + 3 * glyph:
                    rejected.update(row)
                    continue
            members[c] = comps[c]
            free[c] = False
            sel = np.vstack([sel, comps[c][None, :]])
            added = True
        if not added:
            break
    return members


def refine_page(elements: list[Element], img: Image.Image, dpi: float, page_width_pt: float,
                page_height_pt: float) -> PageInk | None:
    """Analyse ``img`` and refine ``elements`` in place. Returns the analysis, or None when nothing needed it
    or the analysis failed (boxes are then left as the model gave them)."""
    if not any(needs_refinement(e) for e in elements):
        return None
    try:
        ink = analyse_page(img, dpi)
        return refine_elements(elements, ink, page_width_pt, page_height_pt)
    except Exception:  # refinement is an improvement, never a reason to fail a page
        log.exception("box refinement failed; keeping model boxes")
        return None
