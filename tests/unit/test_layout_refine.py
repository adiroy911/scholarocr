"""Box refinement and figure images on synthetic pages (ruled notebook paper, handwriting-like strokes, a diagram
with labels), built here with PIL. No model is called and no real document is used."""

from __future__ import annotations

import random

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from scholarocr.pipeline import figures, layout_refine
from scholarocr.schemas import BBox, Element, ElementType, FigureData, Page, PageStatus, SourceRef
from scholarocr.schemas.document import Route
from scholarocr.storage import ArtifactStore

DPI = 200.0
K = 72.0 / DPI  # pt per px
W, H = 1654, 1300
PEN = (35, 55, 165)
DOC = "doc_00000000000000aa"


def _font(size: int):
    for f in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/Library/Fonts/Arial Unicode.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _word(d: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, rng: random.Random) -> None:
    """A handwriting-like squiggle filling roughly (x, y, x + w, y + h)."""
    pts = []
    for i in range(0, w + 1, 7):
        pts.append((x + i, y + h * (0.25 + 0.55 * rng.random())))
    d.line(pts, fill=PEN, width=3, joint="curve")


def _text_line(d, x0, y0, x1, h, rng):
    x = x0
    while x < x1 - 30:
        ww = min(rng.randint(50, 130), x1 - x)
        _word(d, x, y0, ww, h, rng)
        x += ww + rng.randint(18, 30)


def ruled_page() -> tuple[Image.Image, dict[str, tuple[int, int, int, int]]]:
    """Light bluish paper with slanted grey-blue rules every 62 px, a red margin rule, a shadow on the left,
    two lines of 'handwriting', a diagram (box, arrow, labels) and a text line under it. Returns the image and
    the true pixel boxes of the content."""
    rng = random.Random(7)
    img = Image.new("RGB", (W, H), (233, 237, 243))
    d = ImageDraw.Draw(img)
    for y in range(70, H, 62):
        d.line([(0, y), (W, y + 9)], fill=(160, 172, 195), width=2)
    d.line([(150, 0), (152, H)], fill=(215, 130, 130), width=2)
    truth = {}
    _text_line(d, 220, 142, 1240, 30, rng)  # line 1: between the rules at ~132 and ~194
    truth["para"] = (220, 142, 1240, 172)
    # diagram: a box, an arrow out of it, labels T_A (left, outside the box) and x (above the arrow)
    d.rectangle([520, 520, 820, 720], outline=PEN, width=4)
    d.line([(820, 620), (1120, 620)], fill=PEN, width=4)
    d.polygon([(1120, 620), (1095, 607), (1095, 633)], fill=PEN)
    f = _font(38)
    d.text((440, 590), "TA", fill=PEN, font=f)
    d.text((950, 560), "x", fill=PEN, font=f)
    truth["figure"] = (440, 520, 1121, 721)
    _text_line(d, 220, 830, 1100, 30, rng)  # a text line under the diagram (not part of it)
    truth["below"] = (220, 830, 1100, 860)
    arr = np.asarray(img).astype(np.float32)
    arr[:, :260] *= np.linspace(0.7, 1.0, 260)[None, :, None]  # shadow on the left
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8)), truth


def _el(i: int, type_: ElementType, box_px, **kw) -> Element:
    x0, y0, x1, y1 = box_px
    return Element(element_id=f"{DOC}-p0001-e{i:03d}", type=type_, page_id=f"{DOC}-p0001", page_number=1,
                   reading_order=i, content=kw.pop("content", "text"),
                   bbox=BBox(x0=x0 * K, y0=y0 * K, x1=x1 * K, y1=y1 * K), bbox_source="model_estimate",
                   extraction_method="host_model", handwritten=True,
                   source=SourceRef(document_id=DOC, page_number=1, source_file="notes.pdf", source_page_index=0),
                   **kw)


def _px(b: BBox) -> tuple[float, float, float, float]:
    return b.x0 / K, b.y0 / K, b.x1 / K, b.y1 / K


@pytest.fixture(scope="module")
def page_and_truth():
    return ruled_page()


def _elements():
    return [
        _el(1, ElementType.PARAGRAPH, (205, 132, 1215, 186)),  # a bit off: clips the last word, empty margin left
        _el(2, ElementType.FIGURE, (535, 540, 1080, 700), figure=FigureData(description="box with arrow")),  # misses TA
        _el(3, ElementType.PARAGRAPH, (210, 815, 1080, 870)),
        _el(4, ElementType.TEXT, (1300, 1000, 1500, 1040)),  # nothing written there
    ]


def test_rules_are_found_and_removed(page_and_truth):
    img, _ = page_and_truth
    ink = layout_refine.analyse_page(img, DPI)
    # a rule pixel far from any writing: detected as a rule, not left as ink
    y = 70 + 62 * 16 + int(9 * 1400 / W)
    assert ink.rules[y - 3:y + 4, 1400].any()
    assert not ink.ink[y - 4:y + 5, 1380:1420].any()
    # the margin rule is gone too, the pen strokes are kept
    assert not ink.ink[300:400, 148:156].any()
    assert ink.ink[520:524, 600:700].any()


def test_text_box_snaps_to_its_ink(page_and_truth):
    img, truth = page_and_truth
    els = _elements()
    layout_refine.refine_page(els, img, DPI, W * K, H * K)
    para = els[0]
    assert para.bbox_source == "refined" and para.bbox_model is not None
    x0, y0, x1, y1 = _px(para.bbox)
    tx0, ty0, tx1, ty1 = truth["para"]
    assert abs(x0 - tx0) <= 8 and abs(y0 - ty0) <= 8 and abs(y1 - ty1) <= 8
    dark = np.asarray(img.convert("L"))[150:170, :] < 120
    ink_right = int(np.flatnonzero(dark.any(axis=0)).max())  # where the line's ink really ends
    assert x1 >= ink_right - 4  # grew over the clipped last word ...
    assert x1 <= ink_right + 6  # ... but no further


def test_figure_grows_to_strokes_and_labels_not_to_neighbour_text(page_and_truth):
    img, truth = page_and_truth
    els = _elements()
    layout_refine.refine_page(els, img, DPI, W * K, H * K)
    fig = els[1]
    x0, y0, x1, y1 = _px(fig.bbox)
    tx0, ty0, tx1, ty1 = truth["figure"]
    assert x0 <= tx0 + 6  # the T_A label left of the box (outside the model box) belongs to the drawing
    assert y0 <= ty0 + 4 and x1 >= tx1 - 6 and y1 >= ty1 - 6  # the whole box and the arrow head
    assert y1 < truth["below"][1] - 10  # the text line under the drawing is not swallowed
    below = els[2]
    assert _px(below.bbox)[1] >= truth["below"][1] - 8


def test_unreliable_refinement_keeps_the_model_box(page_and_truth):
    img, _ = page_and_truth
    els = _elements()
    ink = layout_refine.refine_page(els, img, DPI, W * K, H * K)
    empty = els[3]
    assert empty.bbox_source == "model_estimate" and empty.bbox_model is None
    assert ink.results[empty.element_id].reason.startswith("no ink")


def test_refinement_is_idempotent_and_keeps_model_box(page_and_truth):
    img, _ = page_and_truth
    els = _elements()
    original = [e.bbox for e in els]
    layout_refine.refine_page(els, img, DPI, W * K, H * K)
    first = [e.bbox for e in els]
    layout_refine.refine_page(els, img, DPI, W * K, H * K)
    assert [e.bbox for e in els] == first
    assert els[0].bbox_model == original[0] and els[1].bbox_model == original[1]


def test_exact_boxes_are_never_touched(page_and_truth):
    img, _ = page_and_truth
    el = _el(1, ElementType.PARAGRAPH, (205, 132, 1205, 186))
    el.bbox_source = "pdf_text_layer"
    before = el.bbox
    assert layout_refine.refine_page([el], img, DPI, W * K, H * K) is None
    assert el.bbox == before and el.bbox_source == "pdf_text_layer"


def test_figure_crop_and_clean_crop(page_and_truth, tmp_path):
    img, truth = page_and_truth
    els = _elements()
    store = ArtifactStore(tmp_path)
    page = Page(page_id=f"{DOC}-p0001", document_id=DOC, page_number=1, width_pt=W * K, height_pt=H * K,
                status=PageStatus.DONE, route=Route.VISUAL_HANDWRITING, elements=els)
    ink = layout_refine.refine_page(els, img, DPI, W * K, H * K)
    written = figures.save_figure_images(store, page, img, DPI, ink)
    fig = els[1]
    assert written == [fig.element_id]
    assert fig.source.region_image == f"pages/0001/figures/{fig.element_id}.png"
    assert fig.figure.image_clean == f"pages/0001/figures/{fig.element_id}.clean.png"
    crop = Image.open(store.resolve(DOC, fig.source.region_image))
    clean = np.asarray(Image.open(store.resolve(DOC, fig.figure.image_clean)).convert("L"))
    # full resolution: the crop covers the refined box at the page's own pixel size (plus a small margin)
    bx0, by0, bx1, by1 = _px(fig.bbox)
    assert crop.width >= bx1 - bx0 and crop.height >= by1 - by0
    # cleaned: white paper, no ruled lines, the strokes kept and dark
    assert np.median(clean) == 255
    dark = clean < 128
    assert dark.sum() > 2000
    rows_with_long_runs = [r for r in dark if r.mean() > 0.9]
    assert len(rows_with_long_runs) <= 8  # only the box's own top and bottom edges, no rule lines across
    # trimmed to the drawing
    assert clean.shape[1] <= (truth["figure"][2] - truth["figure"][0]) + 2 * figures.CLEAN_MARGIN_PX + 20


def test_photo_figures_get_no_cleaned_version(page_and_truth, tmp_path):
    img, _ = page_and_truth
    els = [_el(2, ElementType.IMAGE, (535, 540, 1080, 700), figure=FigureData(kind="photo"))]
    page = Page(page_id=f"{DOC}-p0001", document_id=DOC, page_number=1, width_pt=W * K, height_pt=H * K,
                status=PageStatus.DONE, elements=els)
    figures.save_figure_images(ArtifactStore(tmp_path), page, img, DPI, None)
    assert els[0].source.region_image and els[0].figure.image_clean is None


def test_words_under_an_arrow_stay_with_their_text_not_the_equation_above():
    """Regression (user's page 3, element 006): "↑ Thermal resistance" is an arrow whose head sits under the
    equation R_H = l/KA and the words below it; the model's boxes overlap by a third. The refined text box must
    keep the words (and the arrow); the equation must not take them; and the layout PDF's overlap split must not
    cut them off. (On the real page the refined box did keep the words: the overlay's "006" label sits in the band
    shared with the equation's box, which read as a box of its own.)"""
    from scholarocr.export import layout_pdf as lp

    rng = random.Random(3)
    img = Image.new("RGB", (W, H), (233, 237, 243))
    d = ImageDraw.Draw(img)
    for y in range(70, H, 62):
        d.line([(0, y), (W, y + 4)], fill=(160, 172, 195), width=2)
    f = _font(44)
    d.text((396, 756), "R", fill=PEN, font=f)  # R_H = l / KA, with a scribbled-out blob in the middle
    d.text((440, 770), "=", fill=PEN, font=f)
    for k in range(9):
        d.ellipse([500 + 5 * k, 762 + 3 * k, 540 + 3 * k, 840 - 2 * k], outline=PEN, width=2)
    d.text((612, 752), "l", fill=PEN, font=f)
    d.line([(580, 806), (650, 806)], fill=PEN, width=3)
    d.text((582, 808), "KA", fill=PEN, font=_font(30))
    d.line([(400, 826), (400, 888)], fill=PEN, width=4)  # the arrow: head just under the equation
    d.polygon([(400, 818), (388, 842), (412, 842)], fill=PEN)
    _text_line(d, 420, 880, 653, 30, rng)  # "Thermal resistance"
    eq = _el(5, ElementType.EQUATION, (382, 751, 658, 859), content="R_H = l / (KA)", latex=r"R_H = \frac{l}{KA}")
    txt = _el(6, ElementType.TEXT, (382, 812, 660, 922), content="↑ Thermal resistance")
    ink = layout_refine.refine_page([eq, txt], img, DPI, W * K, H * K)
    assert ink is not None and txt.bbox_source == "refined" and eq.bbox_source == "refined"
    tx0, ty0, tx1, ty1 = _px(txt.bbox)
    ex0, ey0, ex1, ey1 = _px(eq.bbox)
    assert ty1 >= 900 and tx1 >= 620  # the words are in the text's box
    assert ty0 <= 822 and tx0 <= 392  # so is the arrow, head included
    assert ey1 < 878  # the equation stops above the words
    # the layout PDF splits the overlap band between the two boxes: the words stay inside the text's box
    page = Page(page_id=f"{DOC}-p0001", document_id=DOC, page_number=1, width_pt=W * K, height_pt=H * K)
    plan = lp.PagePlan(page, W * K, H * K, items=[lp._math_item(eq, lp.Box(eq.bbox.x0, eq.bbox.y0, eq.bbox.x1,
                                                                            eq.bbox.y1)),
                                                   lp._text_item(txt, lp.Box(txt.bbox.x0, txt.bbox.y0, txt.bbox.x1,
                                                                             txt.bbox.y1), "faithful")])
    lp.resolve_overlaps(plan)
    text_item = plan.items[1]
    assert text_item.box.y0 <= 880 * K and text_item.box.y1 >= 900 * K


# ------------------------------------------------------------------- pen on rules, and whose ink is whose
def _notebook():
    img = Image.new("RGB", (W, H), (233, 237, 243))
    d = ImageDraw.Draw(img)
    rules = [70 + 62 * i for i in range(20)]
    for y in rules:
        d.line([(0, y), (W, y)], fill=(160, 172, 195), width=2)
    return img, d, rules


def test_cleaned_crop_keeps_a_wire_drawn_on_a_rule_and_drops_the_rule():
    img, d, rules = _notebook()
    y = rules[8]
    d.rectangle([500, y - 120, 800, y - 20], outline=PEN, width=4)  # a box above, so the page has plenty of pen ink
    d.line([(800, y - 20), (900, y)], fill=PEN, width=3)
    d.line([(900, y), (1200, y)], fill=PEN, width=3)  # the wire runs exactly along the rule
    d.line([(1200, y), (1260, y + 80)], fill=PEN, width=3)
    els = [_el(1, ElementType.FIGURE, (480, y - 140, 1280, y + 100), figure=FigureData(description="circuit"))]
    ink = layout_refine.refine_page(els, img, DPI, W * K, H * K)
    assert ink is not None and ink.rule_coloured is not None
    box = figures.crop_box_px(els[0], DPI, img.size)
    placed = figures.clean_figure_placed(img, ink, box, ink.components_of(els[0].element_id) or None)
    assert placed is not None
    grey = np.asarray(placed[0].convert("L"))
    ox, oy = placed[1][0], placed[1][1]
    # the wire is still there along its whole length
    wire = grey[max(0, y - oy - 4):y - oy + 5, 920 - ox:1180 - ox]
    assert (wire.min(axis=0) < 170).mean() > 0.9
    # the rule beside the drawing is not
    if 480 - ox >= 0:
        assert (grey[max(0, y - oy - 4):y - oy + 5, :max(1, 495 - ox)] > 200).all()


def test_placement_of_the_cleaned_crop_is_stored(tmp_path):
    img, d, rules = _notebook()
    y = rules[6]
    d.rectangle([500, y - 120, 800, y - 20], outline=PEN, width=4)
    els = [_el(1, ElementType.FIGURE, (480, y - 140, 820, y), figure=FigureData(description="box"))]
    ink = layout_refine.refine_page(els, img, DPI, W * K, H * K)
    store = ArtifactStore(tmp_path)
    page = Page(page_id=f"{DOC}-p0001", document_id=DOC, page_number=1, width_pt=W * K, height_pt=H * K,
                status=PageStatus.DONE, elements=els)
    figures.save_figure_images(store, page, img, DPI, ink)
    placed = els[0].figure.image_clean_box
    assert placed is not None and len(placed) == 4
    # the box covers the drawing, in points
    assert placed[0] <= 500 * K <= placed[2] and placed[1] <= (y - 120) * K <= placed[3]


def test_end_of_a_written_line_stays_with_the_text_not_the_figure():
    img, d, rules = _notebook()
    pen = PEN
    # a written line whose end reaches into the rectangle of the drawing below and to its right
    for x in (200, 250, 300):
        d.line([(x, 300), (x + 30, 335)], fill=pen, width=4)
    for x in (480, 530, 580):
        d.line([(x, 300), (x + 30, 335)], fill=pen, width=4)
    # the drawing, whose model box starts at x=420, y=280
    d.rectangle([700, 420, 1100, 620], outline=pen, width=4)
    d.line([(700, 500), (450, 500)], fill=pen, width=4)
    els = [_el(1, ElementType.EQUATION, (190, 290, 640, 345), content="R = 2R", latex="R = 2R"),
           _el(2, ElementType.FIGURE, (420, 280, 1120, 640), figure=FigureData(description="circuit"))]
    ink = layout_refine.refine_page(els, img, DPI, W * K, H * K)
    assert ink is not None
    x0, _, x1, _ = _px(els[0].bbox)
    assert x0 < 210 and x1 > 595, (x0, x1)  # the text owns its whole line
    figure_comps = ink.components_of(els[1].element_id)
    stats = ink.stats[figure_comps]
    assert not ((stats[:, 1] < 345) & (stats[:, 0] < 620)).any()


def test_a_label_under_a_text_line_stays_with_the_figure():
    img, d, rules = _notebook()
    for x in range(200, 1000, 60):
        d.line([(x, 326), (x + 30, 372)], fill=PEN, width=4)  # a long line of text, between two rules
    d.line([(600, 386), (630, 432)], fill=PEN, width=4)  # a label right under it
    d.line([(630, 386), (600, 432)], fill=PEN, width=4)
    d.rectangle([500, 490, 900, 680], outline=PEN, width=4)
    els = [_el(1, ElementType.PARAGRAPH, (190, 316, 1010, 440), content="a long line of text"),
           _el(2, ElementType.FIGURE, (480, 380, 920, 700), figure=FigureData(description="box"))]
    ink = layout_refine.refine_page(els, img, DPI, W * K, H * K)
    assert ink is not None
    stats = ink.stats[ink.components_of(els[1].element_id)]
    assert ((stats[:, 1] >= 380) & (stats[:, 1] + stats[:, 3] <= 440)).any()  # the label is in the figure
