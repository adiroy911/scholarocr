"""Detect the document region inside a larger frame: a screenshot of a note-taking or meeting
app, or a photo of a page on a desk.

This step is deliberately conservative, because cropping away real content would be far worse
than leaving some margin. A region is cropped only when every one of these holds:
- it is one connected region of uniform tone (dark or light), and its filled outline covers at
  least 85% of its bounding rectangle, so it is roughly rectangular;
- it covers 12-92% of the frame (a normal scan fills the frame, so it is never cropped);
- it touches at most two edges of the frame (letterbox bars and window borders are rejected);
- its tone differs strongly from everything outside it (median difference of at least 60 grey
  levels), so a ruled table box on a white page never qualifies;
- it contains content (edge density), so empty panels are rejected.

Detection repeats inside the chosen region for nested frames. The crop is recorded as a transform: model boxes are mapped back to the original image, and the
original page image is always kept.
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

MIN_AREA, MAX_AREA = 0.12, 0.92
MIN_FILL = 0.85
MIN_TONE_DIFF = 60.0
MIN_EDGE_DENSITY = 0.01
PAD_FRACTION = 0.01


def detect_content_region(img: Image.Image, max_depth: int = 3) -> tuple[int, int, int, int] | None:
    """Return (x0, y0, x1, y1) in original pixels, or None when the frame should be used as is.

    Frames can be nested (screen -> app canvas -> note page), so detection is repeated inside the
    chosen region, up to ``max_depth`` levels.
    """
    region = None
    current = img
    ox = oy = 0
    for _ in range(max_depth):
        r = _detect_once(current)
        if r is None:
            break
        x0, y0, x1, y1 = r
        region = (ox + x0, oy + y0, ox + x1, oy + y1)
        ox, oy = region[0], region[1]
        current = img.crop(region)
    return region


def _detect_once(img: Image.Image) -> tuple[int, int, int, int] | None:
    gray_full = np.asarray(img.convert("L"))
    h_full, w_full = gray_full.shape
    scale = min(1.0, 1000 / max(h_full, w_full))
    gray = cv2.resize(gray_full, (int(w_full * scale), int(h_full * scale)), interpolation=cv2.INTER_AREA) \
        if scale < 1 else gray_full
    h, w = gray.shape
    total = float(h * w)
    t, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)
    best: tuple[float, tuple[int, int, int, int]] | None = None
    for mask in ((gray < t).astype(np.uint8), (gray >= t).astype(np.uint8)):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            rect_area = float(cw * ch)
            frac = rect_area / total
            if not (MIN_AREA <= frac <= MAX_AREA):
                continue
            if cv2.contourArea(c) / rect_area < MIN_FILL:
                continue
            touches = sum([x <= 1, y <= 1, x + cw >= w - 1, y + ch >= h - 1])
            if touches > 2:
                continue
            inside = np.zeros_like(gray, dtype=bool)
            inside[y:y + ch, x:x + cw] = True
            if (~inside).sum() < 0.05 * total:
                continue
            if abs(float(np.median(gray[inside])) - float(np.median(gray[~inside]))) < MIN_TONE_DIFF:
                continue
            if float((edges[inside] > 0).mean()) < MIN_EDGE_DENSITY:
                continue
            if best is None or rect_area > best[0]:
                best = (rect_area, (x, y, x + cw, y + ch))
    if best is None:
        return None
    x0, y0, x1, y1 = best[1]
    pad_x, pad_y = int(PAD_FRACTION * w), int(PAD_FRACTION * h)
    x0, y0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
    x1, y1 = min(w, x1 + pad_x), min(h, y1 + pad_y)
    return (int(x0 / scale), int(y0 / scale), min(w_full, int(round(x1 / scale))), min(h_full, int(round(y1 / scale))))
