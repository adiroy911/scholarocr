"""Condition-driven preprocessing. Steps run only when the quality assessment calls for them;
nothing is binarised or cropped away, and the original image is always kept separately."""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from ..schemas import QualityAssessment, Transform
from . import geometry
from .quality import CONTRAST_LOW, DPI_LOW, GLYPH_LOW_PX, GLYPH_TARGET_PX, NOISE_HIGH, SKEW_MIN

TARGET_DPI = 200.0


def _warp(arr: np.ndarray, m: list[list[float]], size: tuple[int, int]) -> np.ndarray:
    return cv2.warpAffine(arr, np.asarray(m, float), size, flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=(255, 255, 255))


def preprocess(img: Image.Image, quality: QualityAssessment, *, extra_rotation: int = 0,
               crop_box: tuple[int, int, int, int] | None = None,
               ) -> tuple[Image.Image, list[Transform], list[list[float]]]:
    """Return (processed image, applied transforms, affine original->processed).

    ``quality`` must describe the (cropped) region that is processed; ``crop_box`` is in original
    pixels and is applied first.
    """
    arr = np.asarray(img.convert("RGB")).copy()
    matrix = geometry.IDENTITY
    transforms: list[Transform] = []

    if crop_box is not None:
        x0, y0, x1, y1 = crop_box
        arr = arr[y0:y1, x0:x1].copy()
        matrix = [[1.0, 0.0, -float(x0)], [0.0, 1.0, -float(y0)]]
        transforms.append(Transform(step="crop", params={"box_px": [x0, y0, x1, y1],
                                                         "reason": "detected document region inside a larger frame"}))

    rotation = ((quality.orientation_degrees or 0) + extra_rotation) % 360
    if rotation:
        h, w = arr.shape[:2]
        # orientation is "clockwise degrees the content is rotated"; correct by rotating back (CCW positive).
        m, size = geometry.rotation_matrix(w, h, float(rotation))
        arr = _warp(arr, m, size)
        matrix = geometry.compose(m, matrix)
        transforms.append(Transform(step="rotate", params={"degrees_ccw": rotation}))

    skew = quality.skew_degrees or 0.0
    if abs(skew) >= SKEW_MIN and abs(skew) <= 15 and not rotation:
        h, w = arr.shape[:2]
        m, size = geometry.rotation_matrix(w, h, float(skew))
        arr = _warp(arr, m, size)
        matrix = geometry.compose(m, matrix)
        transforms.append(Transform(step="deskew", params={"degrees_ccw": skew}))

    dpi = quality.effective_dpi
    factor = None
    if quality.glyph_height_px is not None:
        # Judge resolution from measured character height; DPI metadata is often a placeholder.
        if quality.glyph_height_px < GLYPH_LOW_PX:
            factor = min(2.5, GLYPH_TARGET_PX / quality.glyph_height_px)
    elif dpi is not None and dpi < DPI_LOW:
        factor = min(2.5, TARGET_DPI / dpi)
    if factor is not None and factor > 1.05:
        h, w = arr.shape[:2]
        arr = cv2.resize(arr, (int(w * factor), int(h * factor)), interpolation=cv2.INTER_CUBIC)
        matrix = geometry.compose(geometry.scale_matrix(factor), matrix)
        transforms.append(Transform(step="upscale", params={"factor": round(factor, 3)}))

    if quality.noise is not None and quality.noise > NOISE_HIGH:
        arr = cv2.fastNlMeansDenoisingColored(arr, None, 7, 7, 7, 21)
        transforms.append(Transform(step="denoise", params={"method": "nl_means", "h": 7}))

    if quality.contrast is not None and quality.contrast < CONTRAST_LOW:
        lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        arr = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
        transforms.append(Transform(step="contrast", params={"method": "clahe", "clip": 2.0}))

    if transforms:
        transforms.append(Transform(step="affine", params={"matrix": matrix}))
    return Image.fromarray(arr), transforms, matrix
