"""Invertible affine geometry. Every geometric preprocessing step is a 2x3 affine matrix mapping
original-image pixels to processed-image pixels; model boxes (on the processed image) are mapped
back through the inverse so the IR always refers to the ORIGINAL page."""

from __future__ import annotations

import cv2
import numpy as np

IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


def compose(second: list[list[float]], first: list[list[float]]) -> list[list[float]]:
    """Matrix for applying ``first`` then ``second``."""
    a = np.vstack([np.asarray(second, float), [0, 0, 1]])
    b = np.vstack([np.asarray(first, float), [0, 0, 1]])
    return (a @ b)[:2].tolist()


def invert(m: list[list[float]]) -> list[list[float]]:
    return cv2.invertAffineTransform(np.asarray(m, float)).tolist()


def apply_point(m: list[list[float]], x: float, y: float) -> tuple[float, float]:
    a = np.asarray(m, float)
    return float(a[0, 0] * x + a[0, 1] * y + a[0, 2]), float(a[1, 0] * x + a[1, 1] * y + a[1, 2])


def apply_box(m: list[list[float]], x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
    pts = [apply_point(m, x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def rotation_matrix(width: int, height: int, angle_ccw: float) -> tuple[list[list[float]], tuple[int, int]]:
    """Rotate by ``angle_ccw`` degrees counter-clockwise around the centre, expanding the canvas."""
    cx, cy = width / 2.0, height / 2.0
    m = cv2.getRotationMatrix2D((cx, cy), angle_ccw, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    new_w = int(round(height * sin + width * cos))
    new_h = int(round(height * cos + width * sin))
    m[0, 2] += new_w / 2.0 - cx
    m[1, 2] += new_h / 2.0 - cy
    return m.tolist(), (new_w, new_h)


def scale_matrix(factor: float) -> list[list[float]]:
    return [[factor, 0.0, 0.0], [0.0, factor, 0.0]]
