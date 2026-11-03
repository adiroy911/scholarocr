"""The redraw fidelity check on a synthetic hand-drawn diagram (built here with PIL): a faithful SVG passes; a
missing arrow, an extra shape, a moved label or a missing label fails with feedback that says where."""

from __future__ import annotations

import random

import pytest
from PIL import Image, ImageDraw, ImageFont

from scholarocr.pipeline.svg_fidelity import check_fidelity, missing_labels
from scholarocr.svg_sanitize import sanitize_svg

W, H = 640, 320
PEN = (30, 50, 160)
LABELS = ["T_A", "x"]


def _font(size: int):
    for f in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/Library/Fonts/Arial Unicode.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wobbly(d, pts, rng, width=4):
    """A hand-drawn line through ``pts``: small jitter along the way."""
    out = []
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(2, int(max(abs(x1 - x0), abs(y1 - y0)) / 12))
        for i in range(n):
            t = i / n
            out.append((x0 + (x1 - x0) * t + rng.uniform(-1.5, 1.5), y0 + (y1 - y0) * t + rng.uniform(-1.5, 1.5)))
    out.append(pts[-1])
    d.line(out, fill=PEN, width=width, joint="curve")


@pytest.fixture(scope="module")
def crop() -> Image.Image:
    rng = random.Random(3)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    _wobbly(d, [(140, 60), (330, 60), (330, 250), (140, 250), (140, 60)], rng)  # the box
    _wobbly(d, [(330, 155), (560, 155)], rng)  # the arrow shaft
    d.polygon([(585, 155), (555, 141), (555, 169)], fill=PEN)  # its head
    f = _font(40)
    d.text((40, 130), "TA", fill=PEN, font=f)
    d.text((430, 100), "x", fill=PEN, font=f)
    return img


def svg(*, arrow=True, extra=False, label_at=(40, 130), x_label=True) -> str:
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 320">',
             '<rect x="140" y="60" width="190" height="190" fill="none" stroke="#111" stroke-width="4"/>']
    if arrow:
        parts += ['<line x1="330" y1="155" x2="560" y2="155" stroke="#111" stroke-width="4"/>',
                  '<polygon points="585,155 555,141 555,169" fill="#111"/>']
    if extra:
        parts.append('<circle cx="480" cy="260" r="40" fill="none" stroke="#111" stroke-width="4"/>')
    lx, ly = label_at
    parts.append(f'<text x="{lx}" y="{ly + 36}" font-size="40" font-family="Arial, Helvetica, sans-serif" '
                 'fill="#111">T<tspan>A</tspan></text>')
    if x_label:
        parts.append('<text x="430" y="130" font-size="40" font-family="Arial, Helvetica, sans-serif" '
                     'fill="#111">x</text>')
    parts.append("</svg>")
    return "".join(parts)


def check(crop, raw):
    clean = sanitize_svg(raw)
    return check_fidelity(clean.svg, clean.texts, crop, LABELS)


def test_faithful_copy_passes(crop):
    fid = check(crop, svg())
    assert fid.passed, fid.feedback()
    assert fid.recall >= 0.9 and fid.precision >= 0.9 and fid.score >= 0.9
    assert fid.size == (W, H) and not fid.labels_missing and not fid.missing_regions and not fid.extra_regions
    d = fid.to_dict()
    assert {"score", "recall", "precision", "passed", "labels_missing", "missing_regions", "extra_regions"} <= set(d)


def test_missing_arrow_fails_with_its_place(crop):
    fid = check(crop, svg(arrow=False))
    assert not fid.passed and fid.missing_regions
    r = fid.missing_regions[0]
    assert r.x0 >= 320 and r.x1 >= 560 and 130 <= (r.y0 + r.y1) / 2 <= 180  # the arrow, right of the box
    assert any("missing from your SVG" in f and "right" in f for f in fid.feedback())


def test_extra_shape_fails(crop):
    fid = check(crop, svg(extra=True))
    assert not fid.passed and fid.extra_regions and fid.precision < 1
    r = fid.extra_regions[0]
    assert 420 <= (r.x0 + r.x1) / 2 <= 540 and (r.y0 + r.y1) / 2 >= 200
    assert any("where the original has none" in f for f in fid.feedback())


def test_moved_label_fails(crop):
    fid = check(crop, svg(label_at=(40, 230)))  # T_A drawn 100 px lower than written
    assert not fid.passed and fid.missing_regions and fid.extra_regions
    assert not fid.labels_missing  # the text is there, in the wrong place
    assert any(r.x0 < 140 for r in fid.missing_regions) and any(r.x0 < 140 for r in fid.extra_regions)


def test_missing_label_fails(crop):
    fid = check(crop, svg(x_label=False))
    assert not fid.passed and fid.labels_missing == ["x"]
    assert any("labels missing" in f and "x" in f for f in fid.feedback())


def test_wrong_viewbox_is_caught(crop):
    raw = svg().replace('viewBox="0 0 640 320"', 'viewBox="0 0 1280 640"')  # everything drawn at half size
    assert not check(crop, raw).passed


def test_label_normalisation():
    assert missing_labels(["T_A", "\\theta_1", "T_x - dT_x", "="], ["T", "A", "θ", "1", "Tx", "−dTx"]) == []
    assert missing_labels(["Q"], ["T", "A"]) == ["Q"]
