"""Cleaned / as-written text and figure images (crop, cleaned crop, redrawn SVG) across exports. Hand-built IR;
nothing calls a model."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import docx as python_docx
import pytest
from PIL import Image, ImageDraw

from scholarocr.export import ExportContext, export_document
from scholarocr.export.base import (default_text_variant, element_text, figure_image_path, resolve_text_variant,
                                    with_text_variant)
from scholarocr.schemas import ElementType
from tests.fixtures.ir_samples import DISTINCTIVE, build_sample

CLEAN_MARK = "converts light energy into chemical energy. Cleaned"
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100"><rect x="5" y="5" width="190" height="90" '
       'fill="none" stroke="#111" stroke-width="2"/><text x="20" y="50" font-size="16">Rate</text></svg>')


@pytest.fixture
def sample(tmp_path):
    document, manifest, doc_dir = build_sample(tmp_path / "docs")
    page2 = document.pages[1]
    para = next(e for e in page2.elements if e.type == ElementType.PARAGRAPH and DISTINCTIVE in e.content)
    para.clean_content = para.content.replace("converts light energy into chemical energy.", CLEAN_MARK)
    para.clean_content_source = "model"
    fig = next(e for e in page2.elements if e.type == ElementType.FIGURE)
    figdir = doc_dir / "pages" / "0002" / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    crop = figdir / f"{fig.element_id}.png"
    Image.open(doc_dir / fig.source.region_image).save(crop)
    clean = Image.new("RGB", (300, 180), "white")
    ImageDraw.Draw(clean).line([(10, 170), (290, 10)], fill=(20, 40, 160), width=4)
    clean.save(figdir / f"{fig.element_id}.clean.png")
    (figdir / f"{fig.element_id}.svg").write_text(SVG, encoding="utf-8")
    fig.source.region_image = f"pages/0002/figures/{fig.element_id}.png"
    fig.figure.image_clean = f"pages/0002/figures/{fig.element_id}.clean.png"
    fig.figure.svg = f"pages/0002/figures/{fig.element_id}.svg"
    fig.figure.svg_fidelity = {"passed": True, "score": 0.94, "recall": 0.95, "precision": 0.93}
    return document, manifest, doc_dir, para, fig


def run(sample, tmp_path, fmt, **options):
    document, manifest, doc_dir, *_ = sample
    ctx = ExportContext(document=document, manifest=manifest, doc_dir=doc_dir, out_dir=tmp_path / "out" / fmt,
                        options=options)
    return export_document(ctx, fmt)


def test_helpers(sample):
    document, _, doc_dir, para, fig = sample
    assert element_text(para, "clean") == para.clean_content and element_text(para, "faithful") == para.content
    assert figure_image_path(doc_dir, fig, "clean").name.endswith(".clean.png")
    assert figure_image_path(doc_dir, fig, "original").name == f"{fig.element_id}.png"
    assert figure_image_path(doc_dir, fig, "svg").suffix == ".svg"
    (doc_dir / fig.figure.svg).unlink()
    assert figure_image_path(doc_dir, fig, "svg").name.endswith(".clean.png")  # falls back
    assert default_text_variant(document) == "faithful"  # printed sample
    for p in document.pages:
        for e in p.elements:
            e.handwritten = True
    assert default_text_variant(document) == "clean" and resolve_text_variant(document, None) == "clean"
    assert resolve_text_variant(document, "faithful") == "faithful"
    assert para.content != with_text_variant(document, "clean").pages[1].elements[para.reading_order].content
    assert para.content == next(e for e in document.pages[1].elements if e.element_id == para.element_id).content


@pytest.mark.parametrize("variant, expect_clean", [("clean", True), ("faithful", False), (None, False)])
def test_text_variant_in_markdown_docx_latex(sample, tmp_path, variant, expect_clean):
    opts = {"text_variant": variant} if variant else {}
    md = Path(run(sample, tmp_path, "markdown", **opts).primary_file).read_text(encoding="utf-8")
    assert (CLEAN_MARK in md) is expect_clean
    d = python_docx.Document(run(sample, tmp_path, "docx", **opts).primary_file)
    assert any(CLEAN_MARK in p.text for p in d.paragraphs) is expect_clean
    tex = Path(run(sample, tmp_path, "latex", **opts).primary_file).read_text(encoding="utf-8")
    assert (CLEAN_MARK in tex) is expect_clean


def test_json_keeps_both_variants_and_figure_paths(sample, tmp_path):
    _, _, _, para, fig = sample
    res = run(sample, tmp_path, "json", text_variant="clean")
    data = json.loads(Path(res.primary_file).read_text(encoding="utf-8"))
    els = {e["element_id"]: e for p in data["pages"] for e in p["elements"]}
    assert els[para.element_id]["content"] == para.content and els[para.element_id]["clean_content"] == para.clean_content
    assert els[fig.element_id]["figure"]["image_clean"].endswith(".clean.png")
    assert els[fig.element_id]["figure"]["svg"].endswith(".svg")
    assert res.verification["text_variant"] == "clean"


def test_searchable_pdf_records_variant(sample, tmp_path):
    res = run(sample, tmp_path, "searchable-pdf", text_variant="clean")
    assert res.verification["text_variant"] == "clean"


@pytest.mark.parametrize("variant, suffix", [(None, ".clean.png"), ("original", ".png"), ("redrawn", ".svg")])
def test_markdown_writes_figure_files_next_to_it(sample, tmp_path, variant, suffix):
    _, _, _, _, fig = sample
    res = run(sample, tmp_path, "markdown", **({"figure_variant": variant} if variant else {}))
    md = Path(res.primary_file)
    text = md.read_text(encoding="utf-8")
    link = f"assets/{fig.element_id}{suffix}"
    assert f"]({link})" in text and (md.parent / link).is_file()
    assert res.verification["broken_image_links"] == []
    alt = text.split(f"]({link})")[0].rsplit("![", 1)[1]
    assert alt.startswith("Graph of reaction rate")  # description as alt text, and below as the caption
    assert "Graph of reaction rate against temperature" in text.split(link, 1)[1]


def test_docx_inline_picture_sized_to_the_page(sample, tmp_path):
    document, _, _, _, fig = sample
    page = document.pages[1]
    res = run(sample, tmp_path, "docx")
    d = python_docx.Document(res.primary_file)
    assert res.verification["images"] == res.verification["images_expected"] == 1
    shape = d.inline_shapes[0]
    sec = d.sections[0]
    text_w = (sec.page_width - sec.left_margin - sec.right_margin)
    assert abs(shape.width / text_w - fig.bbox.width / page.width_pt) < 0.01  # same share of the width
    with zipfile.ZipFile(res.primary_file) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    assert len(media) == 1
    redrawn = run(sample, tmp_path, "docx", figure_variant="redrawn")  # SVG rasterised for Word
    assert redrawn.verification["images"] == 1 and not any("could not" in w for w in redrawn.warnings)


def test_latex_includegraphics_for_each_variant(sample, tmp_path):
    res = run(sample, tmp_path, "latex")
    tex = Path(res.primary_file).read_text(encoding="utf-8")
    assert "\\includegraphics[width=" in tex and "{assets/fig001.png}" in tex and "\\linewidth" in tex
    res = run(sample, tmp_path, "latex", figure_variant="redrawn")
    tex = Path(res.primary_file).read_text(encoding="utf-8")
    assert "{assets/fig001.pdf}" in tex  # the SVG as a vector PDF
    assert (Path(res.primary_file).parent / "assets" / "fig001.pdf").read_bytes().startswith(b"%PDF")


def test_a_redraw_that_failed_the_check_is_never_exported(sample, tmp_path):
    _, _, doc_dir, _, fig = sample
    fig.figure.svg_fidelity = {"passed": False, "score": 0.41}
    assert figure_image_path(doc_dir, fig, "svg").name.endswith(".clean.png")
    for fmt in ("markdown", "docx", "latex"):
        res = run(sample, tmp_path, fmt, figure_variant="redrawn")
        assert any("not faithful (fidelity 41%)" in w for w in res.warnings), (fmt, res.warnings)
    md = Path(run(sample, tmp_path, "markdown", figure_variant="redrawn").primary_file).read_text(encoding="utf-8")
    assert ".svg" not in md and f"assets/{fig.element_id}.clean.png" in md
