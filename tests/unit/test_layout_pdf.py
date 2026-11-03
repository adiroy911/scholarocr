"""Layout PDF export: a typeset remake at the original page size with every element at its box.

Fixtures are hand-built IR (no OCR, no model). Tests that need the Chromium renderer are skipped when no
Chromium-family browser is installed; the PyMuPDF renderer tests always run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

import scholarocr.export.layout_html as lh
import scholarocr.export.layout_pdf as lp
from scholarocr.exceptions import ExportError
from scholarocr.export import FORMATS, ExportContext, export_document
from scholarocr.schemas import (
    BBox,
    Document,
    DocumentType,
    Element,
    ElementType,
    FigureData,
    Manifest,
    ManifestEntry,
    Page,
    PageStatus,
    QuestionInfo,
    SourceFile,
    SourceRef,
    TableCell,
    TableData,
)

DOC_ID = "doc_layout000000001"
A4 = (595.0, 842.0)
LANDSCAPE = (842.0, 595.0)
DPI = 100
HINDI = "प्रकाश संश्लेषण पौधों में होता है"
CLEAN_WORD = "Thermodynamics"
FAITHFUL_WORD = "thermodynamix"
OVERFLOW_TEXT = " ".join(["Overflowing"] * 120)

HAS_BROWSER = lh.find_browser() is not None
needs_browser = pytest.mark.skipif(not HAS_BROWSER, reason="no Chromium-family browser installed")


def _set(el: Element | FigureData, name: str, value) -> None:
    """Set a data-contract field (present in the schema now; object.__setattr__ keeps the test working on a
    schema without it)."""
    if name in type(el).model_fields:
        setattr(el, name, value)
    else:
        object.__setattr__(el, name, value)


class Builder:
    def __init__(self, doc_dir: Path, page_number: int, handwritten: bool = False):
        self.doc_dir, self.n, self.hw = doc_dir, page_number, handwritten
        self.items: list[Element] = []

    @property
    def page_id(self) -> str:
        return f"{DOC_ID}-p{self.n:04d}"

    def add(self, type_: ElementType, content: str = "", box=None, **kw) -> Element:
        idx = len(self.items) + 1
        region = kw.pop("region_image", None)
        el = Element(element_id=f"{self.page_id}-e{idx:03d}", type=type_, page_id=self.page_id, page_number=self.n,
                     reading_order=idx, content=content,
                     bbox=BBox(x0=box[0], y0=box[1], x1=box[2], y1=box[3]) if box else None,
                     bbox_source="refined" if box else None, extraction_method="host_model",
                     handwritten=kw.pop("handwritten", self.hw),
                     source=SourceRef(document_id=DOC_ID, page_number=self.n, source_file="scan.png",
                                      source_page_index=0, page_image=f"pages/{self.n:04d}/original.png",
                                      region_image=region), **kw)
        self.items.append(el)
        return el


def _img(path: Path, size_pt: tuple[float, float], color="navy", label: str = "") -> tuple[int, int]:
    px = (round(size_pt[0] * DPI / 72), round(size_pt[1] * DPI / 72))
    im = Image.new("RGB", px, (236, 238, 245))
    d = ImageDraw.Draw(im)
    d.rectangle((4, 4, px[0] - 5, px[1] - 5), outline=color, width=3)
    d.line((4, px[1] - 5, px[0] - 5, 4), fill=color, width=3)
    if label:
        d.text((10, 10), label, fill=color)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return px


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" viewBox="0 0 200 100">'
       '<rect x="5" y="5" width="190" height="90" fill="none" stroke="#123" stroke-width="3"/>'
       '<path d="M5 95 L195 5" stroke="#123" stroke-width="3"/></svg>')


def build(tmp: Path, *, handwritten: bool = True, page2_image: bool = True) -> tuple[Document, Manifest, Path]:
    doc_dir = tmp / DOC_ID
    (doc_dir / "source").mkdir(parents=True, exist_ok=True)
    scan = doc_dir / "source" / "00_scan.png"
    _img(scan, A4)
    p1, p2 = doc_dir / "pages" / "0001", doc_dir / "pages" / "0002"
    _img(p1 / "original.png", A4, color="darkred")
    if page2_image:
        _img(p2 / "original.png", LANDSCAPE, color="darkgreen")

    b = Builder(doc_dir, 1, handwritten)
    b.add(ElementType.TITLE, "Heat and Light", (60, 40, 400, 80))
    b.add(ElementType.HEADING, "1 Conduction", (60, 90, 300, 112), level=1)
    para = b.add(ElementType.PARAGRAPH, f"The {FAITHFUL_WORD} of a rod: heat flows from $T_A$ to $T_B$ along it.",
                 (60, 120, 540, 160))
    _set(para, "clean_content", f"The {CLEAN_WORD} of a rod: heat flows from $T_A$ to $T_B$ along it.")
    b.add(ElementType.EQUATION, "dQ/dt = -KA dT/dx", (150, 170, 400, 210), latex=r"\frac{dQ}{dt} = -KA\frac{dT}{dx}")
    b.add(ElementType.CHEMICAL_EQUATION, "N2 + 3H2 -> 2NH3", (150, 215, 400, 240), latex=r"\ce{N2 + 3H2 -> 2NH3}")
    table = TableData(header_rows=1, rows=[[TableCell(text="Metal", is_header=True), TableCell(text="Conductivity",
                                                                                                 is_header=True)],
                                           [TableCell(text="Copper"), TableCell(text="401")],
                                           [TableCell(text="Iron"), TableCell(text="80")]])
    b.add(ElementType.TABLE, "", (60, 250, 330, 320), table=table)
    b.add(ElementType.LIST, "", (350, 250, 540, 320), items=["Conduction", "Convection", "Radiation"])
    b.add(ElementType.PARAGRAPH, "Find the rate of heat flow through the slab.", (60, 330, 540, 352),
          question=QuestionInfo(number="4", marks="3"))
    # figure with every image variant
    crop_rel, clean_rel, svg_rel = (f"pages/0001/figures/{b.page_id}-e009{ext}" for ext in (".png", ".clean.png",
                                                                                              ".svg"))
    _img(doc_dir / crop_rel, (200 + 8, 120 + 8), color="purple", label="ORIGINAL")
    _img(doc_dir / clean_rel, (200, 120), color="black", label="CLEAN")
    (doc_dir / svg_rel).write_text(SVG, encoding="utf-8")
    fig = FigureData(description="A rod between two reservoirs", kind="diagram")
    _set(fig, "image_clean", clean_rel)
    _set(fig, "svg", svg_rel)
    # redraws are exported only when they passed the fidelity check (pipeline/svg_fidelity.Fidelity.to_dict)
    _set(fig, "svg_fidelity", {"score": 0.93, "recall": 0.94, "precision": 0.92, "passed": True, "tolerance_px": 4.0,
                               "size": [200, 100], "labels_missing": [], "missing_regions": [], "extra_regions": [],
                               "problems": [], "attempt": 1, "checked_at": "2026-11-03T00:00:00+00:00",
                               "by": "test"})
    b.add(ElementType.FIGURE, "", (60, 370, 260, 490), region_image=crop_rel, figure=fig)
    # figure with no crop at all: cut from the page image
    b.add(ElementType.DIAGRAM, "", (300, 370, 540, 490), figure=FigureData(description="Temperature graph"))
    b.add(ElementType.PAGE_NUMBER, "15", (285, 800, 310, 820))
    b.add(ElementType.PARAGRAPH, "Zymurgy note without a box.")

    c = Builder(doc_dir, 2, handwritten)
    c.add(ElementType.PARAGRAPH, HINDI, (60, 50, 500, 90), language="hi")
    c.add(ElementType.PARAGRAPH, OVERFLOW_TEXT, (60, 120, 160, 130))
    c.add(ElementType.PARAGRAPH, "Upper paragraph overlapping the next one.", (60, 300, 500, 340))
    c.add(ElementType.PARAGRAPH, "Lower paragraph overlapping the previous one.", (60, 330, 500, 370))
    c.add(ElementType.FIGURE, "", (550, 100, 800, 300),
          figure=FigureData(description="Circuit with a battery", figure_label="Fig. 2", labels=["R", "V"]))

    now = "2026-11-03T00:00:00+00:00"
    pages = [Page(page_id=b.page_id, document_id=DOC_ID, page_number=1, width_pt=A4[0], height_pt=A4[1],
                  status=PageStatus.DONE, original_image="pages/0001/original.png", elements=b.items),
             Page(page_id=c.page_id, document_id=DOC_ID, page_number=2, width_pt=LANDSCAPE[0],
                  height_pt=LANDSCAPE[1], status=PageStatus.DONE,
                  original_image="pages/0002/original.png" if page2_image else None, elements=c.items)]
    document = Document(document_id=DOC_ID, title="Layout sample", original_filenames=["scan.png"], page_count=2,
                        created_at=now, extraction_version="test", schema_version="1", status="done", pages=pages,
                        document_type=DocumentType.HANDWRITTEN_NOTES if handwritten else DocumentType.TEXTBOOK)
    sources = [SourceFile(index=0, original_path="/in/scan.png", filename="scan.png", stored_path="source/00_scan.png",
                          sha256="0" * 64, size_bytes=scan.stat().st_size, media_type="image/png", page_count=2)]
    entries = [ManifestEntry(page_number=n, page_id=f"{DOC_ID}-p{n:04d}", source_file="scan.png", source_file_index=0,
                             source_page_index=n - 1, width_pt=w, height_pt=h, content_hash=f"h{n}")
               for n, (w, h) in ((1, A4), (2, LANDSCAPE))]
    return document, Manifest(document_id=DOC_ID, sources=sources, pages=entries), doc_dir


def run(sample, out: Path, **options):
    document, manifest, doc_dir = sample
    ctx = ExportContext(document=document, manifest=manifest, doc_dir=doc_dir, out_dir=out, options=options)
    return export_document(ctx, "layout-pdf")


def el(document: Document, page: int, idx: int) -> Element:
    return document.pages[page - 1].elements[idx - 1]


def words_in(page: pymupdf.Page, box: BBox, pad: float = 2.0, bottom: float | None = None) -> str:
    out = []
    for w in page.get_text("words"):
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if box.x0 - pad <= cx <= box.x1 + pad and box.y0 - pad <= cy <= (bottom or box.y1 + pad):
            out.append(w[4])
    return " ".join(out)


def image_rects(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, int, int]]:
    out = []
    for info in page.get_images(full=True):
        for r in page.get_image_rects(info[0]):
            out.append((r, info[2], info[3]))
    return out


# ------------------------------------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    return build(tmp_path_factory.mktemp("layout"))


@pytest.fixture(scope="module")
def chromium_result(sample, tmp_path_factory):
    if not HAS_BROWSER:
        pytest.skip("no Chromium-family browser installed")
    return run(sample, tmp_path_factory.mktemp("out-chromium"))


@pytest.fixture(scope="module")
def fallback_result(sample, tmp_path_factory):
    return run(sample, tmp_path_factory.mktemp("out-pymupdf"), renderer="pymupdf")


# ------------------------------------------------------------------------------------------------ registry
def test_registered_everywhere():
    assert "layout-pdf" in FORMATS
    from scholarocr.mcp.server import FormatLit as LocalLit
    from scholarocr.remote.server import FormatLit as RemoteLit

    assert "layout-pdf" in LocalLit.__args__ and "layout-pdf" in RemoteLit.__args__


def test_bad_option_rejected(sample, tmp_path):
    with pytest.raises(ExportError, match="figures"):
        run(sample, tmp_path, figures="watercolour", renderer="pymupdf")


# ------------------------------------------------------------------------------------------------ Chromium
@needs_browser
def test_page_sizes_and_orientation(chromium_result):
    v = chromium_result.verification
    assert v["renderer"] == "chromium" and v["page_sizes_match"] and v["page_count_ok"]
    with pymupdf.open(chromium_result.primary_file) as pdf:
        assert pdf.page_count == 2
        assert (round(pdf[0].rect.width, 2), round(pdf[0].rect.height, 2)) == A4
        assert (round(pdf[1].rect.width, 2), round(pdf[1].rect.height, 2)) == LANDSCAPE


@needs_browser
def test_text_lands_inside_its_box(sample, chromium_result):
    document = sample[0]
    fid = chromium_result.verification["layout_fidelity"]
    assert fid["text_in_box_recall"] >= 0.95, fid
    with pymupdf.open(chromium_result.primary_file) as pdf:
        p1 = pdf[0]
        for idx, word in ((1, "Heat"), (2, "Conduction"), (3, "rod"), (6, "Copper"), (7, "Radiation"),
                          (8, "slab"), (11, "15")):
            box = el(document, 1, idx).bbox
            assert word in words_in(p1, box), (idx, word, words_in(p1, box))
        # the title starts at the left edge of its box and is centred in it vertically (PyMuPDF reports the
        # font's full ascender-descender height, which is a little taller than the CSS line box)
        hit = p1.search_for("Heat and Light")[0]
        title = el(document, 1, 1).bbox
        assert abs(hit.x0 - title.x0) < 3
        assert abs((hit.y0 + hit.y1) / 2 - (title.y0 + title.y1) / 2) < 3
        assert hit.height <= title.height * 1.25


@needs_browser
def test_autofit_sizes(sample, chromium_result):
    document = sample[0]
    with pymupdf.open(chromium_result.primary_file) as pdf:
        sizes = {}
        for block in pdf[0].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip():
                        sizes.setdefault(span["text"].strip().split()[0], span["size"])
    assert 6.0 <= sizes["Heat"] <= 40.0
    assert sizes["Heat"] > sizes["The"]  # the title is set larger than the body text
    # every placed text element got a fitted size within its limits
    fid = chromium_result.verification["layout_fidelity"]
    assert fid["fit_share"] is not None and 0 < fid["fit_share"] <= 1
    title = el(document, 1, 1).element_id
    assert title not in fid["overflowed"]


@needs_browser
def test_math_tables_lists_and_question_marks(chromium_result):
    v = chromium_result.verification
    assert v["equations"]["typeset"] == 2 and v["equations"]["plain_text"] == 0
    assert any(f.startswith("KaTeX_") for f in v["fonts_in_pdf"])
    with pymupdf.open(chromium_result.primary_file) as pdf:
        text = pdf[0].get_text()
        assert "NH" in text and "Conductivity" in text and "401" in text
        assert "[3 marks]" in text and re.search(r"4\.\s*Find", text)
        assert "•" in text or pdf[0].get_drawings()  # list bullets (glyph or drawn disc)
        assert pdf[0].find_tables().tables, "the table should be drawn with ruled cells"


@needs_browser
def test_figures_clean_by_default_and_page_crop_fallback(sample, chromium_result):
    document = sample[0]
    v = chromium_result.verification
    assert v["figures"].get("clean") == 1 and v["figures"].get("page_crop") == 2  # p1 diagram + p2 figure
    with pymupdf.open(chromium_result.primary_file) as pdf:
        rects = image_rects(pdf[0])
        fig, diag = el(document, 1, 9).bbox, el(document, 1, 10).bbox
        clean_px = (round(200 * DPI / 72), round(120 * DPI / 72))
        assert any((w, h) == clean_px and abs(r.x0 - fig.x0) < 2 and abs(r.y1 - fig.y1) < 2 for r, w, h in rects), rects
        assert any(abs(r.x0 - diag.x0) < 2 and abs(r.y0 - diag.y0) < 2 and abs(r.x1 - diag.x1) < 2
                   for r, _, _ in rects), rects


@needs_browser
@pytest.mark.parametrize("mode", ["original", "svg"])
def test_figure_options(sample, tmp_path, mode):
    document = sample[0]
    res = run(sample, tmp_path, figures=mode, pages="1")
    assert res.verification["figures"].get(mode) == 1
    fig = el(document, 1, 9).bbox
    with pymupdf.open(res.primary_file) as pdf:
        rects = image_rects(pdf[0])
        inside = [(r, w, h) for r, w, h in rects if r.intersects(pymupdf.Rect(fig.x0, fig.y0, fig.x1, fig.y1))]
        if mode == "original":
            crop_px = (round(208 * DPI / 72), round(128 * DPI / 72))
            assert any((w, h) == crop_px for _, w, h in inside), inside
            # the crop carries a 4 pt margin: drawn at its natural size, centred on the box
            r = next(r for r, w, h in inside if (w, h) == crop_px)
            assert abs(r.width - 208) < 1.5 and abs((r.x0 + r.x1) / 2 - (fig.x0 + fig.x1) / 2) < 1.5
        else:  # the SVG is drawn as vector paths, not a raster image
            assert not inside
            assert any(pymupdf.Rect(fig.x0 - 2, fig.y0 - 2, fig.x1 + 2, fig.y1 + 2).contains(d["rect"])
                       for d in pdf[0].get_drawings())


@needs_browser
def test_placeholder_when_no_image_at_all(tmp_path):
    sample = build(tmp_path / "s", page2_image=False)
    res = run(sample, tmp_path / "o", pages="2")
    fig = el(sample[0], 2, 5)
    assert res.verification["figures"].get("placeholder") == 1
    assert any(fig.element_id in w and "description" in w for w in res.warnings)
    with pymupdf.open(res.primary_file) as pdf:
        assert "Circuit" in words_in(pdf[0], fig.bbox)
        assert pdf[0].get_drawings(), "the placeholder is drawn as a bordered box"


@needs_browser
def test_hindi_text(sample, chromium_result):
    v = chromium_result.verification
    assert any("Devanagari" in f for f in v["fonts_in_pdf"]), v["fonts_in_pdf"]
    with pymupdf.open(chromium_result.primary_file) as pdf:
        text = words_in(pdf[1], el(sample[0], 2, 1).bbox)
        assert len(re.findall(r"[ऀ-ॿ]", text)) >= 10, text


@needs_browser
def test_overflow_warning_and_overlap(sample, chromium_result):
    document = sample[0]
    fid = chromium_result.verification["layout_fidelity"]
    over = el(document, 2, 2).element_id
    assert fid["overflowed"] == [over]
    assert any("overflow downward" in w and over in w for w in chromium_result.warnings)
    upper, lower = el(document, 2, 3), el(document, 2, 4)
    assert {upper.element_id, lower.element_id} <= set(fid["boxes_adjusted"])
    with pymupdf.open(chromium_result.primary_file) as pdf:
        up = [w for w in pdf[1].get_text("words") if w[4] == "Upper"][0]
        low = [w for w in pdf[1].get_text("words") if w[4] == "Lower"][0]
        assert up[3] <= low[1] + 0.5, (up, low)  # no overprinting: the upper line ends above the lower one
    assert el(document, 1, 12).element_id in fid["unplaced"]


@needs_browser
def test_text_variants(sample, tmp_path, chromium_result):
    with pymupdf.open(chromium_result.primary_file) as pdf:  # handwritten document -> clean by default
        assert CLEAN_WORD in pdf[0].get_text() and FAITHFUL_WORD not in pdf[0].get_text()
    assert chromium_result.verification["text_variant"] == "clean"
    res = run(sample, tmp_path, text_variant="faithful", pages="1")
    with pymupdf.open(res.primary_file) as pdf:
        assert FAITHFUL_WORD in pdf[0].get_text() and CLEAN_WORD not in pdf[0].get_text()


@needs_browser
def test_printed_document_defaults_to_faithful(tmp_path):
    sample = build(tmp_path / "s", handwritten=False)
    res = run(sample, tmp_path / "o", pages="1", background="paper", show_boxes=True)
    assert res.verification["text_variant"] == "faithful"
    with pymupdf.open(res.primary_file) as pdf:
        assert FAITHFUL_WORD in pdf[0].get_text()
        assert pdf[0].get_drawings()  # tinted page and debug outlines


# ------------------------------------------------------------------------------------------------ PyMuPDF
def test_fallback_renderer(sample, fallback_result):
    document = sample[0]
    v = fallback_result.verification
    assert v["renderer"] == "pymupdf" and v["page_sizes_match"] and v["equations"]["plain_text"] == 2
    with pymupdf.open(fallback_result.primary_file) as pdf:
        assert (round(pdf[1].rect.width), round(pdf[1].rect.height)) == (842, 595)
        assert "Heat" in words_in(pdf[0], el(document, 1, 1).bbox)
        assert CLEAN_WORD in pdf[0].get_text()
    assert el(document, 2, 2).element_id in v["layout_fidelity"]["overflowed"]


def test_no_browser_falls_back_or_errors(sample, tmp_path, monkeypatch):
    monkeypatch.setattr(lh, "find_browser", lambda explicit=None: None)
    res = run(sample, tmp_path / "a", pages="2")
    assert res.verification["renderer"] == "pymupdf"
    assert any("No Chromium-based browser" in w for w in res.warnings)
    with pytest.raises(ExportError, match="Chromium-based browser"):
        run(sample, tmp_path / "b", renderer="chromium")


# ------------------------------------------------------------------------------------------------ helpers
def _item(eid: str, box, kind="text") -> lp.Item:
    e = Element(element_id=eid, type=ElementType.PARAGRAPH, page_id="p", page_number=1, reading_order=0,
                source=SourceRef(document_id="d", page_number=1, source_file="f", source_page_index=0))
    b = lp.Box(*box)
    return lp.Item(e, kind, "body", b, b.copy())


def test_resolve_overlaps_splits_and_trims():
    page = Page(page_id="p", document_id="d", page_number=1, width_pt=600, height_pt=800)
    plan = lp.PagePlan(page, 600, 800, items=[
        _item("a", (50, 100, 500, 140)), _item("b", (50, 130, 500, 170)),  # stacked text: share the band
        _item("fig", (50, 300, 300, 400), kind="figure"), _item("c", (40, 280, 500, 310)),  # trimmed out of it
        _item("big", (50, 500, 500, 600)), _item("edge", (60, 505, 120, 525)),  # nested at the top: cut out
        _item("box", (50, 650, 500, 750)), _item("mid", (200, 690, 300, 710)),  # nested in the middle: reported
    ])
    lp.resolve_overlaps(plan)
    a, b, fig, c = plan.items[:4]
    assert a.box.y1 == b.box.y0 == 135 and a.adjusted and b.adjusted
    assert c.box.y1 == 300 and c.box.y0 == 280 and fig.box.y0 == 300 and not fig.adjusted
    big, edge, box, mid = plan.items[4:]
    assert big.box.y0 == 525 and big.adjusted and edge.box.y0 == 505
    assert plan.overlaps == ["box~mid"] and mid.over_figure and box.box.y0 == 650


def test_shared_helper_fallbacks(sample):
    document, _, doc_dir = sample
    fig = el(document, 1, 9)
    assert lp._figure_image_path_fallback(doc_dir, fig, "clean").name.endswith(".clean.png")
    assert lp._figure_image_path_fallback(doc_dir, fig, "original").name.endswith("-e009.png")
    assert lp._figure_image_path_fallback(doc_dir, fig, "svg").suffix == ".svg"
    assert lp._figure_image_path_fallback(doc_dir, el(document, 1, 10), "clean") is None
    para = el(document, 1, 3)
    assert CLEAN_WORD in lp._element_text_fallback(para, "clean")
    assert FAITHFUL_WORD in lp._element_text_fallback(para, "faithful")


def test_rich_text_markup():
    h, plain, words = lp.rich_text("~~After some t\nFrom $x^2$ start~~ and **bold** [crossed out: Supp]")
    assert h.count("<s>") == 3 and '<span class="im" data-tex="x^2"' in h and "<b>bold</b>" in h
    assert "~~" not in plain and "x²" in plain and "x" not in words.split()


def test_whiten_image(tmp_path):
    src = tmp_path / "c.png"
    im = Image.new("RGB", (120, 80), (180, 190, 205))  # bluish-grey photographed paper
    ImageDraw.Draw(im).line((10, 40, 110, 40), fill=(30, 40, 150), width=4)
    im.save(src)
    out = lp.whiten_image(src, tmp_path / "w.png")
    with Image.open(out) as w:
        assert w.getpixel((60, 10)) == (255, 255, 255)
        r, g, b = w.getpixel((60, 40))
        assert b > r + 40 and r < 120  # the stroke keeps its blue colour


# ------------------------------------------------------------------------------------------------ shared sizes
def build_sizes(tmp: Path) -> tuple[Document, Manifest, Path]:
    """One handwritten-looking page: paragraph boxes of different heights (handwriting is uneven), a paragraph
    in a box too short for its text with free paper below it, and a display equation in a tight box with free
    paper to its right."""
    doc_dir = tmp / DOC_ID
    (doc_dir / "source").mkdir(parents=True, exist_ok=True)
    scan = doc_dir / "source" / "00_scan.png"
    _img(scan, A4)
    _img(doc_dir / "pages" / "0001" / "original.png", A4, color="darkred")
    b = Builder(doc_dir, 1, True)
    b.add(ElementType.HEADING, "Steady state", (60, 40, 300, 64), level=1)
    b.add(ElementType.PARAGRAPH, "As the heat current is set up, the temperatures at various positions vary with time.",
          (60, 80, 540, 120))
    b.add(ElementType.PARAGRAPH, "A stable temperature gradient has now been established along the rod.",
          (60, 130, 540, 175))
    b.add(ElementType.PARAGRAPH, "Mathematically, at the steady state the temperature depends on position only.",
          (60, 185, 540, 215))
    b.add(ElementType.PARAGRAPH, "This paragraph sits in a box that is far too short for all of its words, but the "
                                 "paper below it is empty, so the box grows down instead of the text shrinking.",
          (60, 230, 300, 244))
    b.add(ElementType.EQUATION, "T = T(x, t)", (60, 400, 95, 412), latex=r"T \equiv T(x, t)")
    now = "2026-11-03T00:00:00+00:00"
    page = Page(page_id=b.page_id, document_id=DOC_ID, page_number=1, width_pt=A4[0], height_pt=A4[1],
                status=PageStatus.DONE, original_image="pages/0001/original.png", elements=b.items)
    document = Document(document_id=DOC_ID, title="Sizes", original_filenames=["scan.png"], page_count=1,
                        created_at=now, extraction_version="test", schema_version="1", status="done", pages=[page],
                        document_type=DocumentType.HANDWRITTEN_NOTES)
    sources = [SourceFile(index=0, original_path="/in/scan.png", filename="scan.png", stored_path="source/00_scan.png",
                          sha256="0" * 64, size_bytes=scan.stat().st_size, media_type="image/png", page_count=1)]
    entries = [ManifestEntry(page_number=1, page_id=f"{DOC_ID}-p0001", source_file="scan.png", source_file_index=0,
                             source_page_index=0, width_pt=A4[0], height_pt=A4[1], content_hash="h1")]
    return document, Manifest(document_id=DOC_ID, sources=sources, pages=entries), doc_dir


def _span_sizes(page: pymupdf.Page) -> dict[str, float]:
    out = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip() and not span["font"].startswith("KaTeX"):
                    out.setdefault(span["text"].strip().split()[0], round(span["size"], 2))
    return out


@pytest.mark.parametrize("renderer", [pytest.param("chromium", marks=needs_browser), "pymupdf"])
def test_one_body_size_and_growth_before_shrinking(tmp_path, renderer):
    sample = build_sizes(tmp_path / "s")
    document = sample[0]
    res = run(sample, tmp_path / "o", renderer=renderer)
    fid = res.verification["layout_fidelity"]
    body = fid["body_size"][1]
    assert body and fid["body_size_scope"][1] == "document"
    short = el(document, 1, 5)
    assert short.element_id in fid["moved"] and short.element_id not in fid["shrunk"]
    assert fid["overflowed"] == []
    with pymupdf.open(res.primary_file) as pdf:
        sizes = _span_sizes(pdf[0])
        # every paragraph is set at the one body size, whatever the height of its box
        for word in ("As", "A", "Mathematically,", "This"):
            assert abs(sizes[word] - body) < 0.15, (word, sizes, body)
        assert sizes["Steady"] >= body * 1.25  # headings at a fixed ratio above it
        # the short box grew down into the free paper: its last words sit below the original box
        moved = next(m for m in fid["moved_detail"] if m["element_id"] == short.element_id)
        assert "down" in moved["grew"] and moved["box"][3] > short.bbox.y1 + 10
        assert "shrinking." in words_in(pdf[0], BBox(x0=moved["box"][0], y0=moved["box"][1], x1=moved["box"][2],
                                                     y1=moved["box"][3]))
    assert fid["shared_size_share"] == 1.0 and fid["fit_share"] == fid["shared_size_share"]


@needs_browser
def test_equation_in_a_tight_box_extends_instead_of_shrinking(tmp_path):
    sample = build_sizes(tmp_path / "s")
    eq = el(sample[0], 1, 6)
    res = run(sample, tmp_path / "o")
    fid = res.verification["layout_fidelity"]
    body = fid["body_size"][1]
    moved = next(m for m in fid["moved_detail"] if m["element_id"] == eq.element_id)
    assert moved["grew"].startswith("right") and moved["box"][2] > eq.bbox.x1 + 20
    assert eq.element_id not in fid["shrunk"]
    with pymupdf.open(res.primary_file) as pdf:
        spans = [s for b in pdf[0].get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"]
                 if s["font"].startswith("KaTeX") and s["text"].strip() and s["bbox"][1] > 390]
        assert spans and max(s["size"] for s in spans) >= 0.9 * body
        # one line: everything of the equation sits within one line height of its top
        assert max(s["bbox"][3] for s in spans) - min(s["bbox"][1] for s in spans) < 2.2 * body


def test_inline_math_keeps_touching_punctuation_and_bullets():
    h, plain, _ = lp.rich_line("• $T = T(x)$ and length ($dx$) of slab")
    assert h.startswith('<span class="nw">•&nbsp;<span class="im"')
    assert '<span class="nw">(<span class="im" data-tex="dx"' in h and plain.endswith("(dx) of slab")
