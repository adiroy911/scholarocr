"""Deterministic page-image quality assessment (OpenCV). Thresholds are documented in
docs/QUALITY_ASSURANCE.md; they select preprocessing steps and inform uncertainty labels."""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from ..schemas import QualityAssessment

SHARPNESS_BLURRY = 400.0  # Laplacian variance near ink
CONTRAST_LOW = 110.0  # paper (90th pct) minus ink (10th pct of dark pixels), grey levels
NOISE_HIGH = 6.0
SKEW_MIN = 0.4
DPI_LOW = 150.0
GLYPH_LOW_PX = 12.0  # median character height below which text is too small to read reliably
GLYPH_TARGET_PX = 22.0


def to_gray(img: Image.Image, max_side: int = 1600) -> tuple[np.ndarray, float]:
    g = np.asarray(img.convert("L"))
    h, w = g.shape
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        g = cv2.resize(g, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return g, scale


def _binarize(gray: np.ndarray) -> np.ndarray:
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw


def _profile_score(bw: np.ndarray, angle: float) -> float:
    h, w = bw.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rot = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    rows = rot.sum(axis=1, dtype=np.float64)
    return float(np.var(np.diff(rows)))


def estimate_skew(bw: np.ndarray, max_angle: float = 10.0) -> float:
    """Projection-profile skew estimate in degrees (positive = rotate CCW to correct)."""
    small = bw
    if max(bw.shape) > 900:
        f = 900 / max(bw.shape)
        small = cv2.resize(bw, (int(bw.shape[1] * f), int(bw.shape[0] * f)), interpolation=cv2.INTER_NEAREST)
    best, best_score = 0.0, -1.0
    for a in np.arange(-max_angle, max_angle + 0.01, 0.5):
        s = _profile_score(small, float(a))
        if s > best_score:
            best, best_score = float(a), s
    for a in np.arange(best - 0.5, best + 0.51, 0.1):
        s = _profile_score(small, float(a))
        if s > best_score:
            best, best_score = float(a), s
    return round(best, 2)


def estimate_orientation(bw: np.ndarray) -> int:
    """Return 90 if text runs vertically (page rotated a quarter turn), else 0.

    Characters are merged into word blobs by dilation sized to the median glyph height; horizontal
    text yields blobs wider than tall. Distinguishing 90 from 270 (and 0 from 180) is not reliable
    deterministically; the Gemini pass reports residual rotation and the pipeline corrects it with
    one re-extraction.
    """
    if (bw > 0).mean() < 0.001:
        return 0
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    if n < 10:
        return 0
    hs = stats[1:, cv2.CC_STAT_HEIGHT]
    ws = stats[1:, cv2.CC_STAT_WIDTH]
    keep = (hs > 3) & (ws > 3) & (hs < bw.shape[0] / 8) & (ws < bw.shape[1] / 8)
    if keep.sum() < 10:
        return 0
    glyph = float(np.median(np.minimum(hs[keep], ws[keep])))
    k = max(2, int(glyph * 0.9))
    merged = cv2.dilate(bw, np.ones((k, k), np.uint8))
    n2, _, st2, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)
    horiz = vert = 0.0
    for i in range(1, n2):
        w, h, area = st2[i, cv2.CC_STAT_WIDTH], st2[i, cv2.CC_STAT_HEIGHT], st2[i, cv2.CC_STAT_AREA]
        if w > 1.6 * h:
            horiz += area
        elif h > 1.6 * w:
            vert += area
    return 90 if vert > 2.0 * horiz and vert > 0 else 0


def glyph_height_px(bw: np.ndarray) -> float | None:
    """Median height of character-sized connected components (in the given image's pixels)."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    if n < 15:
        return None
    hs = stats[1:, cv2.CC_STAT_HEIGHT]
    ws = stats[1:, cv2.CC_STAT_WIDTH]
    keep = (hs >= 3) & (ws >= 2) & (hs < bw.shape[0] / 10) & (ws < bw.shape[1] / 10)
    if keep.sum() < 15:
        return None
    return float(np.median(hs[keep]))


def assess(img: Image.Image, dpi: float | None) -> QualityAssessment:
    gray, scale = to_gray(img)
    # Polarity: notes written on a dark page (dark-mode apps, blackboards) are measured on the
    # inverted image so "ink" means writing, not background. The image sent to the model is
    # never inverted (ink colours carry meaning).
    light_on_dark = float(np.median(gray)) < 110
    if light_on_dark:
        gray = 255 - gray
    bw = _binarize(gray)
    ink = float((bw > 0).mean())
    # Ink-vs-paper dynamic range, measured on the darkest pixels actually present, so sparse
    # pages (a single heading) are not mistaken for low-contrast or blank ones.
    paper = float(np.percentile(gray, 90))
    darkish = gray[gray < paper - 30]
    ink_level = float(np.percentile(darkish, 10)) if darkish.size else float(gray.min())
    contrast = paper - ink_level
    q = QualityAssessment(
        sharpness=None,
        contrast=round(contrast, 2),
        brightness=round(float(gray.mean()), 2),
        ink_ratio=round(ink, 4),
        effective_dpi=dpi,
        light_on_dark=light_on_dark,
    )
    med = cv2.medianBlur(gray, 3)
    background = gray > np.percentile(gray, 60)
    q.noise = round(float(np.abs(gray.astype(np.int16) - med.astype(np.int16))[background].mean()) if background.any() else 0.0, 2)
    dark_fraction = float(darkish.size) / gray.size
    q.is_blank = contrast < 30.0 or dark_fraction < 0.0002
    if q.is_blank:
        q.grade = "good"
        q.issues.append("blank")
        return q
    # Sharpness: Laplacian variance measured only around ink (white paper would dilute it).
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    near_ink = cv2.dilate((gray < paper - 30).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    q.sharpness = round(float(lap[near_ink].var()), 2) if near_ink.any() else None
    q.orientation_degrees = estimate_orientation(bw)
    # Skew from a projection profile needs several text lines; too little ink gives noise.
    enough_text = dark_fraction >= 0.004
    q.skew_degrees = estimate_skew(bw) if (q.orientation_degrees == 0 and enough_text) else 0.0
    glyph = glyph_height_px(bw)
    q.glyph_height_px = round(glyph / scale, 1) if glyph else None
    issues = []
    if light_on_dark:
        issues.append("light_on_dark")
    if q.sharpness is not None and q.sharpness < SHARPNESS_BLURRY:
        issues.append("blurry")
    if contrast < CONTRAST_LOW:
        issues.append("low_contrast")
    if q.noise is not None and q.noise > NOISE_HIGH:
        issues.append("noisy")
    if q.skew_degrees and abs(q.skew_degrees) >= SKEW_MIN:
        issues.append("skewed")
    if q.orientation_degrees:
        issues.append("rotated")
    # Resolution is judged from the pixels (character height) when measurable; DPI metadata is
    # only a fallback because screenshots and phone photos often carry placeholder values.
    if q.glyph_height_px is not None:
        if q.glyph_height_px < GLYPH_LOW_PX:
            issues.append("low_resolution")
    elif dpi is not None and dpi < DPI_LOW:
        issues.append("low_resolution")
    if q.brightness is not None and q.brightness < 90:
        issues.append("dark")
    q.issues = issues
    severe = {"blurry", "noisy", "low_resolution", "dark"}
    q.grade = "poor" if len(set(issues) & severe) >= 2 or ("blurry" in issues and "low_contrast" in issues) else (
        "fair" if set(issues) - {"light_on_dark"} else "good")
    return q
