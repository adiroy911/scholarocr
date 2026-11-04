"""Layout PDF defects found on a real handwritten document, rebuilt on synthetic pages. Each test also checks that
verification reports the failure when it does happen: passing silently was half of the problem."""

from __future__ import annotations

import pymupdf
import pytest
from PIL import Image

from scholarocr.export import base, layout_html
from scholarocr.export import layout_pdf as lp
from scholarocr.export.base import ExportContext
from scholarocr.schemas import (
    BBox,
    Document,
    DocumentType,
    Element,
    ElementType,
    FigureData,
    Manifest,
    Page,
    PageStatus,
    QuestionInfo,
    SourceRef,
)
from scholarocr.schemas.document import ManifestEntry, SourceFile

needs_browser = pytest.mark.skipif(layout_html.find_browser() is None, reason="no Chromium-family browser")
A4 = (595.0, 842.0)
DOC_ID = "doc_0000000000000bad"
LONG = (r"\Rightarrow \left(\frac{a_1 b_1}{c_1} + \frac{a_2 b_2}{c_2}\right) = y \left(\frac{a_1}{c_1} + "
        r"\frac{a_2}{c_2}\right) \Rightarrow y = \left(\frac{a_1 b_1 c_2 + a_2 b_2 c_1}{a_1 c_2 + a_2 c_1}\right) "
        r"= \frac{a_1 b_1 c_2 + a_2 b_2 c_1}{a_1 c_2 + a_2 c_1} \quad [\text{Ans (ii)}]")


def _source(n: int) -> SourceRef:
    return SourceRef(document_id=DOC_ID, page_number=n, source_file="s.pdf", source_page_index=n - 1)


def _doc(tmp_path, specs: list[list[dict]], *, size=A4) -> ExportContext:
    pages = []
    for pn, spec in enumerate(specs, 1):
        els = []
        for i, s in enumerate(spec, 1):
            x0, y0, x1, y1 = s.pop("box")
            s.setdefault("content", s.get("latex") or "")
            els.append(Element(element_id=f"{DOC_ID}-p{pn:04d}-e{i:03d}", page_id=f"{DOC_ID}-p{pn:04d}",
                               page_number=pn, reading_order=i, bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1),
                               bbox_source="refined", source=_source(pn), **s))
        pages.append(Page(page_id=f"{DOC_ID}-p{pn:04d}", document_id=DOC_ID, page_number=pn, width_pt=size[0],
                          height_pt=size[1], status=PageStatus.DONE, elements=els))
    n = len(pages)
    document = Document(document_id=DOC_ID, title="t", original_filenames=["s.pdf"], page_count=n,
                        created_at="2026-11-04T00:00:00+00:00", extraction_version="t", schema_version="1",
                        status="done", pages=pages, document_type=DocumentType.HANDWRITTEN_NOTES)
    sources = [SourceFile(index=0, original_path="/in/s.pdf", filename="s.pdf", stored_path="source/00_s.pdf",
                          sha256="0" * 64, size_bytes=1, media_type="application/pdf", page_count=n)]
    entries = [ManifestEntry(page_number=i + 1, page_id=f"{DOC_ID}-p{i + 1:04d}", source_file="s.pdf",
                             source_file_index=0, source_page_index=i, width_pt=size[0], height_pt=size[1],
                             content_hash=f"h{i}") for i in range(n)]
    return ExportContext(document=document, manifest=Manifest(document_id=DOC_ID, sources=sources, pages=entries),
                         doc_dir=tmp_path, out_dir=tmp_path / "out")


def _export(ctx: ExportContext):
    res = lp.export_layout_pdf(ctx)
    return res, res.verification["layout_fidelity"]


def test_split_at_relations_keeps_groups_whole():
    assert lp.split_at_relations("a = b") == ["a", "= b"]
    assert lp.split_at_relations(r"\frac{a=b}{c}") == []
    assert lp.split_at_relations(r"\left(a = b\right) \Rightarrow c \quad [\text{x=y}]") == [
        r"\left(a = b\right)", r"\Rightarrow c", r"\quad [\text{x=y}]"]
    assert lp.split_at_relations(r"\begin{aligned} a &= b \\ c &= d \end{aligned}") == []
    assert lp.split_at_relations(r"\{a\} = b") == [r"\{a\}", "= b"]
    assert lp.split_at_relations("") == []


@pytest.mark.parametrize("text,bare", [
    (r"\log \frac{a}{b_2} \quad \frac{b_1}{a}", True),
    (r"\alpha_1", True),
    (r"The value of \alpha is large here", False),
    (r"$\alpha$ and \beta", False),
    ("plain words", False),
])
def test_is_bare_latex(text, bare):
    assert lp.is_bare_latex(text) is bare


def test_crossed_out_forms():
    h, plain, _ = lp.rich_text(r"Ans: a) [crossed out: $y = \frac{a}{b}$] done")
    assert "crossed" not in h and '<span class="sm"><span class="im"' in h and "crossed" not in plain
    h, plain, words = lp.rich_text(r"values and [crossed out] $y_1$")
    assert 'class="sm sgap"' in h and "crossed" not in h and "crossed" not in plain and "crossed" not in words
    assert lp._GAP not in plain
    h, _, _ = lp.rich_text("[crossed out: old word] kept")
    assert "<s>old word</s>" in h


def test_question_part_named_in_the_text_is_not_added():
    assert base.question_lead("a", "Find a) the total along the line") == ""
    assert base.question_lead("b", "(b) The total") == ""
    assert base.question_lead("5", "A circle of radius R") == "5"
    assert base.question_lead("a", "A person walks") == "a"


@needs_browser
def test_long_equation_wraps_instead_of_being_cut_off(tmp_path):
    ctx = _doc(tmp_path, [[{"type": ElementType.EQUATION, "box": (27, 695, 595, 780), "latex": LONG}]])
    res, fid = _export(ctx)
    assert fid["clipped_by_page"] == []
    with pymupdf.open(res.primary_file) as pdf:
        words = [w for w in pdf[0].get_text("words") if w[4].replace("\u200b", "").strip()]
        answer = [w for w in words if "Ans" in w[4] or "(ii)" in w[4]]
        assert answer, [w[4] for w in words]
        for w in words:
            assert w[2] <= A4[0] + 0.5


@needs_browser
def test_equation_starting_late_on_the_line_stays_on_the_page(tmp_path):
    latex = r"m_{\text{mean}} = \frac{\sum m_i n_i}{\sum n_i} \quad (\text{by count}) = \frac{\int m \, dn}{\pi r^2}"
    ctx = _doc(tmp_path, [[{"type": ElementType.EQUATION, "box": (430, 393, 590, 446), "latex": latex}]])
    res, fid = _export(ctx)
    assert fid["clipped_by_page"] == []
    with pymupdf.open(res.primary_file) as pdf:
        assert max(w[2] for w in pdf[0].get_text("words")) <= A4[0] + 0.5


@needs_browser
def test_bare_latex_is_typeset_not_printed(tmp_path):
    latex = r"\log \frac{a}{b_2} \quad \frac{b_1}{a}"
    ctx = _doc(tmp_path, [[{"type": ElementType.TEXT, "box": (302, 147, 405, 184), "content": latex}]])
    res, fid = _export(ctx)
    assert fid["raw_latex"] == []
    with pymupdf.open(res.primary_file) as pdf:
        assert "\\" not in pdf[0].get_text()


@needs_browser
def test_body_size_does_not_jump_between_pages_of_a_shape(tmp_path):
    words = "words in a paragraph of ordinary handwriting that runs across the page " * 2
    tight = [{"type": ElementType.PARAGRAPH, "box": (40, 60 + i * 70, 560, 110 + i * 70), "content": words}
             for i in range(4)]
    roomy = [{"type": ElementType.PARAGRAPH, "box": (40, 60 + i * 140, 560, 180 + i * 140), "content": "few words"}
             for i in range(4)]
    pages = [[dict(s) for s in tight] for _ in range(3)] + [roomy]
    _, fid = _export(_doc(tmp_path, pages))
    sizes = fid["body_size"]
    assert sizes[4] == sizes[1] and fid["body_size_scope"][4] == "document"


def test_struck_formula_and_gap_have_a_visible_line():
    assert ".c .sm::after" in layout_html.BASE_CSS and "border-top" in layout_html.BASE_CSS


def test_font_loading_precedes_measuring():
    js = layout_html.FIT_JS
    assert js.index("document.fonts.forEach") > js.index("function start()")
    assert "f.load()" in js


def _write_crop(path, size=(200, 100), ink=None):
    img = Image.new("RGB", size, (255, 255, 255))
    if ink:
        img.paste((20, 30, 150), ink)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _figure_spec(rel: str, box, placed):
    return {"type": ElementType.FIGURE, "box": box, "content": "",
            "figure": FigureData(image_clean=rel, image_clean_box=list(placed))}


@needs_browser
def test_cleaned_crop_is_placed_exactly_and_empty_corner_is_no_overlap(tmp_path):
    rel = "pages/0001/figures/f.clean.png"
    _write_crop(tmp_path / rel, ink=(100, 50, 200, 100))  # strokes in the lower right; the upper left is paper
    ctx = _doc(tmp_path, [[_figure_spec(rel, (100, 100, 300, 200), (104.0, 102.0, 296.0, 198.0)),
                           {"type": ElementType.PARAGRAPH, "box": (100, 100, 190, 125), "content": "Label text"}]])
    res, fid = _export(ctx)
    plan = lp.plan_page(ctx, ctx.document.pages[0], lp.parse_options(ctx), tmp_path / "assets")
    fig = next(i for i in plan.items if i.kind == "figure")
    text = next(i for i in plan.items if i.kind != "figure")
    assert (fig.image_box.x0, fig.image_box.y0, fig.image_box.x1) == (104.0, 102.0, 296.0)
    assert not text.over_figure and not text.adjusted  # nothing under it: not trimmed, no backing
    assert fid["overprinted"] == []


@needs_browser
def test_verification_reports_text_printed_over_a_figures_strokes(tmp_path):
    rel = "pages/0001/figures/f.clean.png"
    _write_crop(tmp_path / rel, ink=(0, 0, 200, 100))  # solid ink under everything
    ctx = _doc(tmp_path, [[_figure_spec(rel, (100, 100, 300, 200), (100.0, 100.0, 300.0, 200.0)),
                           {"type": ElementType.PARAGRAPH, "box": (120, 130, 280, 170), "content": "Some words here"}]])
    plan = lp.plan_page(ctx, ctx.document.pages[0], lp.parse_options(ctx), tmp_path / "assets")
    for it in plan.items:
        it.over_figure = False  # the case the layout should have avoided
    browser = layout_html.find_browser()
    raw, _ = lp.render_chromium([plan], browser, _ensure(tmp_path / "w"), title="t", lang=None,
                                opts=lp.parse_options(ctx))
    with pymupdf.open(raw) as pdf:
        pairs = lp._overprinted(pdf[0], plan)
    assert pairs and all("~" in p for p in pairs)


def _ensure(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def _printed_over_each_other(tmp_path):
    """A PDF with two lines printed on top of each other, and the plan of those two elements."""
    ctx = _doc(tmp_path, [[{"type": ElementType.PARAGRAPH, "box": (100, 100, 400, 130), "content": "Overlapping words"},
                           {"type": ElementType.PARAGRAPH, "box": (100, 100, 400, 130), "content": "Overlapping words"}]])
    pdf = pymupdf.open()
    page = pdf.new_page(width=A4[0], height=A4[1])
    page.insert_text((100, 120), "Overlapping words", fontsize=13)
    page.insert_text((100, 120), "Overlapping words", fontsize=13)
    a = lp.Item(ctx.document.pages[0].elements[0], "text", "body", lp.Box(100, 100, 300, 130), lp.Box(100, 100, 300, 130))
    b = lp.Item(ctx.document.pages[0].elements[1], "text", "body", lp.Box(100, 100, 300, 130), lp.Box(100, 100, 300, 130))
    plan = lp.PagePlan(ctx.document.pages[0], A4[0], A4[1], items=[a, b])
    return pdf, plan


def test_verification_reports_elements_printed_over_each_other(tmp_path):
    pdf, plan = _printed_over_each_other(tmp_path)
    assert lp._overprinted(pdf[0], plan) == [f"{plan.items[0].el.element_id}~{plan.items[1].el.element_id}"]


def test_overlap_inside_one_element_is_not_reported(tmp_path):
    pdf, plan = _printed_over_each_other(tmp_path)
    plan.items.pop()
    assert lp._overprinted(pdf[0], plan) == []


def test_verification_reports_raw_latex(tmp_path):
    pdf, plan = _printed_over_each_other(tmp_path)
    page = pdf.new_page(width=A4[0], height=A4[1])
    page.insert_text((100, 120), r"\frac{a}{b}", fontsize=13)
    assert lp._raw_latex(page, plan) == [plan.items[0].el.element_id]


def test_clipped_check_reads_the_laid_out_text():
    el = Element(element_id=f"{DOC_ID}-p0001-e001", page_id="p", page_number=1, reading_order=1,
                 type=ElementType.EQUATION, content="x", bbox=BBox(x0=1, y0=1, x1=2, y1=2), source=_source(1))
    page = Page(page_id="p", document_id=DOC_ID, page_number=1, width_pt=100, height_pt=100,
                status=PageStatus.DONE, elements=[el])
    plan = lp.PagePlan(page, 100, 100)
    item = lp.Item(el, "math", "math", lp.Box(1, 1, 2, 2), lp.Box(1, 1, 2, 2))
    plan.items.append(item)
    item.ink = lp.Box(10, 10, 90, 20)
    assert lp._clipped(plan) == []
    item.ink = lp.Box(10, 10, 130, 20)
    assert lp._clipped(plan) == [el.element_id]


def test_text_variant_auto_is_accepted(tmp_path):
    ctx = _doc(tmp_path, [[{"type": ElementType.PARAGRAPH, "box": (40, 60, 400, 90), "content": "x"}]])
    ctx.options["text_variant"] = "auto"
    assert lp.parse_options(ctx).text_variant == "clean"


@needs_browser
def test_question_part_in_a_real_export(tmp_path):
    ctx = _doc(tmp_path, [[{"type": ElementType.PARAGRAPH, "box": (40, 60, 400, 90), "content": "Find a) the total",
                            "question": QuestionInfo(number="a", parent_number="2")}]])
    res, _ = _export(ctx)
    with pymupdf.open(res.primary_file) as pdf:
        assert pdf[0].get_text().split()[0] == "Find"
